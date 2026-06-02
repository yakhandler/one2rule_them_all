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
    for name in ("python3.13", "python3.12", "python3.11"):
        path = shutil.which(name)
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
        self.error: str | None = None

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


def _strip_mcp_tables(text: str) -> str:
    """Remove every [mcp_servers] / [mcp_servers.*] table block, keep all else verbatim."""
    out: list[str] = []
    in_mcp = False
    for line in text.splitlines():
        name = _toml_table_name(line)
        if name is not None:
            in_mcp = name == "mcp_servers" or name.startswith("mcp_servers.")
            if in_mcp:
                continue
        if in_mcp:
            continue
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
    """Keep the target's existing server order, append new names alphabetically."""
    existing = list(t.servers.keys())
    new = sorted(n for n in final_map if n not in t.servers)
    ordered = {}
    for n in existing + new:
        if n in final_map:
            ordered[n] = final_map[n]
    return ordered


def write_json_target(t: Target, final_map: dict[str, dict]) -> None:
    data = dict(t.parsed) if isinstance(t.parsed, dict) else {}
    servers = {n: adapt_for_target(e, t.quirk) for n, e in ordered_final(t, final_map).items()}
    data["mcpServers"] = servers
    _atomic_write_text(t.path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


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
                 final_map, plans, applied, stamp):
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

    if unresolved:
        print("\n" + "!" * 78)
        print("BLOCKING CONFLICTS — same server name, different definitions.")
        print("Nothing was written. Resolve by editing one config to match, or re-run")
        print("with --prefer <client[,client...]> to choose a winner. Client keys:")
        print("  " + ", ".join(CLIENT_KEYS))
        print("!" * 78)
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
        print("\nWrote changes. Backups created with suffix .bak-" + stamp)
    elif not unresolved:
        any_change = any(plans[t.key][0] or plans[t.key][1] for t in readable)
        if any_change:
            print("\nThis was a dry run. Re-run with --apply to write these changes (backups will be made).")
        else:
            print("\nEverything is already in sync - nothing to do.")
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
    if args.apply and not unresolved:
        for t in readable:
            adds, changes, _ = plans[t.key]
            if not adds and not changes:
                continue
            if t.exists:
                backup_file(t.path, stamp)
            if t.fmt == "json":
                write_json_target(t, final_map)
            else:
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
            "not_installed": [t.key for t in targets if not t.exists],
        }, indent=2, ensure_ascii=False))
    else:
        print_report(targets, sources, union, conflicts, resolved, unresolved,
                     final_map, plans, applied, stamp)

    if unresolved:
        return 2
    if any(t.error for t in targets):
        return 3  # partial: ran, but some files were unreadable
    return 0


if __name__ == "__main__":
    _reexec_under_modern_python()
    sys.exit(main())
