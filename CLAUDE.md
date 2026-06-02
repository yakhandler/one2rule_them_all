# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A source-of-truth collection of **Agent Skills** (not an app). It ships two sibling skills under [.claude/skills/](.claude/skills/) that keep AI tooling from drifting out of sync by reconciling the **union** of definitions back into every tool on the machine:

- [one2rule_them_all-mcps](.claude/skills/one2rule_them_all-mcps/) — syncs **MCP server definitions** across MCP clients.
- [one2rule_them_all-skills](.claude/skills/one2rule_them_all-skills/) — syncs **Agent Skills** (whole directories) across skill-capable tools.

`INSTALL.ps1` / `INSTALL.sh` copy these skills into the user's `~/.claude/skills`. Important consequence: the skills that *this* Claude Code session loads come from the installed copies under `~/.claude/skills`, **not** from this repo's working tree. Editing a `SKILL.md` here does not change triggering behavior until you reinstall.

## Architecture (the part that spans files)

Both skills follow the **same three-part shape**, and understanding one transfers to the other:

1. `SKILL.md` — thin agent-facing instructions. The agent's job is only to *run the script, interpret its output, and handle conflicts* — never to hand-edit the target configs/skill folders.
2. `scripts/reconcile_*.py` — a **self-contained, stdlib-only Python engine** that does all real work. No shared library between the two; they are deliberately independent siblings.
3. `references/*-paths.md` — documents every destination path and the per-tool format quirks the engine relies on.

Both engines implement the **same design contract** (this is the core invariant set — preserve it in any change):

- **Dry-run by default; `--apply` writes.** Every plan is shown before anything is written.
- **Additive — never *removes*.** No server/skill is dropped; each tool ends with at least what it had. It is *not* strictly non-mutating, and two paths deliberately change existing content (both backed up first): `--prefer` overwrites the losing side of a conflict, and changed mcps files are re-serialized (JSON whitespace and Codex *in-block* TOML comments are lost; everything outside the `mcp_servers` block is kept verbatim). Skills copy byte-for-byte, no such loss.
- **Back up before overwrite, then write atomically.** mcps writes `<file>.bak-<stamp>` then swaps the new config in via a temp file + `os.replace` (`_atomic_write_text`); skills copy the old dir to `<root>/.skill-backups/<stamp>/` then swap the new tree in via a dot-prefixed temp dir + rename (`write_skill_atomic`), never an in-place `rmtree`-then-write. The `<stamp>` carries microseconds so rapid re-runs can't collide. An interrupted run therefore can't leave a half-written config or a deleted-but-not-rewritten skill.
- **Compute the UNION** across only the tools/clients whose config actually exists on the machine.
- **Conflicts block (exit code 2).** Same name + different definition/content → the engine refuses to guess, prints both (with a ready-to-use `--prefer` suggestion + the clients/tools involved), writes nothing. Resolve via `--prefer <keys>` (priority list picks a winner), by editing one side to match, or with `--skip-conflicts` (opt-in) to sync everything *except* the conflicting names and leave each side's own copy untouched — still exit 2 so the divergence isn't forgotten. Exit codes: `0` ok, `2` unresolved conflicts (even if `--skip-conflicts` applied the rest), `3` ran but some files were unreadable. Note: `--skip-conflicts` relies on `ordered_final` (mcps) preserving a target's own entry for any name absent from `final_map`, so the section rewrite stays additive; skills are safe inherently (per-directory writes).
- **Touch only the relevant section.** Everything else in each file/namespace is preserved.

Where the two engines **diverge** (because the problems differ):

