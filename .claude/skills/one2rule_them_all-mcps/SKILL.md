---
name: one2rule_them_all-mcps
description: >-
  Merge and sync MCP server definitions across every MCP client config on the machine
  (Claude Desktop/Cowork, Claude Code, Codex, Gemini CLI, Antigravity, Cursor) so each
  client ends up with the full union of servers, written in that client's native format,
  with nothing lost. Use this whenever the user wants to reconcile, sync, merge,
  consolidate, copy, or "share" MCP servers between clients/apps/tools — e.g. "I added a
  server in Cursor but Claude Desktop doesn't have it", "get all my MCP servers into
  every tool", "my MCP configs are out of sync", "one config to rule them all", or any
  request involving MCP server lists drifting apart across clients. Trigger even if the
  user names only one or two clients or doesn't say the word "reconcile."
---

# Reconcile MCP Servers Across Clients

Each MCP client keeps its own list of MCP servers in its own file and format, so the
lists drift: a server you add in Cursor never shows up in Codex, one you set up for
Claude Desktop is missing from Gemini, and so on. This skill computes the **union** of
every server across every client present on the machine and writes that complete list
back into each client — in the format that client expects — without losing or silently
changing anything.

All the real work is done by **`scripts/reconcile_mcp.py`**. Your job is to run it,
interpret its output for the user, and handle conflicts. Do not hand-edit config files
yourself — the script handles format conversion, file preservation, and backups
correctly, and hand-editing risks corrupting large files like `~/.claude.json` or
Codex's `config.toml`.

## What it guarantees

These properties are why the tool exists; keep them in mind when explaining results:

- **No server is ever deleted.** Every client ends with at least the servers it started
  with. The merge is purely additive (plus conflict resolution you approve).
- **Only the MCP section is touched.** Everything else in each file — Codex profiles,
  Claude Code's `projects`/history, Gemini settings, etc. — is preserved. Project-scoped
  servers inside `~/.claude.json`'s `projects` block are intentionally left alone; only
  the top-level global `mcpServers` is reconciled.
- **Every file is backed up** before it's written (`<file>.bak-<timestamp>`).
- **Conflicts stop the process.** If the same server *name* has *different* definitions
  in two clients, the tool refuses to guess — it reports both and writes nothing until
  the user decides.

## Workflow

### 1. Run a plan first (always)

Never start with `--apply`. Run the default dry-run so you and the user can see exactly
what would change:

```
python3 <skill_dir>/scripts/reconcile_mcp.py
```

> Use `python3` on macOS/Linux; on Windows use `python` (or `py -3`). The engine needs
> Python 3.11+ for Codex (stdlib `tomllib`); if launched on an older interpreter it
> auto-re-execs into the newest `python3.x` on PATH. If none is found, the JSON clients
> still sync but Codex is skipped (with a message telling you how to fix it).

The script auto-discovers each client's config from standard locations (see
`references/client-config-paths.md`). It only considers clients whose config file
actually exists; ones that aren't installed are listed as skipped.

Read the output and summarize for the user: how many clients were found, how many unique
servers make up the union, and which servers are missing from which clients. The
"Per-client plan" section shows `+ add`, `~ change`, and `= unchanged` counts per client.

### 2. Handle conflicts (exit code 2)

If the report shows **BLOCKING CONFLICTS** (and the script exits with code 2), do not
apply anything by default. Show the user the conflicting definitions the script printed
(it also prints a ready-to-use `--prefer` suggestion and lists the clients involved) and
ask how they want to resolve each one. There are three ways forward:

- **Pick a winning client** — re-run with `--prefer`, a comma-separated priority list of
  client keys. For each conflicting name, the first client in the list that defines it
  wins. Example: `--prefer cursor,claude-code` means "use Cursor's version; if Cursor
  doesn't have it, use Claude Code's."
- **Edit to match** — the user edits one config so both definitions are identical, then
  you re-run the plan and the conflict disappears.
- **Skip them for now** — re-run with `--skip-conflicts` to sync everything *except* the
  conflicting names, leaving each client's own copy of those untouched (nothing is
  overwritten or deleted). The conflicts are still reported and the exit code stays **2**
  so they aren't forgotten. Good when the user wants the non-conflicting servers in place
  immediately and will reconcile the rest later. Confirm with the user before using it,
  since it leaves real divergence unresolved.

Valid client keys: `claude-desktop`, `claude-code`, `codex`, `gemini`, `antigravity`,
`cursor`, `agents`. Resolving a conflict by preference will *change* the losing clients'
entries to match the winner — call that out explicitly before applying, since it's the one
case where an existing definition gets overwritten.

`agents` is the vendor-neutral [.agents standard](https://dotagentsprotocol.com/) config at
`~/.agents/mcp.json` (same `mcpServers` JSON schema). It's a first-class source and
destination and is **created if missing**, so the standard location always exists.

### 3. Apply on confirmation

Once the plan looks right and any conflicts are resolved, get the user's go-ahead and
run with `--apply` (carry over the same `--prefer` you used in the plan):

```
python3 <skill_dir>/scripts/reconcile_mcp.py --apply [--prefer <keys>]
```

Report back: which files changed, what was added to each, and where the backups were
written. Remind the user that clients which were running may need a restart to pick up
the new servers.

## Useful options

- `--prefer <keys>` — priority order to auto-resolve name conflicts (see above).
- `--skip-conflicts` — sync the non-conflicting servers and leave conflicting names alone
  instead of blocking the whole run (see above). Exit code stays 2; nothing is overwritten.
- `--only <keys>` / `--exclude <keys>` — limit which clients participate. Use `--exclude`
  if the user wants to leave a particular client out of the sync.
- `--create-missing` — also create config files for clients that don't have one yet.
  Off by default, because fabricating a config for an app that isn't installed is usually
  not what the user wants. Only use it if they explicitly ask to set up a client that has
  no config.
- `--json` — machine-readable summary instead of the text report; handy if you need to
  reason programmatically about the plan before deciding next steps.
- `--home`, `--appdata`, `--localappdata` — override path roots (used for testing; not
  needed in normal runs).

## Things worth knowing

- **`type: "stdio"` is normalized.** A command-based server is stdio by definition.
  Antigravity records an explicit `type: "stdio"` on each entry; other clients omit it.
  The tool treats these as the same server (so it doesn't report a false conflict), drops
  the redundant `type` in its neutral form, and re-adds it only when writing Antigravity.
  Remote servers (`type: "sse"` / `"http"` with a `url`) keep their type everywhere.
- **Codex is the only non-JSON target.** Its `config.toml` uses an `mcp_servers` table
  with snake_case. The tool reads it with stdlib `tomllib`, surgically replaces just the
  `[mcp_servers.*]` tables, and leaves the rest of the file (comments, model settings,
  profiles) intact.
- **JSON files are re-serialized** with 2-space indentation when changed. Data and key
  order are preserved, but exact original whitespace is not — the backup holds the
  original verbatim if the user ever wants to compare.
- **Scope is the six clients above, global configs only.** Project-local configs (e.g. a
  `.cursor/mcp.json` inside a repo, or per-project servers in `~/.claude.json`) are out
  of scope and untouched.

For the exact paths, formats, and per-client quirks, see
`references/client-config-paths.md`.
