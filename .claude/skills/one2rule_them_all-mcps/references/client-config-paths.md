# MCP Client Config Destinations

The reconciler resolves these paths automatically. This table documents where each
client stores its MCP server list and the format/quirks the script accounts for. Client
keys (used with `--only`/`--exclude`/`--prefer`) are in the first column.

| Key              | Client                                 | Destination path                                                                           | Format / schema                               |
| ---------------- | -------------------------------------- | ------------------------------------------------------------------------------------------ | --------------------------------------------- |
| `claude-desktop` | Claude Desktop & Cowork (Windows)      | `%APPDATA%\Claude\claude_desktop_config.json`                                              | JSON `mcpServers`                             |
| `claude-desktop` | Claude Desktop & Cowork (Windows MSIX) | `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json` | JSON `mcpServers`                             |
| `claude-desktop` | Claude Desktop & Cowork (macOS)        | `~/Library/Application Support/Claude/claude_desktop_config.json`                          | JSON `mcpServers`                             |
| `claude-desktop` | Claude Desktop & Cowork (Linux)        | `~/.config/Claude/claude_desktop_config.json`                                             | JSON `mcpServers`                             |
| `claude-code`    | Claude Code (global)                   | `~/.claude.json`                                                                           | JSON `mcpServers`                             |
| `codex`          | Codex (CLI / Desktop)                  | `~/.codex/config.toml`                                                                     | TOML `mcp_servers` table                      |
| `gemini`         | Gemini CLI                             | `~/.gemini/settings.json`                                                                  | JSON `mcpServers`                             |
| `antigravity`    | Antigravity (CLI / IDE)                | `~/.gemini/config/mcp_config.json`                                                         | JSON `mcpServers`, entry gets `type: "stdio"` |
| `cursor`         | Cursor                                 | `~/.cursor/mcp.json`                                                                       | JSON `mcpServers`                             |
| `agents`         | `.agents` standard (auto-created)      | `~/.agents/mcp.json`                                                                       | JSON `mcpServers`                             |

## Path resolution notes

- `~` = `Path.home()` (honors `USERPROFILE` on Windows, `HOME` elsewhere).
- `%APPDATA%` / `%LOCALAPPDATA%` are read from the environment.
- The Windows MSIX (Microsoft Store) install nests under a package folder whose id
  varies, so it's resolved with a glob on `Claude_*`. If both a standard and an MSIX
  install are present, each existing file is reconciled independently.
- The macOS/Linux Claude Desktop paths let the tool work off-Windows; on a given machine
  only the locations that actually exist are used.

## Format details the reconciler relies on

- **Shared JSON schema.** Every client except Codex stores servers under a top-level
  `mcpServers` object: `{ "mcpServers": { "<name>": { ... } } }`. The reconciler edits the
  file **surgically** — it locates the top-level `mcpServers` value in the raw text and
  replaces just that span (or inserts the key if absent), leaving every other byte
  untouched (other keys, whitespace, key order, and project-scoped servers under
  `projects.*.mcpServers`). The replaced block is re-serialized to match the file's own
  indentation/newline style. The result is verified by re-parsing before it is written; if
  the surgical edit can't be verified, that file is **left untouched** and reported (the run
  exits code 3), never reformatted or corrupted. (This is why a large, sensitive
  `~/.claude.json` is never reformatted top-to-bottom — not even as a fallback.)
- **Antigravity quirk.** Same JSON schema, but each stdio server entry carries an
  explicit `type: "stdio"`. The tool injects this when writing Antigravity and strips it
  (as redundant) in its neutral comparison form, so an Antigravity entry never looks like
  a conflict against the same server elsewhere. Remote entries keep their real `type`.
- **Codex TOML.** The only non-JSON target. Servers live in `[mcp_servers.<name>]`
  tables (snake_case `mcp_servers`), e.g.:

  ```toml
  [mcp_servers.marq-snowflake]
  command = "uvx"
  args = ["marq-snowflake"]
  env = { SNOWFLAKE_ACCOUNT = "marq" }
  ```

  Read via stdlib `tomllib`. On write, the tool removes existing `[mcp_servers.*]` table
  blocks and appends freshly rendered tables, leaving every other line (comments, model
  settings, `[profiles.*]`, etc.) untouched. Remote servers are written with `type`,
  `url`, and any headers as table keys.

## Out of scope (intentionally untouched)

- Project-scoped servers inside `~/.claude.json` under `projects.<path>.mcpServers`.
- Repo-local `.cursor/mcp.json` or other per-project MCP configs.
- VS Code / other clients not listed above.
