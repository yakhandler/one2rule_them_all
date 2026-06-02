# Skill Root Destinations

The reconciler resolves these paths automatically. This documents where each tool stores
its skills, plus the on-disk behaviors the engine relies on. The keys in the first column
are what you pass to `--only` / `--exclude` / `--include` / `--prefer`.

| Key                            | Tool                                  | Skills root                 | Role                           |
| ------------------------------ | ------------------------------------- | --------------------------- | ------------------------------ |
| `claude`                       | Claude Code / Desktop                 | `~/.claude/skills`          | read + write                   |
| `gemini` (alias `antigravity`) | Gemini CLI + Antigravity (CLI & IDE)  | `~/.gemini/skills`          | read + write                   |
| `agents` (alias `codex`)       | `.agents` standard + OpenAI Codex     | `~/.agents/skills`          | read + write, auto-created     |
| `cursor`                       | Cursor IDE                            | `~/.cursor/skills` (native) | read-only source               |

- **`gemini` / `antigravity`** are one entry: the Gemini CLI and both Antigravity surfaces
  (the `agy` CLI and the IDE) all read the *same* `~/.gemini/skills` directory. Either key
  selects it.
- **`agents`** is the vendor-neutral [.agents standard](https://dotagentsprotocol.com/),
  read by Antigravity, Cursor, OpenCode, and others. It is also **OpenAI Codex's** user
  skill location ([Codex docs](https://developers.openai.com/codex/skills)): Codex reads user
  skills from `~/.agents/skills`, **not** `~/.codex/skills`, so the old `codex` key is now an
  alias for this entry. It is **created if missing** so the standard location always exists.
- **`cursor`** is read-only: Cursor natively loads the other tools' folders
  ([Cursor docs](https://cursor.com/docs/skills)), so once they're synced it already sees the
  union. The engine **reads** `~/.cursor/skills` (so Cursor-authored skills propagate out) but
  **never writes** to any Cursor folder, and ignores `~/.cursor/skills-cursor` (a third-party
  sync tool's folder that Cursor does not read).

`~` = `Path.home()` (honors `USERPROFILE` on Windows, `HOME` elsewhere). Override with `--home`.

## On-disk format (shared by every tool)

Every tool uses the same Anthropic Agent Skills directory format, so reconciling is a
**faithful directory copy** — no cross-format conversion:

```
<root>/<skill-name>/
  SKILL.md            # YAML frontmatter (name, description) + markdown body
  scripts/            # optional executable helpers
  references/         # optional docs loaded on demand
  assets/             # optional icons/images
  agents/openai.yaml  # optional product-specific interface metadata (Codex/Antigravity)
  LICENSE.txt         # optional
```

Files are copied byte-for-byte. The only field the engine ever rewrites is an over-limit
`description:` value (see below).

## Behavior the reconciler relies on

- **Dot-prefixed entries are skipped entirely** — never read, written, or counted. This
  covers each tool's reserved `.system/` namespace (imagegen, skill-creator, …), the script's
  own `.skill-backups/`, and stray files like Cursor's `.sync-manifest.json`.
- **A directory is a skill only if it contains a `SKILL.md`.** Scratch/workspace dirs holding
  only helper scripts are ignored.
- **Frontmatter is never normalized.** Real skills use quoted, unquoted, and folded (`>-`)
  scalars, extra keys, and at least one ships a malformed `\---` fence with CRLF. The engine
  copies verbatim and parses frontmatter only defensively (for display, identity, and the
  description-limit check). If a description can't be located safely, it is flagged, not touched.

## Frontmatter limits

- **`name` ≤ 64 characters** — flagged when over, never auto-changed: the directory name is the
  skill's identifier, and renaming would break cross-references.
- **`description` ≤ 1024 characters** (Anthropic's documented max) is the default ceiling before
  the engine warns and shortens. Override per run with `--max-desc`.
- **Shortening is minimal**: the description is trimmed at the last sentence boundary that fits,
  else the last word boundary, keeping the leading trigger text. The full original survives in
  the source tool's copy and in the backup.

## Out of scope (intentionally untouched)

- The reserved `.system/` skills and any other dot-prefixed entry.
- Project-local skill folders (e.g. a repository's own `.claude/skills`).
- A tool's own non-skill config files in a skills root (e.g. Cursor's `.sync-manifest.json`).
- VS Code / other editors not listed above.
