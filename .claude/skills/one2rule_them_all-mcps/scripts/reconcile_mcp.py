#!/usr/bin/env python3
"""
reconcile_mcp.py — Reconcile MCP server definitions across every MCP client config.

The problem this solves: each MCP client (Claude Desktop, Claude Code, Codex, Gemini
CLI, Antigravity, Cursor) keeps its own list of MCP servers in its own file and format.
Over time the lists drift — a server you added in Cursor is missing from Codex, etc.
This tool reads every config that exists on the machine, computes the UNION of all
servers, and (on --apply) writes that full union back into each client in that client's
native format — without ever dropping a server and without touching anything else in
the file.

Design guarantees:
  * No server is ever deleted. Every client ends with at least the servers it had.
  * Only the mcpServers / mcp_servers section is rewritten; all other settings in the
    file (Codex profiles, Claude Code projects/history, Gemini settings...) are kept.
  * Every file is backed up before it is written.
  * If the same server NAME has DIFFERENT definitions in two clients, the tool refuses
    to guess: it reports the conflict and writes nothing until you resolve it (either by
    editing one config to match, or by passing --prefer to pick a winner).

Default run = dry-run "plan": prints what WOULD change and writes nothing.
Pass --apply to actually write (after backing up).

Run `python3 reconcile_mcp.py --help` for options (use `python` / `py -3` on Windows).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # TOML reading unavailable; Codex will be skipped with a warning.


# --------------------------------------------------------------------------------------
# Interpreter bootstrap — make `python3 reconcile_mcp.py` "just work" on an old python
# --------------------------------------------------------------------------------------
_PY3_MINOR = re.compile(r"^python3\.(\d+)$")


def _find_newest_python3() -> str | None:
    """Highest `python3.<minor>` with minor >= 11 found on PATH, or None.

    Chosen by FILENAME, not by executing anything: `python3.12` is 3.12 by universal
    convention across distros, Homebrew, and pyenv shims, so we never run a candidate (no
    hangs) and — crucially — never hardcode a version ceiling that goes stale as new
    Pythons ship. 3.11 is the only number in the code, and only because it's the floor
    `tomllib` requires.
    """
    best_minor, best_path, seen = -1, None, set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or d in seen:
            continue
        seen.add(d)
        try:
            entries = os.listdir(d)
        except OSError:
            continue  # unreadable / nonexistent PATH entry — skip it
        for name in entries:
            m = _PY3_MINOR.match(name)
            if not m:
                continue
            minor = int(m.group(1))
            full = os.path.join(d, name)
            if minor >= 11 and minor > best_minor and os.access(full, os.X_OK):
                best_minor, best_path = minor, full
    return best_path


def _reexec_under_modern_python() -> None:
    """Re-exec under a newer Python when launched on a pre-3.11 interpreter.

    Codex's config.toml is read with the stdlib `tomllib` (Python 3.11+). On macOS
    (Command Line Tools ships 3.9) and Ubuntu 22.04 (3.10), bare `python3` is often
    pre-3.11 — which would silently drop Codex from the sync. If we were launched on such
    an interpreter, find the newest `python3.x` on PATH and re-exec into it so Codex is
    still included. This keeps the documented `python3 reconcile_mcp.py` invocation
    correct without asking the caller to hunt for the right interpreter.

    No-op when already on 3.11+ or on Windows (Python is normally current there and
    os.execv is unreliable). Never loops (sentinel env var). If nothing newer is found we
    simply fall through and run: the JSON clients still sync and Codex is reported as
    skipped with an actionable message.
    """
    if sys.version_info >= (3, 11):
        return
    if os.name == "nt" or os.environ.get("ONE2RULE_BOOTSTRAPPED") == "1":
        return
    path = _find_newest_python3()
    if path:
        os.environ["ONE2RULE_BOOTSTRAPPED"] = "1"
        os.execv(path, [path, os.path.abspath(__file__), *sys.argv[1:]])


# --------------------------------------------------------------------------------------
# Path resolution (overridable for testing via --home / --appdata / --localappdata)
# --------------------------------------------------------------------------------------
_OVERRIDES: dict[str, str | None] = {}


def home() -> Path:
    return Path(_OVERRIDES.get("home") or Path.home())


def appdata() -> str | None:
    return _OVERRIDES.get("appdata") or os.environ.get("APPDATA")


def localappdata() -> str | None:
    return _OVERRIDES.get("localappdata") or os.environ.get("LOCALAPPDATA")


def _claude_desktop_paths() -> list[Path]:
    """Claude Desktop / Cowork stores its config per-OS and per-install-flavor."""
    out: list[Path] = []
    ad = appdata()
    if ad:  # Windows standard install
        out.append(Path(ad) / "Claude" / "claude_desktop_config.json")
    lad = localappdata()
    if lad:  # Windows MSIX (Store) install — package id varies, so glob it
        pattern = str(
            Path(lad) / "Packages" / "Claude_*" / "LocalCache" / "Roaming"
            / "Claude" / "claude_desktop_config.json"
        )
        out.extend(Path(p) for p in sorted(glob.glob(pattern)))
    # macOS and Linux locations so the tool also works off-Windows
    out.append(home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    out.append(home() / ".config" / "Claude" / "claude_desktop_config.json")
    return out


# Each client: a stable key, a human label, the file format, and how to find its file(s).
# quirk="antigravity" => stdio entries must carry an explicit type:"stdio".
CLIENTS: list[dict] = [
    dict(key="claude-desktop", label="Claude Desktop / Cowork", fmt="json", quirk=None,
         paths=_claude_desktop_paths),
    dict(key="claude-code", label="Claude Code (global)", fmt="json", quirk=None,
         paths=lambda: [home() / ".claude.json"]),
    dict(key="codex", label="Codex CLI", fmt="toml", quirk=None,
         paths=lambda: [home() / ".codex" / "config.toml"]),
    dict(key="gemini", label="Gemini CLI", fmt="json", quirk=None,
         paths=lambda: [home() / ".gemini" / "settings.json"]),
    dict(key="antigravity", label="Antigravity", fmt="json", quirk="antigravity",
         paths=lambda: [home() / ".gemini" / "config" / "mcp_config.json"]),
    dict(key="cursor", label="Cursor", fmt="json", quirk=None,
         paths=lambda: [home() / ".cursor" / "mcp.json"]),
    # Vendor-neutral .agents standard (dotagentsprotocol.com): ~/.agents/mcp.json, same
    # top-level `mcpServers` JSON schema. A first-class source AND destination, and is
    # created if missing (always_create) so the standard location always exists.
    dict(key="agents", label="Agents (.agents standard)", fmt="json", quirk=None,
         always_create=True, paths=lambda: [home() / ".agents" / "mcp.json"]),
]
CLIENT_KEYS = [c["key"] for c in CLIENTS]


# --------------------------------------------------------------------------------------
# Target = one concrete config file belonging to one client
# --------------------------------------------------------------------------------------
class Target:
    def __init__(self, client: dict, path: Path):
        self.client = client
        self.key = client["key"]
        self.label = client["label"]
        self.fmt = client["fmt"]
        self.quirk = client["quirk"]
        self.path = path
        self.exists = path.exists()
        self.always_create = client.get("always_create", False)  # materialize the file even if absent
        self.raw_text: str | None = None
        self.parsed: dict | None = None      # full parsed JSON object (json targets only)
        self.servers: dict[str, dict] = {}   # name -> normalized server entry
        self.error: str | None = None        # read-time error (servers not included in union)
        self.write_error: str | None = None  # apply-time skip (could not edit in place safely)

    @property
    def display(self) -> str:
        return f"{self.label} [{self.key}]"


def enumerate_targets(only: set[str] | None, exclude: set[str], create_missing: bool) -> list[Target]:
    targets: list[Target] = []
    for client in CLIENTS:
        if only and client["key"] not in only:
            continue
        if client["key"] in exclude:
            continue
        paths = client["paths"]()
        existing = [p for p in paths if p.exists()]
        for path in existing:
            targets.append(Target(client, path))
        # A client may list several candidate paths (e.g. Claude Desktop's Windows /
        # macOS / Linux locations). When creating from scratch, only materialize ONE —
        # the platform-default, which is the first candidate (env-specific paths that
        # don't apply on this OS resolve to nothing and aren't first).
        if (create_missing or client.get("always_create")) and not existing and paths:
            targets.append(Target(client, paths[0]))
    return targets


# --------------------------------------------------------------------------------------
# Normalization — make entries comparable across clients so we don't flag false conflicts
# --------------------------------------------------------------------------------------
def normalize_entry(entry):
    """Canonical, client-neutral form of a server definition.

    `type: "stdio"` is dropped because a command-based server is stdio by definition;
    Antigravity injects it, others omit it, and we don't want that to look like a real
    difference. Remote servers (type sse/http) keep their type since it is meaningful.
    """
    if not isinstance(entry, dict):
        return entry
    e = dict(entry)
    if e.get("type") == "stdio":
        e.pop("type", None)
    return e


def canon(entry) -> str:
    """Stable string for equality comparison of two normalized entries."""
    return json.dumps(entry, sort_keys=True, ensure_ascii=False)


def adapt_for_target(entry: dict, quirk: str | None) -> dict:
    """Turn a canonical entry into the exact shape a given client expects."""
    e = dict(entry)
    if quirk == "antigravity":
        is_remote = "url" in e
        if not is_remote and e.get("type") != "stdio":
            e = {"type": "stdio", **e}  # prepend, per Antigravity's documented schema
    return e


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------
def load_target(t: Target) -> None:
    if not t.exists:
        t.parsed = {} if t.fmt == "json" else None
        t.servers = {}
        return
    try:
        text = t.path.read_text(encoding="utf-8")
    except OSError as exc:
        t.error = f"could not read file: {exc}"
        return
    t.raw_text = text
    if t.fmt == "json":
        try:
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            t.error = f"invalid JSON ({exc}); skipping to avoid corrupting it"
            return
        if not isinstance(data, dict):
            t.error = "top-level JSON is not an object; skipping"
            return
        t.parsed = data
        raw = data.get("mcpServers") or {}
    else:  # toml
        if tomllib is None:
            t.error = ("Codex skipped: needs Python 3.11+ for the stdlib `tomllib` reader and "
                       "none was found on PATH. Install one (e.g. `brew install python@3.12`, "
                       "pyenv, or your distro's python3.12 package) and re-run to include Codex.")
            return
        try:
            data = tomllib.loads(text)
        except Exception as exc:  # tomllib.TOMLDecodeError and friends
            t.error = f"invalid TOML ({exc}); skipping to avoid corrupting it"
            return
        raw = data.get("mcp_servers") or {}
    if not isinstance(raw, dict):
        t.error = "mcpServers section is not a table/object; skipping"
        return
    t.servers = {name: normalize_entry(entry) for name, entry in raw.items()}


# --------------------------------------------------------------------------------------
# Union + conflict detection
# --------------------------------------------------------------------------------------
def build_union(targets: list[Target]):
    """Returns (sources, union, conflicts).

    sources:   name -> list[(client_key, entry)] across all readable targets
    union:     name -> entry  (only names with a single agreed definition)
    conflicts: name -> list[(canon_str, entry, [client_keys])]  (>1 distinct definition)
    """
    sources: dict[str, list[tuple[str, dict]]] = {}
    for t in targets:
        if t.error:
            continue
        for name, entry in t.servers.items():
            sources.setdefault(name, []).append((t.key, entry))

    union: dict[str, dict] = {}
    conflicts: dict[str, list] = {}
    for name, occ in sources.items():
        distinct: dict[str, list] = {}
        for ck, entry in occ:
            distinct.setdefault(canon(entry), [entry, []])[1].append(ck)
        if len(distinct) == 1:
            union[name] = next(iter(distinct.values()))[0]
        else:
            conflicts[name] = [(c, v[0], v[1]) for c, v in distinct.items()]
    return sources, union, conflicts


def resolve_conflicts(conflicts: dict[str, list], sources: dict, prefer: list[str]):
    """Use the --prefer priority list to pick a winner per conflicting name.

    Returns (resolved: name->entry, unresolved: name->list[(client_key, entry)]).
    """
    resolved: dict[str, dict] = {}
    unresolved: dict[str, list] = {}
    for name in conflicts:
        chosen = None
        for pk in prefer:
            for ck, entry in sources[name]:
                if ck == pk:
                    chosen = entry
                    break
            if chosen is not None:
                break
        if chosen is not None:
            resolved[name] = chosen
        else:
            unresolved[name] = sources[name]
    return resolved, unresolved


# --------------------------------------------------------------------------------------
# Per-target plan
# --------------------------------------------------------------------------------------
def plan_for_target(t: Target, final_map: dict[str, dict]):
    """Compare a target's current servers to the final union. Never deletes."""
    adds, changes, keeps = [], [], []
    for name, entry in final_map.items():
        desired = normalize_entry(adapt_for_target(entry, t.quirk))
        current = t.servers.get(name)
        if current is None:
            adds.append(name)
        elif canon(normalize_entry(current)) != canon(desired):
            changes.append(name)
        else:
            keeps.append(name)
    return sorted(adds), sorted(changes), keeps


