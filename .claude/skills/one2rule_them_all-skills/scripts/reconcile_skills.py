#!/usr/bin/env python3
"""
reconcile_skills.py — Reconcile Agent Skills across every skill-capable tool on the machine.

Sibling of reconcile_mcp.py, but skills are a different shape of problem. An MCP server is
a tiny JSON/TOML object; a *skill* is a whole directory tree — a `SKILL.md` (YAML
frontmatter + body) plus optional `scripts/`, `references/`, `assets/`, `agents/openai.yaml`,
etc. So this tool reconciles DIRECTORIES, not config keys.

Each tool keeps its skills in its own root directory (Claude `~/.claude/skills`, the Gemini CLI
and Antigravity (CLI `agy` & IDE) both at `~/.gemini/skills`, the `.agents` standard
`~/.agents/skills` — which is also where Codex reads user skills — and Cursor `~/.cursor/skills`).
All of them use the SAME on-disk format: `<root>/<name>/SKILL.md`.
Over time the sets drift — a skill you authored for Claude never shows up in Codex, etc.
This tool reads every root that exists, computes the UNION of skills, and (on --apply) copies
each skill into every participating tool that's missing it — without ever deleting a skill
and without touching anything outside the skill directories it manages.

Cursor is a READ-ONLY source: per Cursor's docs it natively loads skills from the other tools'
folders (~/.claude/skills) plus ~/.agents/skills and its own ~/.cursor/skills, so once those are
synced it already sees the union. We read its native ~/.cursor/skills so Cursor-authored skills
propagate out, but never write to any Cursor folder.

Design guarantees (mirrors reconcile_mcp.py):
  * No skill is ever deleted. Every tool ends with at least the skills it started with.
  * Tool-managed namespaces are left alone: any entry whose name starts with "." (e.g. the
    reserved `.system/` skills installed by the tool itself, Cursor's `.sync-manifest.json`)
    is never read, written, or counted.
  * A directory only counts as a skill if it contains a `SKILL.md`.
  * Every skill directory that would be overwritten is backed up first, to a dot-prefixed
    `<root>/.skill-backups/<stamp>/<name>/` (invisible to discovery).
  * Same skill NAME with genuinely different CONTENT in two tools => the tool refuses to
    guess: it reports the conflict and writes nothing until you resolve it (edit one to
    match, or pass --prefer to pick a winner).

Frontmatter quirk handling (the "little quirks" skills have):
  * Skills are copied BYTE-FOR-BYTE. The only thing this tool ever rewrites is the exact
    `description:` value, and only when it exceeds a target tool's limit. When that happens
    it WARNS and shortens the description to fit, changing as little as possible (it trims
    at a sentence boundary, then a word boundary; fences, line-endings, other frontmatter
    keys, and the body are left untouched). Malformed frontmatter (e.g. a `\\---` fence, or
    CRLF with blank-line-separated keys) is preserved as-is; if the description can't be
    located safely it is never rewritten — only flagged.
  * A skill and its auto-shortened copy are recognized as THE SAME skill (the description
    value is excluded from identity), so re-running is idempotent and never invents a
    conflict between a full description and the trimmed copy the tool itself wrote.

Default run = dry-run "plan": prints what WOULD change and writes nothing.
Pass --apply to actually write (after backing up).

Run `python3 reconcile_skills.py --help` for options (use `python` / `py -3` on Windows).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from datetime import datetime
from pathlib import Path

# Default frontmatter limits. name<=64 is authoritative (skill-creator). The "max 200"
# figure some docs cite for description is not what tools actually enforce — skills with
# 200-900 char descriptions load fine — so the real ceiling used here is 1024 (Anthropic's
# documented max). Override globally with --max-desc / --max-name.
DEFAULT_DESC_LIMIT = 1024
DEFAULT_NAME_LIMIT = 64

# Transient files we never propagate (noise, not skill content).
_NOISE_NAMES = {".DS_Store", "Thumbs.db"}
_NOISE_DIRS = {"__pycache__", ".git"}


# --------------------------------------------------------------------------------------
# Path resolution (overridable for testing via --home)
# --------------------------------------------------------------------------------------
_OVERRIDES: dict[str, str | None] = {}


def home() -> Path:
    return Path(_OVERRIDES.get("home") or Path.home())


# Each tool: a stable key, a human label, whether it participates by default, and how to
# find its skills root(s). A tool may list several candidate roots (Antigravity); every
# root that exists is reconciled independently, and a skill in any of them counts.
TOOLS: list[dict] = [
    dict(key="claude", label="Claude Code / Desktop", default=True,
         roots=lambda: [home() / ".claude" / "skills"]),
    # The Gemini CLI AND Antigravity (both the agy CLI and the IDE) read the SAME directory,
    # ~/.gemini/skills (verified by test on-machine). One physical root => one entry. The old
    # ~/.gemini/antigravity-cli/skills is the agy-CLI-only slash-command staging dir, NOT what
    # the IDE reads, so it is no longer used. `antigravity` is accepted as an alias for this key.
    dict(key="gemini", label="Gemini CLI & Antigravity (CLI & IDE)", default=True,
         roots=lambda: [home() / ".gemini" / "skills"]),
    # The vendor-neutral .agents standard (dotagentsprotocol.com): ~/.agents/skills is the
    # global "agent-compatible" skills dir read by Codex (its USER skill scope, per OpenAI's
    # docs), Antigravity, Cursor, OpenCode, and others — NOT ~/.codex/skills, which Codex does
    # not read. It is a first-class source AND destination, created if missing (always_create).
    dict(key="agents", label="Agents (.agents standard)", default=True, always_create=True,
         roots=lambda: [home() / ".agents" / "skills"]),
    # Cursor is a READ-ONLY source. Per Cursor's docs it natively loads skills from the other
    # tools' folders for compatibility (~/.claude/skills) plus ~/.agents/skills (which Codex
    # also reads) and its own ~/.cursor/skills — so once we sync those, Cursor sees the full union and
    # needs nothing written into it. We still READ its native ~/.cursor/skills so skills you author
    # directly in Cursor propagate OUT to the other tools. (Note: ~/.cursor/skills-cursor is a
    # third-party sync tool's folder that Cursor does not read, so it is intentionally NOT used.)
    dict(key="cursor", label="Cursor (read-only source)", default=True, source_only=True,
         roots=lambda: [home() / ".cursor" / "skills"]),
]
TOOL_KEYS = [t["key"] for t in TOOLS]
TOOL_BY_KEY = {t["key"]: t for t in TOOLS}

# `antigravity` used to be its own key/root; Antigravity (CLI & IDE) now shares ~/.gemini/skills
# with the Gemini CLI, so it's folded into the `gemini` entry. Likewise `codex`: per OpenAI's docs
# Codex reads user skills from ~/.agents/skills (the .agents standard) — the same dir as the
# `agents` entry — not ~/.codex/skills, so it's folded into `agents`. Both old keys are accepted
# as aliases so existing --only / --exclude / --include / --prefer invocations keep working.
KEY_ALIASES = {"antigravity": "gemini", "codex": "agents"}


def canon_key(key: str) -> str:
    """Map a user-supplied tool key through any alias to its canonical TOOL key."""
    return KEY_ALIASES.get(key, key)


# --------------------------------------------------------------------------------------
# Reading a skill into a comparable form
# --------------------------------------------------------------------------------------
def decode(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


def read_skill_files(skill_dir: Path) -> dict[str, bytes]:
    """All files under a skill dir as {posix_relpath: bytes}, skipping transient noise."""
    files: dict[str, bytes] = {}
    for p in sorted(skill_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(skill_dir)
        parts = set(rel.parts)
        if parts & _NOISE_DIRS:
            continue
        if p.name in _NOISE_NAMES or p.suffix == ".pyc":
            continue
        files[rel.as_posix()] = p.read_bytes()
    return files


def skill_md_key(file_map: dict[str, bytes]) -> str | None:
    """Locate the SKILL.md within a skill's file map (case-insensitive, top level)."""
    for rel in file_map:
        if "/" not in rel and rel.lower() == "skill.md":
            return rel
    return None


