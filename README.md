# one2rule_them_all

Two [Claude Code](https://docs.claude.com/en/docs/claude-code) Agent Skills that stop your AI tooling from drifting out of sync. Add an MCP server in Cursor, or write a skill in Claude, and these reconcile the **union** back into every tool on your machine — in each tool's native format, additively, with backups, never deleting anything.

## The skills

| Skill                        | Syncs                            | Across                                                                               |
| ---------------------------- | -------------------------------- | ------------------------------------------------------------------------------------ |
| **one2rule_them_all-mcps**   | MCP server definitions           | Claude Desktop/Code, Codex, Gemini CLI, Antigravity, Cursor, `.agents` standard      |
| **one2rule_them_all-skills** | Agent Skills (whole directories) | Claude, Codex, Antigravity, Gemini, `.agents` standard (Cursor reads these natively) |

Each computes the union of everything across the tools you actually have installed and writes that complete set back into each one — converting formats where needed for MCP configs (e.g. Codex's TOML), copying skill directories verbatim, and leaving the rest of every file untouched. Skills need no format conversion since every tool uses the same `SKILL.md` directory layout; the one per-tool extra, Codex/Antigravity's optional `agents/openai.yaml` interface file, is copied along when present but never fabricated, since it's non-critical metadata a skill runs fine without.

Both also sync the vendor-neutral [`.agents` standard](https://dotagentsprotocol.com/) — `~/.agents/skills` and `~/.agents/mcp.json` — as a first-class source and destination, and **create those paths if missing** so the standard location always exists. (Cursor is a read-only source: it natively reads the other tools' folders, so the union reaches it without anything being written into a Cursor folder.)

## Install

Copies both skills into your Claude Code user skills folder (`~/.claude/skills`):

```powershell
.\INSTALL.ps1      # Windows
```

```bash
bash INSTALL.sh    # macOS / Linux
```

Then, in any tool that has the skill, just ask — e.g. _"sync my MCP servers across all my tools"_ or _"get all my skills into every tool"_ — and the matching skill triggers automatically.

## How it runs

Each skill drives a Python script under its `scripts/` folder. It **always plans before it writes** (the agent does this for you):

```bash
python3 <skill_dir>/scripts/reconcile_mcp.py            # dry-run plan
python3 <skill_dir>/scripts/reconcile_mcp.py --apply    # apply changes
```

Use `python3` on macOS/Linux; on Windows use `python` (or `py -3`). Only the MCP engine needs Python 3.11+ (for Codex), and it auto-finds a newer `python3.x` if your default is older; the skills engine runs on 3.9+ (see Requirements).

## Safety model

The merge is **additive with a backup** — honest framing matters here, so: it is _not_ a guaranteed no-op rewrite, but nothing you have is ever lost.

- **Nothing is ever removed.** No server or skill is dropped from any tool; every tool ends with at least what it had. `--only-skill` / `--skip-skill` / `--exclude` only narrow what gets _synced in_ — they never prune what's already there.
- **Conflicts block.** Same name, different definition/content in two tools → it stops (exit code 2), prints both, and writes nothing until you resolve it (`--prefer` to pick a winner, or edit one side to match).
- **Backed up before every overwrite.** MCP configs to `<file>.bak-<stamp>`; skills to `<root>/.skill-backups/<stamp>/<name>/`. The stamp carries microseconds, so two runs in the same second can't clobber each other's backups.
- **Writes are atomic.** Each config/skill is built at a temporary path and swapped into place with a rename, so an interrupted run (Ctrl-C, crash, power loss) can't leave a half-written config or a deleted-but-not-rewritten skill — the original stays intact until the final atomic step.
- **Scoped.** Global configs only; project-local configs and tool-managed `.system/` namespaces are left alone.

Two cases **do** deliberately change existing content (both backed up first, so they're reversible):

- **`--prefer` overwrites the losing side.** Resolving a conflict by preference replaces the other tools' copy with the winner's.
- **Only the MCP section is rewritten** (MCP only). Both JSON and TOML configs are edited _surgically_: only the `mcpServers` value (JSON) / `[mcp_servers.*]` tables (TOML) are replaced, and everything else in the file — including a huge, sensitive `~/.claude.json` (auth tokens, project history, UI state) — is kept **byte-for-byte**. The replaced block itself is re-serialized to match the file's own indentation, and for Codex's TOML comments _inside_ the block are dropped. (JSON edits are verified by re-parsing before writing; on the rare chance that check fails, that file is left **untouched** and reported — never reformatted or corrupted — and the run exits non-zero.) The `.bak` holds the exact original. Skills are copied byte-for-byte.

### Restoring from a backup

Every overwrite leaves a timestamped backup. To roll back, replace the live file/folder with its backup:

```powershell
# Windows — MCP config (the .bak sits next to the original)
Copy-Item "$HOME\.claude.json.bak-<stamp>" "$HOME\.claude.json" -Force

# Windows — one skill (originals live under the root's .skill-backups/<stamp>/)
$root = "$HOME\.claude\skills"
Remove-Item "$root\myskill" -Recurse -Force
Copy-Item "$root\.skill-backups\<stamp>\myskill" "$root\myskill" -Recurse
```

```bash
# macOS / Linux — MCP config
cp ~/.codex/config.toml.bak-<stamp> ~/.codex/config.toml

# macOS / Linux — one skill
root=~/.claude/skills
rm -rf "$root/myskill" && cp -r "$root/.skill-backups/<stamp>/myskill" "$root/myskill"
```

Backups are never pruned automatically — delete old `.bak-*` files and `.skill-backups/` folders yourself when you no longer need them.

## Requirements

- **Python 3.11+ for the MCP engine only** — `reconcile_mcp.py` uses the stdlib `tomllib`
  (added in 3.11) to read Codex's `config.toml`. On an older Python the JSON clients still
  sync, but **Codex is skipped** (with a message telling you how to fix it). The skills
  engine (`reconcile_skills.py`) has no such dependency and runs on **Python 3.9+**.
- Heads-up for macOS/Linux: the _system_ Python is often too old, and there may be no bare
  `python` at all. macOS Command Line Tools ships 3.9 (and only as `python3`); Ubuntu 22.04
  LTS ships 3.10. For Codex support, install a current Python — e.g. `brew install python@3.12`,
  `pyenv`, or your distro's `python3.12` package. You don't have to invoke it as bare
  `python3`: the MCP engine auto-re-execs into the newest `python3.x` it finds on PATH.

## Layout

```
.claude/skills/
  one2rule_them_all-mcps/    # MCP server sync  (SKILL.md + scripts + references)
  one2rule_them_all-skills/  # Agent Skill sync (SKILL.md + scripts + references)
INSTALL.ps1 / INSTALL.sh     # install the skills to ~/.claude/skills
```

## Known Issues

### 1. `~/.agents/` is materialized on apply whether or not you use the `.agents` standard

**When:** first run · **Type:** expected behavior, surprising

- Both engines flag `agents` with `always_create=True`, honored independently of
  `--create-missing`. First `--apply` with a non-empty union creates
  `~/.agents/mcp.json` and `~/.agents/skills/` even if the user never uses dotagents.

**Possible fix:** gate `always_create` behind a flag, or call it out in the plan output
before writing.

### 2. Some destination paths are the repo's own admitted unknowns

**When:** first run · **Type:** unverified assumption

- README "To Do" and CLAUDE.md flag that historical path/format research was partly wrong
  and that the Cursor skills path and Antigravity's `~/.gemini/...` sharing aren't fully
  verified.
- Failure mode is **ineffective, not destructive**: servers/skills written to a dir the
  tool doesn't actually read.

**Possible fix:** verify on a real machine that each target tool _sees_ a newly-synced
server/skill; add macOS/Linux destination tables to both `references/` docs.

### 3. A running Claude Code can clobber the surgical `~/.claude.json` edit

**When:** apply while Claude Code is running · **Type:** race condition

- The MCP engine now edits `~/.claude.json` surgically (only the `mcpServers` value, rest
  byte-for-byte) and writes atomically — so it can't corrupt or reformat the file. But
  Claude Code keeps its own in-memory copy of that config and flushes it on state changes
  and on exit. If it flushes after the engine's write, it **overwrites the synced
  `mcpServers`** (the change silently doesn't "stick"); the reverse ordering is fine.
- This is a "change may not take effect," not a data-loss or corruption issue — nothing is
  deleted, and the `.bak` holds the pre-edit file regardless.

**Possible fix (operational for now):** apply when Claude Code isn't the live writer, or
restart it afterward so it reloads from disk. A code-level fix would need to detect a
running instance and warn/defer, which the engine doesn't currently do.

---

### Minor notes

- **Run `bash INSTALL.sh`, not `sh INSTALL.sh`** — `set -u` + `${BASH_SOURCE[0]}` errors
  under dash. README says `bash`; just don't deviate.
- **`INSTALL.sh` doesn't back up** skills it overwrites (`rm -rf` then copy), unlike the
  engines. Only matters if a prior install of this repo exists; still inconsistent with
  the project's own "back up before overwrite" guarantee.
- **Memory:** `reconcile_skills.py` reads every file of every skill into RAM as bytes to
  fingerprint. Fine for dozens of skills, but not streaming; large assets add up.