# --------------------------------------------------------------------------------------
# TOML serialization (only the value shapes MCP configs actually use)
# --------------------------------------------------------------------------------------
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(k: str) -> str:
    if _BARE_KEY.match(k):
        return k
    return '"' + k.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_str(s: str) -> str:
    s = (s.replace("\\", "\\\\").replace('"', '\\"')
          .replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r"))
    return '"' + s + '"'


def _toml_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_val(x)}" for k, x in v.items()) + " }"
    if v is None:
        return '""'
    return _toml_str(str(v))


def _toml_table_name(line: str) -> str | None:
    """If `line` is a TOML table header, return its dotted name, else None."""
    s = line.strip()
    if not s.startswith("["):
        return None
    if s.startswith("[["):
        end = s.find("]]")
        return s[2:end].strip() if end != -1 else None
    end = s.find("]")
    return s[1:end].strip() if end != -1 else None


def _advance_toml_state(line: str, in_str: bool, delim: str, depth: int):
    """Update the cross-line TOML lexer state after one physical line.

    Returns (in_str, delim, depth): whether the line ends inside a multi-line string (and
    which `\"\"\"`/`'''` delimiter), plus the net `[`...`]` array depth carried to the next
    line. Brackets and quotes inside strings or after a `#` comment are ignored. The input is
    always valid TOML (load_target parsed it with tomllib before we ever write), so only
    well-formed constructs need handling. This lets _strip_mcp_tables tell a real table
    header from a `[`-leading line that is really an element of a multi-line array — the case
    that previously corrupted Codex configs."""
    i, n = 0, len(line)
    while i < n:
        if in_str:  # inside a multi-line string: only its closing delimiter matters
            idx = line.find(delim, i)
            if idx == -1:
                return True, delim, depth
            i = idx + len(delim)
            in_str, delim = False, ""
            continue
        c = line[i]
        if c == "#":  # comment runs to end of line
            break
        if line.startswith('"""', i) or line.startswith("'''", i):
            in_str, delim = True, line[i:i + 3]
            i += 3
            continue
        if c == '"' or c == "'":  # single-line string (basic strings honor backslash escapes)
            i += 1
            while i < n:
                if c == '"' and line[i] == "\\":
                    i += 2
                    continue
                if line[i] == c:
                    i += 1
                    break
                i += 1
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth = max(0, depth - 1)
        i += 1
    return in_str, delim, depth