def discover_skills(root: Path) -> dict[str, dict[str, bytes]]:
    """name -> file_map for every immediate subdir holding a SKILL.md. Skips dot-dirs."""
    out: dict[str, dict[str, bytes]] = {}
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        fm = read_skill_files(child)
        if skill_md_key(fm) is None:
            continue  # a directory without a SKILL.md is not a skill (e.g. scratch/workspace dirs)
        out[child.name] = fm
    return out


# --------------------------------------------------------------------------------------
# Frontmatter: extract / normalize / minimally rewrite the description value
# --------------------------------------------------------------------------------------
_DESC_KEY = re.compile(r"(?im)^([ \t]*)description[ \t]*:[ \t]*")
_NAME_KEY = re.compile(r"(?im)^([ \t]*)name[ \t]*:[ \t]*")
_BLOCK_INDICATORS = {">", ">-", ">+", "|", "|-", "|+"}


def _normalize_desc(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return re.sub(r"\s+", " ", s).strip()


def extract_description(text: str):
    """Find the SKILL.md description.

    Returns (normalized_desc, value_start, value_end): the cleaned single-line description
    and the [start, end) span of its RAW value in `text` (everything after the colon through
    the end of the value, folded continuation lines included). Returns (None, None, None) if
    no description can be confidently located — callers then leave the file untouched.
    """
    m = _DESC_KEY.search(text)
    if not m:
        return None, None, None
    key_indent = len(m.group(1))
    s = m.end()
    # End of the first line of the value.
    nl = text.find("\n", s)
    first_line_end = len(text) if nl == -1 else nl  # exclude the newline char itself
    first_line = text[s:first_line_end]
    indicator = first_line.strip().rstrip("\r")

    if indicator in _BLOCK_INDICATORS:
        # Folded/literal block: value is the indented lines that follow.
        pos = first_line_end + (0 if nl == -1 else 1)
        last_content_end = first_line_end  # empty block if no continuation
        collected: list[str] = []
        while pos < len(text):
            le = text.find("\n", pos)
            line_full = text[pos:len(text)] if le == -1 else text[pos:le]
            content = line_full.rstrip("\r")
            if content.strip() == "":
                pos = len(text) if le == -1 else le + 1
                continue
            indent = len(content) - len(content.lstrip(" "))
            if indent > key_indent:
                collected.append(content.strip())
                last_content_end = pos + len(content)
                pos = len(text) if le == -1 else le + 1
            else:
                break
        desc = _normalize_desc(" ".join(collected))
        return desc, s, last_content_end

    # Inline scalar (quoted or plain) — the common case; treat as a single line.
    desc = _normalize_desc(first_line)
    return desc, s, first_line_end


def extract_name_fm(text: str) -> str | None:
    m = _NAME_KEY.search(text)
    if not m:
        return None
    nl = text.find("\n", m.end())
    line = text[m.end():(len(text) if nl == -1 else nl)]
    return _normalize_desc(line) or None


def shorten_description(desc: str, limit: int) -> str:
    """Trim a description to <= limit chars, changing as little as possible.

    Retains as much of the leading text as fits (cut at the last whole-word boundary), and
    snaps back to a sentence end only when one sits within a few chars of that cut — so we
    never sacrifice most of the budget just to land on a clean sentence. The leading text,
    which carries the skill's trigger intent, is what survives.
    """
    desc = desc.strip()
    if len(desc) <= limit:
        return desc
    cut = desc.rfind(" ", 0, limit + 1)
    if cut < 24:  # pathological single long token — hard cut
        cut = limit
    seg = desc[:cut]
    sentence_ends = [mm.start() + 1 for mm in re.finditer(r"[.!?](?=\s|$)", seg)]
    if sentence_ends and cut - sentence_ends[-1] <= 12:
        cut = sentence_ends[-1]
    return desc[:cut].rstrip().rstrip(",;:—-(").rstrip()


def _yaml_dq(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def description_blanked(text: str) -> str:
    """SKILL.md text with the description value replaced by a fixed placeholder.

    Used for identity comparison so two skills that differ ONLY in their description (e.g. a
    full copy vs. an auto-shortened copy) are still recognized as the same skill.
    """
    _, s, e = extract_description(text)
    if s is None:
        return text
    return text[:s] + "\x00DESC\x00" + text[e:]


# --------------------------------------------------------------------------------------
# Adapt a canonical skill to a specific tool (frontmatter limits)
# --------------------------------------------------------------------------------------
def adapt_skill(file_map: dict[str, bytes], tool: dict, name: str,
                desc_limit: int, name_limit: int):
    """Return (adapted_file_map, warnings) for writing `name` into `tool`.

    Byte-for-byte identical to the source EXCEPT the description value may be shortened to
    fit the tool's limit. SKILL.md bytes are passed through untouched unless a rewrite
    actually happens, so fidelity is perfect in the common case.
    """
    warnings: list[str] = []
    adapted = dict(file_map)
    if len(name) > name_limit:
        warnings.append(
            f"skill '{name}': name is {len(name)} chars (> {name_limit}); left unchanged "
            f"for {tool['label']} — renaming a skill directory is unsafe (breaks references)."
        )
    smk = skill_md_key(file_map)
    if smk is None:
        return adapted, warnings
    text = decode(file_map[smk])
    desc, s, e = extract_description(text)
    if desc is not None and len(desc) > desc_limit:
        if s is not None:
            short = shorten_description(desc, desc_limit)
            new_text = text[:s] + _yaml_dq(short) + text[e:]
            adapted[smk] = new_text.encode("utf-8")
            warnings.append(
                f"skill '{name}': description shortened for {tool['label']} "
                f"({len(desc)} -> {len(short)} chars; limit {desc_limit})."
            )
        else:
            warnings.append(
                f"skill '{name}': description is {len(desc)} chars (> {desc_limit}) for "
                f"{tool['label']} but could not be located safely; left as-is — shorten by hand."
            )
    return adapted, warnings


# --------------------------------------------------------------------------------------
# Fingerprints
# --------------------------------------------------------------------------------------
def fingerprint(file_map: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for rel in sorted(file_map):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(file_map[rel]).digest())
    return h.hexdigest()


def identity(file_map: dict[str, bytes]) -> str:
    """Fingerprint that ignores the description value (the one field we may rewrite)."""
    m = dict(file_map)
    smk = skill_md_key(file_map)
    if smk is not None:
        m[smk] = description_blanked(decode(file_map[smk])).encode("utf-8")
    return fingerprint(m)


def description_of(file_map: dict[str, bytes]) -> str | None:
    smk = skill_md_key(file_map)
    if smk is None:
        return None
    desc, _, _ = extract_description(decode(file_map[smk]))
    return desc


# --------------------------------------------------------------------------------------
# Target = one concrete skills root belonging to one tool
# --------------------------------------------------------------------------------------
class Target:
    def __init__(self, tool: dict, root: Path, desc_limit: int, name_limit: int):
        self.tool = tool
        self.key = tool["key"]
        self.label = tool["label"]
        self.root = root
        self.exists = root.is_dir()
        self.desc_limit = desc_limit
        self.name_limit = name_limit
        self.skills: dict[str, dict[str, bytes]] = {}
        self.source_only = bool(tool.get("source_only"))  # read its skills, but never write to it
        self.always_create = bool(tool.get("always_create"))  # materialize the root even if absent
        self.error: str | None = None

    @property
    def display(self) -> str:
        return f"{self.label} [{self.key}]"


def enumerate_targets(participating: list[str], create_missing: bool,
                      desc_limit: int, name_limit: int) -> list[Target]:
    targets: list[Target] = []
    for key in participating:
        tool = TOOL_BY_KEY[key]
        roots = tool["roots"]()
        existing = [r for r in roots if r.is_dir()]
        for root in existing:
            targets.append(Target(tool, root, desc_limit, name_limit))
        if (create_missing or tool.get("always_create")) and not existing and roots:
            targets.append(Target(tool, roots[0], desc_limit, name_limit))
    return targets


def load_target(t: Target) -> None:
    if not t.exists:
        t.skills = {}
        return
    try:
        t.skills = discover_skills(t.root)
    except OSError as exc:
        t.error = f"could not read skills root: {exc}"


# --------------------------------------------------------------------------------------
# Union + conflict detection
# --------------------------------------------------------------------------------------
def build_union(targets: list[Target], only_skill: set[str] | None, skip_skill: set[str]):
    """Returns (sources, union, conflicts).

    sources:   name -> list[(target_key, file_map)]
    union:     name -> canonical file_map (consistent across all tools that have it)
    conflicts: name -> {"kind": "body"|"description", "variants": [(file_map, [keys])]}
    """
    sources: dict[str, list[tuple[str, dict]]] = {}
    for t in targets:
        if t.error:
            continue
        for name, fm in t.skills.items():
            if only_skill is not None and name not in only_skill:
                continue
            if name in skip_skill:
                continue
            sources.setdefault(name, []).append((t.key, fm))

    union: dict[str, dict] = {}
    conflicts: dict[str, dict] = {}
    for name, occ in sources.items():
        # Group by body identity (everything except the description value).
        by_body: dict[str, list[tuple[str, dict]]] = {}
        for key, fm in occ:
            by_body.setdefault(identity(fm), []).append((key, fm))
        if len(by_body) > 1:
            conflicts[name] = {
                "kind": "body",
                "variants": [(grp[0][1], [k for k, _ in grp]) for grp in by_body.values()],
            }
            continue
        # Same body everywhere. Check the description is consistent (full vs trimmed copy ok).
        canonical = max(occ, key=lambda kv: len(description_of(kv[1]) or ""))[1]
        d_star = description_of(canonical)
        consistent = True
        for key, fm in occ:
            di = description_of(fm)
            limit = next((t.desc_limit for t in targets if t.key == key), DEFAULT_DESC_LIMIT)
            expected = shorten_description(d_star, limit) if d_star is not None else None
            if di != expected:
                consistent = False
                break
        if consistent:
            union[name] = canonical
        else:
            # Distinct descriptions that aren't explained by trimming -> genuine divergence.
            by_desc: dict[str, list[str]] = {}
            for key, fm in occ:
                by_desc.setdefault(description_of(fm) or "", []).append(key)
            conflicts[name] = {
                "kind": "description",
                "variants": [(dict(occ_for(occ, keys[0])), keys) for d, keys in by_desc.items()],
                "descs": by_desc,
            }
    return sources, union, conflicts


def occ_for(occ, key):
    for k, fm in occ:
        if k == key:
            return fm
    return {}


def resolve_conflicts(conflicts: dict[str, dict], sources: dict, prefer: list[str]):
    """Use --prefer priority order to pick a winning tool's version per conflicting name."""
    resolved: dict[str, dict] = {}
    unresolved: dict[str, dict] = {}
    for name, info in conflicts.items():
        chosen = None
        for pk in prefer:
            for ck, fm in sources[name]:
                if ck == pk:
                    chosen = fm
                    break
            if chosen is not None:
                break
        if chosen is not None:
            resolved[name] = chosen
        else:
            unresolved[name] = info
    return resolved, unresolved


# --------------------------------------------------------------------------------------
# Per-target plan
# --------------------------------------------------------------------------------------
def plan_for_target(t: Target, final_map: dict[str, dict]):
    """Compare a target's current skills to the final union. Never deletes.

    Source-only targets (e.g. Cursor) are never written, so they get an empty write plan even
    though their own skills still feed the union.
    """
    adds, changes, keeps, warnings = [], [], [], []
    if t.source_only:
        return adds, changes, sorted(t.skills), warnings
    for name, fm in final_map.items():
        adapted, warns = adapt_skill(fm, t.tool, name, t.desc_limit, t.name_limit)
        warnings.extend(warns)
        desired = fingerprint(adapted)
        current = t.skills.get(name)
        if current is None:
            adds.append(name)
        elif fingerprint(current) != desired:
            changes.append(name)
        else:
            keeps.append(name)
    return sorted(adds), sorted(changes), keeps, warnings


# --------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------
def _on_rm_error(func, path, _exc):
    """Make a read-only file writable and retry (Windows)."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _rmtree(path: Path) -> None:
    """shutil.rmtree with the read-only retry, using onexc on 3.12+ and onerror below it."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=lambda func, p, exc: _on_rm_error(func, p, exc))
    else:
        shutil.rmtree(path, onerror=_on_rm_error)


def write_skill(dest: Path, file_map: dict[str, bytes]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for rel, data in file_map.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def write_skill_atomic(dest: Path, file_map: dict[str, bytes]) -> None:
    """Write a skill directory so a mid-write crash can never leave `dest` deleted or
    half-written.

    The naive approach (rmtree(dest) then write_skill(dest)) leaves the live skill gone for
    the entire duration of a recursive multi-file copy. Instead we build the new tree in a
    dot-prefixed temp dir (invisible to discover_skills), then swap it in with fast renames:
    move the old dir aside, move the new one into place, delete the old one. The only
    vulnerable window is the instant between two near-instant renames — and even then the
    .skill-backups copy the caller already took is a complete recovery point.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    # Dot-prefixed names are skipped by discover_skills(), so a temp dir left behind by a
    # crash is never mistaken for a real skill.
    tmp_new = dest.with_name(f".one2rule-tmp-new-{dest.name}-{pid}")
    if tmp_new.exists():
        _rmtree(tmp_new)
    write_skill(tmp_new, file_map)
    if dest.exists():
        tmp_old = dest.with_name(f".one2rule-tmp-old-{dest.name}-{pid}")
        if tmp_old.exists():
            _rmtree(tmp_old)
        os.replace(dest, tmp_old)        # move the live dir aside (dest now free)
        try:
            os.replace(tmp_new, dest)    # swap the new tree into place
        finally:
            if tmp_old.exists():
                _rmtree(tmp_old)
    else:
        os.replace(tmp_new, dest)


def backup_skill(dest: Path, root: Path, stamp: str, name: str) -> Path:
    bdir = root / ".skill-backups" / stamp / name
    bdir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(dest, bdir)
    return bdir


def apply_to_target(t: Target, final_map: dict[str, dict], adds: list[str],
                    changes: list[str], stamp: str) -> None:
    for name in adds + changes:
        adapted, _ = adapt_skill(final_map[name], t.tool, name, t.desc_limit, t.name_limit)
        dest = t.root / name
        if dest.exists():
            backup_skill(dest, t.root, stamp, name)
        write_skill_atomic(dest, adapted)


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------
def fmt_list(names: list[str], limit: int = 12) -> str:
    if not names:
        return "(none)"
    shown = names[:limit]
    extra = len(names) - len(shown)
    return ", ".join(shown) + (f", ...(+{extra} more)" if extra else "")


def print_report(targets, sources, union, conflicts, resolved, unresolved,
                 final_map, plans, applied, stamp, all_warnings, skip_conflicts=False):
    print("=" * 78)
    print("SKILL RECONCILIATION " + ("(APPLIED)" if applied else "(PLAN - no files written)"))
    print("=" * 78)

    readable = [t for t in targets if not t.error and (t.exists or t.always_create)]
    missing = [t for t in targets if not t.exists and not t.always_create]
    errored = [t for t in targets if t.error]

    print(f"\nTools found: {len(readable)}    Unique skills (union): {len(final_map)}")
    if conflicts:
        print(f"Conflicts: {len(conflicts)}  (resolved {len(resolved)}, UNRESOLVED {len(unresolved)})")

    if errored:
        print("\n!! Skipped (could not read — their skills were NOT included):")
        for t in errored:
            print(f"   - {t.display}: {t.error}\n     {t.root}")

    if missing:
        print("\nNo skills root (not installed / not created — skipped):")
        for t in missing:
            print(f"   - {t.display}: {t.root}")

    if readable and final_map:
        print("\nSkill coverage (tools currently missing each skill):")
        for name in sorted(final_map):
            have = {ck for ck, _ in sources.get(name, [])}
            missing_in = [t.label for t in readable if t.key not in have]
            status = "ALL" if not missing_in else f"missing in: {fmt_list(list(dict.fromkeys(missing_in)))}"
            print(f"   - {name}: {status}")

    print("\nPer-tool plan:")
    for t in readable:
        adds, changes, keeps, _ = plans[t.key]
        print(f"\n  {t.display}")
        print(f"    {t.root}")
        if t.source_only:
            print(f"    ○ read-only source: contributes {len(keeps)} skill(s) to the union; "
                  f"never written to (Cursor reads the other tools' folders natively).")
            continue
        print(f"    + add ({len(adds)}): {fmt_list(adds)}")
        print(f"    ~ change ({len(changes)}): {fmt_list(changes)}")
        print(f"    = unchanged: {len(keeps)}")

    if all_warnings:
        print("\n" + "-" * 78)
        print("WARNINGS (frontmatter adapted / flagged):")
        for w in all_warnings:
            print(f"   ! {w}")

    if unresolved:
        tools_in = sorted({k for nm in unresolved for k, _ in sources.get(nm, [])})
        bar = "!" * 78
        print("\n" + bar)
        if applied:  # only reachable under --skip-conflicts
            print(f"SKIPPED CONFLICTS — {len(unresolved)} skill name(s) differ across tools.")
            print("Synced everything else; these were left untouched (nothing overwritten).")
        else:
            print(f"BLOCKING CONFLICTS — {len(unresolved)} skill name(s) differ across tools.")
            print("With --skip-conflicts, --apply syncs everything EXCEPT these."
                  if skip_conflicts else "Nothing was written.")
        print("Resolve each by either:")
        suggestion = tools_in[0] if tools_in else TOOL_KEYS[0]
        print(f"  - re-running with --prefer <tool[,tool...]> to pick a winner "
              f"(e.g. --prefer {suggestion})")
        if tools_in:
            print(f"      tools in these conflicts: {', '.join(tools_in)}")
        print("  - editing one copy so the content matches, then re-running")
        if not skip_conflicts:
            print("  - or --skip-conflicts to sync everything else now and resolve these later")
        print(bar)
        for name, info in unresolved.items():
            kind = info["kind"]
            print(f"\n  Conflict on '{name}' ({kind} differs):")
            if kind == "description":
                for desc, keys in info["descs"].items():
                    print(f"    [{', '.join(keys)}] description ({len(desc)} chars):")
                    print(f"      {desc[:200]}{'...' if len(desc) > 200 else ''}")
            else:
                for fm, keys in info["variants"]:
                    files = ", ".join(sorted(fm)[:8])
                    print(f"    [{', '.join(keys)}] {len(fm)} files: {files}"
                          f"{' ...' if len(fm) > 8 else ''}")

    if applied:
        if unresolved:
            print(f"\nApplied the non-conflicting skills; {len(unresolved)} conflict(s) skipped "
                  f"(above). Overwritten skills were backed up under each root's "
                  f".skill-backups/{stamp}/")
        else:
            print(f"\nWrote changes. Overwritten skills were backed up under each root's "
                  f".skill-backups/{stamp}/")
    elif not unresolved:
        any_change = any(plans[t.key][0] or plans[t.key][1] for t in readable)
        if any_change:
            print("\nThis was a dry run. Re-run with --apply to write these changes "
                  "(backups will be made).")
        else:
            print("\nEverything is already in sync - nothing to do.")
    else:  # dry run with unresolved conflicts
        if skip_conflicts:
            print("\nThis was a dry run. Re-run with --apply --skip-conflicts to write the "
                  "non-conflicting skills (the conflicts above stay untouched).")
        else:
            print("\nThis was a dry run. Resolve the conflicts above (or add --skip-conflicts), "
                  "then re-run with --apply.")
    print()


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reconcile Agent Skills across all tool skill roots.")
    ap.add_argument("--apply", action="store_true",
                    help="Write changes (default is a dry-run plan). Backs up overwritten skills first.")
    ap.add_argument("--prefer", default="",
                    help=f"Comma-separated tool priority order to auto-resolve conflicts (keys: {','.join(TOOL_KEYS)}).")
    ap.add_argument("--only", default="",
                    help="Comma-separated tool keys to include (default: all installed tools).")
    ap.add_argument("--exclude", default="", help="Comma-separated tool keys to skip (e.g. cursor).")
    ap.add_argument("--include", default="",
                    help="Comma-separated tool keys to pull into the run even if excluded by default.")
    ap.add_argument("--only-skill", default="", help="Comma-separated skill names to restrict the sync to.")
    ap.add_argument("--skip-skill", default="", help="Comma-separated skill names to leave out of the sync.")
    ap.add_argument("--create-missing", action="store_true",
                    help="Also create skills roots for participating tools that don't have one yet.")
    ap.add_argument("--skip-conflicts", action="store_true",
                    help="Instead of blocking the whole run on a conflict, sync the "
                         "non-conflicting skills and leave conflicting names untouched. "
                         "Conflicts are still reported and the exit code stays 2 so you "
                         "know to resolve them.")
    ap.add_argument("--max-desc", type=int, default=DEFAULT_DESC_LIMIT,
                    help=f"Description character limit before it gets shortened (default {DEFAULT_DESC_LIMIT}).")
    ap.add_argument("--max-name", type=int, default=DEFAULT_NAME_LIMIT,
                    help=f"Skill-name character limit to warn at (default {DEFAULT_NAME_LIMIT}).")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="Emit a machine-readable JSON summary instead of the text report.")
    ap.add_argument("--home", help="Override the home directory (testing).")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    if args.home:
        _OVERRIDES["home"] = args.home

    only = {canon_key(k.strip()) for k in args.only.split(",") if k.strip()} or None
    exclude = {canon_key(k.strip()) for k in args.exclude.split(",") if k.strip()}
    include = {canon_key(k.strip()) for k in args.include.split(",") if k.strip()}
    prefer = [canon_key(k.strip()) for k in args.prefer.split(",") if k.strip()]
    for k in (only or set()) | exclude | include | set(prefer):
        if k not in TOOL_KEYS:
            aliases = ", ".join(f"{a}->{c}" for a, c in KEY_ALIASES.items())
            ap.error(f"unknown tool key '{k}'. Valid: {', '.join(TOOL_KEYS)} (aliases: {aliases})")

    only_skill = {s.strip() for s in args.only_skill.split(",") if s.strip()} or None
    skip_skill = {s.strip() for s in args.skip_skill.split(",") if s.strip()}

    base = set(only) if only else {t["key"] for t in TOOLS if t["default"]}
    base |= include
    base -= exclude
    participating = [t["key"] for t in TOOLS if t["key"] in base]
    if not participating:
        ap.error("no tools selected to reconcile.")

    # Microseconds make the stamp unique per run, so two applies in the same second can't
    # collide in .skill-backups/<stamp>/ (copytree would otherwise raise FileExistsError).
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")

    targets = enumerate_targets(participating, args.create_missing, args.max_desc, args.max_name)
    for t in targets:
        load_target(t)

    sources, union, conflicts = build_union(targets, only_skill, skip_skill)
    resolved, unresolved = resolve_conflicts(conflicts, sources, prefer) if conflicts else ({}, {})

    final_map: dict[str, dict] = dict(union)
    final_map.update(resolved)

    readable = [t for t in targets if not t.error and (t.exists or args.create_missing or t.always_create)]
    plans = {t.key: plan_for_target(t, final_map) for t in readable}
    all_warnings = sorted({w for t in readable for w in plans[t.key][3]})

    applied = False
    # Conflicting names are already excluded from final_map (union + resolved only), so a
    # --skip-conflicts apply simply writes the non-conflicting set and leaves conflicts alone.
    if args.apply and (args.skip_conflicts or not unresolved):
        for t in readable:
            if t.source_only:
                continue  # read-only source (Cursor): never written to
            adds, changes, _, _ = plans[t.key]
            if not adds and not changes:
                continue
            t.root.mkdir(parents=True, exist_ok=True)
            apply_to_target(t, final_map, adds, changes, stamp)
        applied = True

    if args.as_json:
        print(json.dumps({
            "applied": applied,
            "stamp": stamp,
            "union": sorted(final_map),
            "conflicts": {n: {"kind": v["kind"],
                              "tools": sorted({k for _, ks in v["variants"] for k in ks})}
                          for n, v in conflicts.items()},
            "unresolved": sorted(unresolved),
            "warnings": all_warnings,
            "plan": {t.key: {"root": str(t.root), "source_only": t.source_only,
                             "add": plans[t.key][0], "change": plans[t.key][1],
                             "unchanged": len(plans[t.key][2])}
                     for t in readable},
            "skipped_errors": {t.key: t.error for t in targets if t.error},
            "no_root": [t.key for t in targets if not t.exists],
        }, indent=2, ensure_ascii=False))
    else:
        print_report(targets, sources, union, conflicts, resolved, unresolved,
                     final_map, plans, applied, stamp, all_warnings, args.skip_conflicts)

    if unresolved:
        return 2
    if any(t.error for t in targets):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
