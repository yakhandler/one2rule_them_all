# one2rule_them_all

Two [Claude Code](https://docs.claude.com/en/docs/claude-code) Agent Skills that stop your AI tooling from drifting out of sync. Add an MCP server in Cursor, or write a skill in Claude, and these reconcile the **union** back into every tool on your machine — in each tool's native format, additively, with backups, never deleting anything.

## The skills

| Skill                        | Syncs                            | Across                                                           |
| ---------------------------- | -------------------------------- | ---------------------------------------------------------------- |
| **one2rule_them_all-mcps**   | MCP server definitions           | Claude Desktop/Code, Codex, Gemini CLI, Antigravity, Cursor, `.agents` standard |
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
python <skill_dir>/scripts/reconcile_mcp.py            # dry-run plan
python <skill_dir>/scripts/reconcile_mcp.py --apply    # apply changes
```

## Safety model

The merge is **additive with a backup** — honest framing matters here, so: it is *not* a guaranteed no-op rewrite, but nothing you have is ever lost.

- **Nothing is ever removed.** No server or skill is dropped from any tool; every tool ends with at least what it had. `--only-skill` / `--skip-skill` / `--exclude` only narrow what gets *synced in* — they never prune what's already there.
- **Conflicts block.** Same name, different definition/content in two tools → it stops (exit code 2), prints both, and writes nothing until you resolve it (`--prefer` to pick a winner, or edit one side to match).
- **Backed up before every overwrite.** MCP configs to `<file>.bak-<stamp>`; skills to `<root>/.skill-backups/<stamp>/<name>/`. The stamp carries microseconds, so two runs in the same second can't clobber each other's backups.
- **Writes are atomic.** Each config/skill is built at a temporary path and swapped into place with a rename, so an interrupted run (Ctrl-C, crash, power loss) can't leave a half-written config or a deleted-but-not-rewritten skill — the original stays intact until the final atomic step.
- **Scoped.** Global configs only; project-local configs and tool-managed `.system/` namespaces are left alone.

Two cases **do** deliberately change existing content (both backed up first, so they're reversible):

- **`--prefer` overwrites the losing side.** Resolving a conflict by preference replaces the other tools' copy with the winner's.
- **Re-serialization drops formatting** (MCP only). Changed JSON is re-emitted with 2-space indent (original whitespace gone); for Codex's TOML, comments *inside* the `[mcp_servers.*]` block are dropped while everything outside it is kept verbatim. The `.bak` holds the exact original. (Skills are copied byte-for-byte and have no such loss.)

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

- Python 3.11+ (uses the stdlib `tomllib` for Codex's config).

## Layout

```
.claude/skills/
  one2rule_them_all-mcps/    # MCP server sync  (SKILL.md + scripts + references)
  one2rule_them_all-skills/  # Agent Skill sync (SKILL.md + scripts + references)
INSTALL.ps1 / INSTALL.sh     # install the skills to ~/.claude/skills
```

## To Do

- Add macOS support for destinations — duplicate each of the destination reference tables for macOS (and Linux too).