def _strip_mcp_tables(text: str) -> str:
    """Remove every [mcp_servers] / [mcp_servers.*] table block, keep all else verbatim.

    A line is only treated as a table header when we're at TOP LEVEL — not inside a
    multi-line string or a multi-line array. That guard is what makes this safe: an array
    element or string line that happens to start with `[` inside an mcp block (e.g. a nested
    `["x"]` on its own line) can no longer be misread as a header, prematurely ending the
    block and leaking stray lines into the output."""
    out: list[str] = []
    in_mcp = False
    in_str = False
    delim = ""
    depth = 0
    for line in text.splitlines():
        if not in_str and depth == 0:  # only here can a line be a genuine table header
            name = _toml_table_name(line)
            if name is not None:
                in_mcp = name == "mcp_servers" or name.startswith("mcp_servers.")
                if not in_mcp:
                    out.append(line)
                continue  # header lines are self-contained in valid TOML; no state to advance
        in_str, delim, depth = _advance_toml_state(line, in_str, delim, depth)
        if not in_mcp:
            out.append(line)
    return "\n".join(out)


def _render_mcp_tables(final_map: dict[str, dict]) -> str:
    parts: list[str] = []
    for name, entry in final_map.items():
        parts.append(f"[mcp_servers.{_toml_key(name)}]")
        for k, val in entry.items():
            parts.append(f"{_toml_key(k)} = {_toml_val(val)}")
        parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


