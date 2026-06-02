---
name: one2rule_them_all-skills
description: >-
  Merge and sync Agent Skills across every skill-capable tool on the machine (Claude
  Code/Desktop, Codex, Antigravity, Gemini CLI, and optionally Cursor) so each tool ends up
  with the full union of skills — each skill's whole directory (SKILL.md + scripts +
  references + assets) copied in faithfully, with nothing lost. Use this whenever the user
  wants to reconcile, sync, merge, consolidate, copy, share, or "install everywhere" their
  skills between tools/apps — e.g. "I made a skill in Claude but Codex doesn't have it",
  "get all my skills into every tool", "my skills are out of sync across CLIs", "one config
  to rule them all but for skills", or any request about skill folders drifting apart.
  Trigger even if the user names only one or two tools or doesn't say "reconcile." This is
  the SKILLS counterpart to one2rule_them_all-mcps (which does the same for MCP servers).
---

# Reconcile Agent Skills Across Tools

Every skill-capable tool keeps its skills in its own root directory, and the sets drift: a
skill you author for Claude never appears in Codex, one you set up for Antigravity is
missing from Gemini, and so on. This skill computes the **union** of every skill across
every participating tool and copies that complete set into each tool — faithfully, in the
same on-disk format they all use (`<root>/<name>/SKILL.md` + the skill's other files) —
without losing or silently changing anything.

Unlike MCP servers (small config-file entries), **a skill is a whole directory tree**. So
this tool syncs *directories*, byte-for-byte, not config keys. All the real work is done by
**`scripts/reconcile_skills.py`**. Your job is to run it, interpret its output, and handle
conflicts and warnings. Do not hand-copy skill folders yourself — the script handles
discovery, identity, frontmatter adaptation, backups, and idempotency correctly.

## What it guarantees

- **No skill is ever deleted.** Every tool ends with at least the skills it started with.
  The merge is purely additive (plus any conflict resolution you approve).
- **Tool-managed namespaces are left alone.** Anything whose name starts with `.` is never
  read or written — the reserved `.system/` skills the tools install themselves, Cursor's
  `.sync-manifest.json`, the script's own `.skill-backups/`. A directory only counts as a
  skill if it contains a `SKILL.md` (so scratch/workspace folders are ignored).
- **Skills are copied verbatim.** The *only* thing ever rewritten is an over-limit
  `description:` value (see below) — fences, line endings, body, scripts, assets, and
  `agents/openai.yaml` all travel exactly as-is.
- **Overwrites are backed up** to `<root>/.skill-backups/<timestamp>/<name>/` before writing.
- **Conflicts stop the process.** If the same skill *name* has genuinely *different content*
  in two tools, the tool refuses to guess — it reports the difference and writes nothing
  until you decide.

## Scope (which tools participate)

| Tool key                       | Skills root                                            | Role         |
| ------------------------------ | ------------------------------------------------------ | ------------ |
| `claude`                       | `~/.claude/skills`                                     | read + write |
| `codex`                        | `~/.codex/skills`                                      | read + write |
| `gemini` (alias `antigravity`) | `~/.gemini/skills` — Gemini CLI **and** Antigravity (CLI & IDE) | read + write |
| `agents`                       | `~/.agents/skills` (.agents standard)                  | read + write, **auto-created** |
| `cursor`                       | `~/.cursor/skills` (native)                            | **read-only source** |

The Gemini CLI and both Antigravity surfaces (`agy` CLI and IDE) read the *same* `~/.gemini/skills`
directory, so they're one entry. The old key `antigravity` still works as an alias on `--only` /
`--exclude` / `--include` / `--prefer`.

`~/.agents/skills` is the vendor-neutral [.agents standard](https://dotagentsprotocol.com/)
location (read by Antigravity, Cursor, OpenCode, and others). It's a first-class source and
destination, and is **created if missing** so the standard location always exists.

### Cursor is a read-only source — and why

Per [Cursor's docs](https://cursor.com/docs/skills), Cursor **natively loads skills from the
other tools' folders** for compatibility — `~/.claude/skills`, `~/.codex/skills`, plus
`~/.agents/skills` and its own `~/.cursor/skills`. So once this tool syncs the union across
Claude/Codex/Antigravity, **Cursor already sees the full union for free** — nothing needs to
be written into a Cursor folder. The reconciler therefore:

- **Reads** `~/.cursor/skills` (Cursor's *native* folder) as a source, so any skill you author
  directly in Cursor propagates *out* to the other tools.
- **Never writes** to any Cursor folder.
- **Ignores `~/.cursor/skills-cursor`** entirely — that's a third-party sync tool's folder
  (it carries a `.sync-manifest.json`) that Cursor itself does not read.

For the other three tools, each tool's own built-in skills live in a reserved `.system/`
directory; anything dot-prefixed is skipped by discovery, so those built-ins never propagate
and are never touched.

See `references/skill-paths.md` for the full path/format/quirk details.

## Workflow

### 1. Run a plan first (always)

Never start with `--apply`. Run the default dry-run:

```
python3 <skill_dir>/scripts/reconcile_skills.py
```

> Use `python3` on macOS/Linux; on Windows use `python` (or `py -3`). Needs Python 3.11+.

Summarize for the user: how many tools were found, how many unique skills make up the
union, and which skills are missing from which tools (the "Skill coverage" section). The
"Per-tool plan" shows `+ add`, `~ change`, and `= unchanged` counts per tool.

### 2. Read the WARNINGS section

If a skill's `description:` exceeds a tool's frontmatter limit (default 1024 chars), the
tool **warns and shortens it to fit**, changing as little as possible (it trims at a
sentence/word boundary, keeping the leading trigger text; everything else is untouched).
When you see a shorten warning, offer to hand-write a tighter description in the *source*
skill instead — a human-quality rewrite beats a mechanical trim — then re-run. A `name`
over 64 chars is only flagged, never auto-changed (renaming a skill directory breaks
references).

### 3. Handle conflicts (exit code 2)

If the report shows **BLOCKING CONFLICTS** (exit code 2), do not apply. The script reports
the kind:
- **body differs** — the skills' files/scripts/SKILL.md body genuinely differ.
- **description differs** — same files, but descriptions diverge in a way that isn't just
  one being a trimmed copy of the other.

Two ways forward:
- **Pick a winner** — re-run with `--prefer`, a comma-separated tool priority list. For
  each conflicting skill, the first tool in the list that has it wins and its version is
  copied everywhere. Example: `--prefer claude,codex`.
- **Edit to match** — the user reconciles the two copies by hand, then you re-run.

Resolving by preference **overwrites the losing tools' copies** (their originals are backed
up). Call that out before applying.

### 4. Apply on confirmation

Once the plan looks right and conflicts are resolved, get the go-ahead and run with
`--apply` (carry over the same `--prefer`/`--include` you used in the plan):

```
python3 <skill_dir>/scripts/reconcile_skills.py --apply [--prefer <keys>] [--include cursor]
```

Report which tools changed, what was added/changed, and that backups are under each root's
`.skill-backups/<stamp>/`. Tools that were running may need a restart to pick up new skills.

## Useful options

- `--prefer <keys>` — priority order to auto-resolve conflicts (see above).
- `--only <keys>` / `--exclude <keys>` — limit which tools participate (e.g. `--exclude cursor`).
- `--include <keys>` — add a tool that was excluded, without dropping the rest.
- `--only-skill <names>` / `--skip-skill <names>` — restrict the sync to, or hold back,
  specific skills by name. Use `--skip-skill` to leave a tool-specific skill where it is.
- `--create-missing` — also create skills roots for participating tools that don't have one.
  Off by default. Only use it if the user explicitly wants to seed a tool that has no root.
- `--max-desc <n>` / `--max-name <n>` — override the frontmatter limits (default 1024 / 64).
- `--json` — machine-readable summary instead of the text report.
- `--home <path>` — override the home directory (used for testing with a sandbox).

## Things worth knowing

- **All tools share one format.** Despite older notes claiming JSON rule-maps (Cursor),
  TOML configs (Codex), or `AGENTS.md` orchestrators (Antigravity), on disk every tool uses
  the same Anthropic SKILL.md directory format. So syncing is a faithful directory copy;
  there is no cross-format conversion to do.
- **Identity ignores the description value.** A skill and the auto-shortened copy the tool
  itself wrote are recognized as the same skill, so re-running is idempotent and never
  invents a conflict between a full description and its trimmed twin.
- **`agents/openai.yaml` and assets travel with the skill.** They're just files in the
  skill directory; nothing special is done to them, and nothing is fabricated.
- **Scope is global skill roots only.** Project-local skills (e.g. a repo's `.claude/skills`)
  and the reserved `.system/` namespace are out of scope and untouched.

For the exact paths, formats, and per-tool quirks, see `references/skill-paths.md`.