- **mcps** reconciles *config-file entries*. Most clients are JSON (`mcpServers`), Codex is TOML (`mcp_servers`, surgically rewritten via stdlib `tomllib`). Quirk: Antigravity records an explicit `type:"stdio"` that others omit — the engine normalizes it away for comparison and re-adds it only when writing Antigravity. Project-scoped servers inside `~/.claude.json` are intentionally left alone. Client keys: `claude-desktop, claude-code, codex, gemini, antigravity, cursor, agents`.
- **skills** reconciles *whole directory trees* (copied byte-for-byte). All tools share the same `SKILL.md` directory format, so there is **no cross-format conversion**. Key ideas to know before editing [reconcile_skills.py](.claude/skills/one2rule_them_all-skills/scripts/reconcile_skills.py):
  - **Identity = the tree minus the `description:` value.** This is why a skill and the auto-shortened copy the tool itself wrote are *not* a false conflict — re-running must stay idempotent.
  - **Over-limit frontmatter is warned + minimally rewritten** (description trimmed at a word/sentence boundary, default limit 1024; only the description *value* substring is ever touched, fences/CRLF/other keys preserved). A `name` over 64 chars is flagged only, never renamed.
  - **Discovery skips dot-prefixed entries** (the tool-managed `.system/` namespace, `.skill-backups/`, Cursor's `.sync-manifest.json`) and requires a `SKILL.md` (so `*-workspace` scratch dirs are ignored).
  - **Cursor is a read-only source** (`source_only=True` on its TOOL entry). Per [Cursor's docs](https://cursor.com/docs/skills) Cursor natively loads skills from `~/.claude/skills`, `~/.codex/skills`, `~/.agents/skills`, and its own `~/.cursor/skills` — so once the union is synced across the other three, Cursor already sees everything; nothing is written into any Cursor folder. The engine reads Cursor's **native** `~/.cursor/skills` so Cursor-authored skills propagate out, never writes to it, and ignores `~/.cursor/skills-cursor` (a third-party sync tool's folder, with a `.sync-manifest.json`, that Cursor does not read). Claude/Codex/Antigravity built-ins live in the dot-prefixed `.system/` dir, skipped by discovery. Tool keys: `claude, codex, gemini, agents, cursor` (with `antigravity` accepted as an alias for `gemini`, since Antigravity's CLI & IDE both read `~/.gemini/skills` — the same dir as the Gemini CLI — so they're a single entry).
  - **The `.agents` standard** ([dotagentsprotocol.com](https://dotagentsprotocol.com/)) is a first-class read+write entry, auto-created via the per-entry `always_create=True` flag (honored in `enumerate_targets` + the `readable` filter, independent of `--create-missing`). skills → `~/.agents/skills` (the `agents` tool key; it was split out of the old Antigravity entry). Antigravity itself (CLI `agy` **and** IDE) reads `~/.gemini/skills` — the same dir as the Gemini CLI (verified by test) — so the two are one entry (key `gemini`, alias `antigravity`); the legacy `~/.gemini/antigravity-cli/skills` is the agy-CLI-only slash-command dir and is no longer used as a skills root. mcps → `~/.agents/mcp.json` (same `mcpServers` schema). Both engines share this `always_create` mechanism.
  - **`agents/openai.yaml`** (Codex/Antigravity's *optional* interface sidecar — display name, icon, example prompt, implicit-invocation policy) is copied **verbatim when present and never fabricated**. It is optional (a skill runs in Codex without it) and belongs in the source skill, so generating it per-target would hurt quality and break the identical-everywhere/idempotent guarantee. This is a deliberate decision, not a gap. There is no SKILL.md format conversion of any kind.

## Commands

There is no build/lint step and no package. Work is: run an engine, read its plan, apply.

Invoke with `python3` on macOS/Linux (where there's usually no bare `python`), `python` or
`py -3` on Windows. The MCP engine needs Python 3.11+ for Codex (stdlib `tomllib`) and
auto-re-execs into a newer `python3.x` if the default is older; the skills engine runs on
3.9+. The examples below use `python3`.

```bash
# Plan (dry-run) and apply — MCP servers
python3 .claude/skills/one2rule_them_all-mcps/scripts/reconcile_mcp.py
python3 .claude/skills/one2rule_them_all-mcps/scripts/reconcile_mcp.py --apply [--prefer cursor,claude-code]

# Plan (dry-run) and apply — Agent Skills
python3 .claude/skills/one2rule_them_all-skills/scripts/reconcile_skills.py
python3 .claude/skills/one2rule_them_all-skills/scripts/reconcile_skills.py --apply [--prefer claude] [--include cursor]

# Machine-readable output (useful for reasoning about a plan programmatically)
python3 <engine> --json
```

Common flags: `--only`/`--exclude` (limit clients/tools), `--create-missing` (seed configs/roots that don't exist), `--json`. skills adds `--include`, `--only-skill`/`--skip-skill`, `--max-desc`/`--max-name`.

### Testing (smoke tests against a fake home)

Tests are sandbox-based, not pytest, and there is **no committed fixtures harness** — build a throwaway tree of fake skill/config roots under a temp dir, point the engine at it, then dry-run → `--apply` → dry-run again and confirm "already in sync" (idempotency).

**Isolate fully — this bites.** `--home` only redirects `~`. The mcps engine *also* resolves Claude Desktop via `%APPDATA%`/`%LOCALAPPDATA%`, so a run that overrides only `--home` will READ your real Claude Desktop config and, under `--apply`, WRITE to it. For mcps always pass all three (`--home <sb>/home --appdata <sb>/appdata --localappdata <sb>/localappdata`); skills only needs `--home`. Never `--apply` an un-isolated run, and never print a generated `mcp.json` — real configs hold live secrets.

```bash
# skills — a fake home is enough; author a minimal <sb>/home/.claude/skills/demo/SKILL.md, then:
python3 .claude/skills/one2rule_them_all-skills/scripts/reconcile_skills.py --home <sb>/home          # plan
python3 .claude/skills/one2rule_them_all-skills/scripts/reconcile_skills.py --home <sb>/home --apply  # then re-run: expect "already in sync"

# mcps — MUST also sandbox appdata/localappdata or it touches your real Claude Desktop config
python3 .claude/skills/one2rule_them_all-mcps/scripts/reconcile_mcp.py --home <sb>/home --appdata <sb>/appdata --localappdata <sb>/localappdata
```

Throwaway sandboxes plus `.skill-backups/` and `*.bak-*` are gitignored.

## Conventions & gotchas

- **Python 3.11+ for the MCP engine** (stdlib `tomllib`, for Codex); the skills engine runs on **3.9+**. The MCP engine auto-re-execs into the newest `python3.x` on PATH when launched on an older interpreter (`_reexec_under_modern_python`). Stdlib only — do not add dependencies; the engines must run anywhere Python does.
- **Windows-first** (paths use `Path.home()` honoring `USERPROFILE`; rmtree has a read-only retry). Keep cross-platform: read/write files as **bytes** in the skills engine so byte round-trips and CRLF survive.
- **Real-world frontmatter is messy** — at least one installed skill has a malformed `\---` fence with CRLF. Parse frontmatter defensively (for display/identity/limit checks only); never normalize or "fix" it on copy.
- The original research tables claiming per-tool formats (Cursor JSON rule-maps, Codex `.toml`, Antigravity `AGENTS.md` for *skills*) are **wrong** — on disk every tool uses the same SKILL.md directory format. The verified reality lives in each skill's `references/` doc and the raw notes in [.research/](.research/).
- Known open work is tracked in [README.md](README.md) ("To Do") — notably: verify the Cursor skills sync path, and add macOS/Linux destination tables to both `references/` docs (currently Windows-centric).