# --------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------
def backup_file(path: Path, stamp: str) -> Path:
    b = path.with_name(path.name + f".bak-{stamp}")
    shutil.copy2(path, b)
    return b


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically: write a temp file in the same directory, then
    os.replace it into place. A crash mid-write can then never leave the config truncated or
    half-written — the original stays intact until the final, atomic rename. (The caller has
    already taken a .bak backup as well.)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def ordered_final(t: Target, final_map: dict[str, dict]) -> dict[str, dict]:
    """Server map to write for this target: keep existing order, override each with the
    reconciled definition where we have one, but PRESERVE the target's own current entry for
    any name not in final_map — then append new names alphabetically.

    The preserve step is what keeps the merge additive under --skip-conflicts: a server whose
    name is an unresolved conflict is omitted from final_map, and rewriting the section from
    final_map alone would silently drop the target's existing copy. We never delete, so we
    carry that entry through unchanged. (On the normal path final_map covers every existing
    name, so this branch is a no-op.)"""
    existing = list(t.servers.keys())
    new = sorted(n for n in final_map if n not in t.servers)
    ordered = {}
    for n in existing:
        ordered[n] = final_map[n] if n in final_map else t.servers[n]
    for n in new:
        ordered[n] = final_map[n]
    return ordered


# --------------------------------------------------------------------------------------
# Surgical JSON member splice — edit only the mcpServers value, keep the rest byte-for-byte
# --------------------------------------------------------------------------------------
# ~/.claude.json holds far more than MCP servers (auth tokens, project history, UI state) and
# can be large, so re-serializing the whole file is needless risk. Instead we locate the
# top-level `mcpServers` value in the RAW text and replace just that span (or insert the key
# if absent), leaving every other byte untouched — the same "touch only the relevant section"
# approach the Codex TOML writer uses. write_json_target verifies the spliced text by
# re-parsing it before writing, and falls back to a full re-serialize if anything is off, so
# corrupt JSON can never be emitted.
def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _skip_string(s: str, i: int) -> int:
    """`s[i]` is the opening quote; return the index just past the closing quote."""
    i += 1
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    raise ValueError("unterminated string")


def _skip_value(s: str, i: int) -> int:
    """Return the index just past the JSON value beginning at `s[i]` (object/array matched
    with brace depth, strings skipped wholesale, scalars run to the next delimiter)."""
    c = s[i]
    if c == '"':
        return _skip_string(s, i)
    if c in "{[":
        depth = 0
        while i < len(s):
            c = s[i]
            if c == '"':
                i = _skip_string(s, i)
                continue
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise ValueError("unterminated container")
    j = i  # scalar: number / true / false / null
    while j < len(s) and s[j] not in ",}] \t\r\n":
        j += 1
    return j


def _find_top_level_member(text: str, root_brace: int, key: str):
    """Return (value_start, value_end) of the root object's member `key`, or None if absent.

    Each member value is skipped as a balanced unit, so a same-named key nested deeper (e.g.
    projects.<path>.mcpServers) is never matched. Raises ValueError on anything unexpected."""
    i = root_brace + 1
    while True:
        i = _skip_ws(text, i)
        if i >= len(text):
            raise ValueError("unterminated object")
        if text[i] == "}":
            return None
        if text[i] == ",":
            i = _skip_ws(text, i + 1)
        if i >= len(text) or text[i] != '"':
            raise ValueError("expected key")
        k_start = i
        k_end = _skip_string(text, i)
        k = json.loads(text[k_start:k_end])
        i = _skip_ws(text, k_end)
        if i >= len(text) or text[i] != ":":
            raise ValueError("expected ':'")
        i = _skip_ws(text, i + 1)
        v_start = i
        v_end = _skip_value(text, i)
        if k == key:
            return v_start, v_end
        i = v_end


def _detect_json_style(text: str, root_brace: int):
    """Infer (newline, indent_unit): CRLF vs LF, and the root members' leading-whitespace
    string ('' when the object is written compact on a single line)."""
    newline = "\r\n" if "\r\n" in text else "\n"
    j = _skip_ws(text, root_brace + 1)
    if j < len(text) and text[j] == "}":
        return newline, ""  # empty object — style unknown, default compact
    line_start = text.rfind("\n", 0, j) + 1
    indent = text[line_start:j]
    return (newline, indent) if (indent and indent.strip() == "") else (newline, "")


def _splice_json_member(text: str, key: str, value):
    """Replace (or insert) the TOP-LEVEL `key` value in valid JSON `text`, touching nothing
    else. Returns the new text, or None if the structure isn't what we expect (caller then
    falls back to a full re-serialize)."""
    i = _skip_ws(text, 0)
    if i >= len(text) or text[i] != "{":
        return None
    root_brace = i
    newline, unit = _detect_json_style(text, root_brace)
    if unit:  # indent the value to the member's level, matching the file's newline style
        body = json.dumps(value, indent=unit, ensure_ascii=False).replace("\n", newline + unit)
    else:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    try:
        span = _find_top_level_member(text, root_brace, key)
    except ValueError:
        return None
    if span is not None:
        v_start, v_end = span
        return text[:v_start] + body + text[v_end:]
    # key absent — insert it as the first member of the root object
    after = _skip_ws(text, root_brace + 1)
    head = text[:root_brace + 1]
    if after < len(text) and text[after] == "}":  # empty root object
        if unit:
            return head + newline + unit + json.dumps(key) + ": " + body + newline + text[after:]
        return head + json.dumps(key) + ":" + body + text[after:]
    if unit:
        return head + newline + unit + json.dumps(key) + ": " + body + "," + text[root_brace + 1:]
    return head + json.dumps(key) + ":" + body + "," + text[root_brace + 1:]


def render_json_target(t: Target, final_map: dict[str, dict]) -> str | None:
    """Return the exact text to write for a JSON target, or None if an EXISTING file can't be
    edited surgically.

    For an existing file we splice only the `mcpServers` value and verify by re-parsing. If
    that verification fails we return None — the caller then leaves the file untouched rather
    than reformatting it (we never full-rewrite a file that already exists, so a large,
    sensitive `~/.claude.json` is never reflowed). A not-yet-existing file has nothing to
    preserve, so it is built fresh."""
    servers = {n: adapt_for_target(e, t.quirk) for n, e in ordered_final(t, final_map).items()}
    if t.raw_text is None:  # new file: nothing to preserve, build it from scratch
        data = dict(t.parsed) if isinstance(t.parsed, dict) else {}
        data["mcpServers"] = servers
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    spliced = _splice_json_member(t.raw_text, "mcpServers", servers)
    if spliced is not None:
        expected = dict(t.parsed) if isinstance(t.parsed, dict) else {}
        expected["mcpServers"] = servers
        try:
            if json.loads(spliced) == expected:
                return spliced
        except json.JSONDecodeError:
            pass
    return None  # surgical edit couldn't be verified -> caller skips this file (no rewrite)


def write_toml_target(t: Target, final_map: dict[str, dict]) -> None:
    body = _strip_mcp_tables(t.raw_text) if t.raw_text else ""
    block = _render_mcp_tables(ordered_final(t, final_map))
    new = body.rstrip("\n")
    new = (new + "\n\n" if new else "") + block
    if not new.endswith("\n"):
        new += "\n"
    _atomic_write_text(t.path, new)


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------
def fmt_list(names: list[str], limit: int = 12) -> str:
    if not names:
        return "(none)"
    shown = names[:limit]
    extra = len(names) - len(shown)
    s = ", ".join(shown)
    return s + (f", ...(+{extra} more)" if extra else "")


def print_report(targets, sources, union, conflicts, resolved, unresolved,
                 final_map, plans, applied, stamp, skip_conflicts=False):
    print("=" * 78)
    print("MCP SERVER RECONCILIATION " + ("(APPLIED)" if applied else "(PLAN - no files written)"))
    print("=" * 78)

    readable = [t for t in targets if not t.error and (t.exists or t.always_create)]
    missing = [t for t in targets if not t.exists and not t.always_create]
    errored = [t for t in targets if t.error]

    print(f"\nClients found: {len(readable)}    Unique servers (union): {len(final_map)}")
    if conflicts:
        print(f"Conflicts: {len(conflicts)}  (resolved {len(resolved)}, UNRESOLVED {len(unresolved)})")

    if errored:
        print("\n!! Skipped (could not read — their servers were NOT included):")
        for t in errored:
            print(f"   - {t.display}: {t.error}\n     {t.path}")

    if missing:
        print("\nNot installed / no config file (skipped):")
        for t in missing:
            print(f"   - {t.display}: {t.path}")

    # Coverage: which clients are missing each server
    if readable and final_map:
        print("\nServer coverage (clients currently missing each server):")
        for name in sorted(final_map):
            have = {ck for ck, _ in sources.get(name, [])}
            missing_in = [t.label for t in readable if t.key not in have]
            # also count targets that have it but with a stale (to-be-changed) def
            status = "ALL" if not missing_in else f"missing in: {fmt_list([m for m in dict.fromkeys(missing_in)])}"
            print(f"   - {name}: {status}")

    print("\nPer-client plan:")
    for t in readable:
        adds, changes, keeps = plans[t.key]
        print(f"\n  {t.display}")
        print(f"    {t.path}")
        print(f"    + add ({len(adds)}): {fmt_list(adds)}")
        print(f"    ~ change ({len(changes)}): {fmt_list(changes)}")
        print(f"    = unchanged: {len(keeps)}")

    write_skipped = [t for t in targets if t.write_error]
    if write_skipped:
        print("\n" + "!" * 78)
        print("COULD NOT WRITE — left untouched (no file was reformatted, nothing lost):")
        for t in write_skipped:
            print(f"   - {t.display}: {t.write_error}\n     {t.path}")
        print("These clients did NOT receive the update. Re-run to retry, or report the file.")
        print("!" * 78)

    if unresolved:
        clients_in = sorted({ck for occ in unresolved.values() for ck, _ in occ})
        bar = "!" * 78
        print("\n" + bar)
        if applied:  # only reachable under --skip-conflicts
            print(f"SKIPPED CONFLICTS — {len(unresolved)} server name(s) differ across clients.")
            print("Synced everything else; these were left untouched (nothing overwritten).")
        else:
            print(f"BLOCKING CONFLICTS — {len(unresolved)} server name(s) differ across clients.")
            print("With --skip-conflicts, --apply syncs everything EXCEPT these."
                  if skip_conflicts else "Nothing was written.")
        print("Resolve each by either:")
        print(f"  - re-running with --prefer <client[,client...]> to pick a winner "
              f"(e.g. --prefer {clients_in[0]})")
        print(f"      clients in these conflicts: {', '.join(clients_in)}")
        print("  - editing one config so the definitions match, then re-running")
        if not skip_conflicts:
            print("  - or --skip-conflicts to sync everything else now and resolve these later")
        print(bar)
        for name, occ in unresolved.items():
            print(f"\n  Conflict on '{name}':")
            seen = {}
            for ck, entry in occ:
                seen.setdefault(canon(entry), []).append(ck)
            for c, cks in seen.items():
                print(f"    [{', '.join(cks)}]")
                for ln in json.dumps(json.loads(c), indent=6, ensure_ascii=False).splitlines():
                    print("    " + ln)

    if applied:
        if unresolved:
            print(f"\nApplied the non-conflicting servers; {len(unresolved)} conflict(s) skipped "
                  f"(above). Backups created with suffix .bak-{stamp}")
        else:
            print("\nWrote changes. Backups created with suffix .bak-" + stamp)
    elif not unresolved:
        any_change = any(plans[t.key][0] or plans[t.key][1] for t in readable)
        if any_change:
            print("\nThis was a dry run. Re-run with --apply to write these changes (backups will be made).")
        else:
            print("\nEverything is already in sync - nothing to do.")
    else:  # dry run with unresolved conflicts
        if skip_conflicts:
            print("\nThis was a dry run. Re-run with --apply --skip-conflicts to write the "
                  "non-conflicting servers (the conflicts above stay untouched).")
        else:
            print("\nThis was a dry run. Resolve the conflicts above (or add --skip-conflicts), "
                  "then re-run with --apply.")
    print()


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reconcile MCP servers across all client configs.")
    ap.add_argument("--apply", action="store_true",
                    help="Write changes (default is a dry-run plan). Backs up every file first.")
    ap.add_argument("--prefer", default="",
                    help="Comma-separated client priority order to auto-resolve conflicts "
                         f"(keys: {','.join(CLIENT_KEYS)}).")
    ap.add_argument("--only", default="", help="Comma-separated client keys to include (default: all).")
    ap.add_argument("--exclude", default="", help="Comma-separated client keys to skip.")
    ap.add_argument("--create-missing", action="store_true",
                    help="Also create config files for clients that don't have one yet.")
    ap.add_argument("--skip-conflicts", action="store_true",
                    help="Instead of blocking the whole run on a conflict, sync the "
                         "non-conflicting servers and leave conflicting names untouched. "
                         "Conflicts are still reported and the exit code stays 2 so you "
                         "know to resolve them.")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="Emit a machine-readable JSON summary instead of the text report.")
    # Testing / advanced path overrides
    ap.add_argument("--home", help="Override the home directory (testing).")
    ap.add_argument("--appdata", help="Override %%APPDATA%% (testing).")
    ap.add_argument("--localappdata", help="Override %%LOCALAPPDATA%% (testing).")
    args = ap.parse_args(argv)

    try:  # keep unicode in server names/values from crashing a legacy console
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    if args.home:
        _OVERRIDES["home"] = args.home
    if args.appdata:
        _OVERRIDES["appdata"] = args.appdata
    if args.localappdata:
        _OVERRIDES["localappdata"] = args.localappdata

    only = {k.strip() for k in args.only.split(",") if k.strip()} or None
    exclude = {k.strip() for k in args.exclude.split(",") if k.strip()}
    prefer = [k.strip() for k in args.prefer.split(",") if k.strip()]
    for k in (only or set()) | exclude | set(prefer):
        if k not in CLIENT_KEYS:
            ap.error(f"unknown client key '{k}'. Valid: {', '.join(CLIENT_KEYS)}")

    # Microseconds make the stamp unique per run, so two applies in the same second can't
    # clobber each other's backups (.bak-<stamp> is otherwise per-second).
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")

    targets = enumerate_targets(only, exclude, args.create_missing)
    for t in targets:
        load_target(t)

    sources, union, conflicts = build_union(targets)
    resolved, unresolved = resolve_conflicts(conflicts, sources, prefer) if conflicts else ({}, {})

    final_map: dict[str, dict] = dict(union)
    final_map.update(resolved)

    readable = [t for t in targets if not t.error and (t.exists or args.create_missing or t.always_create)]
    plans = {t.key: plan_for_target(t, final_map) for t in readable}

    applied = False
    # Conflicting names are already excluded from final_map (union + resolved only), so a
    # --skip-conflicts apply simply writes the non-conflicting set and leaves conflicts alone.
    if args.apply and (args.skip_conflicts or not unresolved):
        for t in readable:
            adds, changes, _ = plans[t.key]
            if not adds and not changes:
                continue
            if t.fmt == "json":
                # Build (and verify) the surgically-edited text first; if it can't be done
                # safely on an existing file, skip it — never back up or rewrite it.
                text = render_json_target(t, final_map)
                if text is None:
                    t.write_error = ("could not edit in place safely (surgical splice failed "
                                     "verification); left untouched to avoid reformatting it")
                    continue
                if t.exists:
                    backup_file(t.path, stamp)
                _atomic_write_text(t.path, text)
            else:
                if t.exists:
                    backup_file(t.path, stamp)
                write_toml_target(t, final_map)
        applied = True

    if args.as_json:
        print(json.dumps({
            "applied": applied,
            "stamp": stamp,
            "union": sorted(final_map),
            "conflicts": {n: [{"clients": cks, "entry": e} for _, e, cks in v]
                          for n, v in conflicts.items()},
            "unresolved": sorted(unresolved),
            "plan": {t.key: {"path": str(t.path), "add": plans[t.key][0],
                             "change": plans[t.key][1], "unchanged": len(plans[t.key][2])}
                     for t in readable},
            "skipped_errors": {t.key: t.error for t in targets if t.error},
            "write_skipped": {t.key: t.write_error for t in targets if t.write_error},
            "not_installed": [t.key for t in targets if not t.exists],
        }, indent=2, ensure_ascii=False))
    else:
        print_report(targets, sources, union, conflicts, resolved, unresolved,
                     final_map, plans, applied, stamp, args.skip_conflicts)

    if unresolved:
        return 2
    if any(t.error or t.write_error for t in targets):
        return 3  # partial: some files couldn't be read, or couldn't be edited in place
    return 0


if __name__ == "__main__":
    _reexec_under_modern_python()
    sys.exit(main())
