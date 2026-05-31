# MCP Client Config Destinations

| #   | Client                                 | Destination path                                                                           | Format / schema                               |
| --- | -------------------------------------- | ------------------------------------------------------------------------------------------ | --------------------------------------------- |
| 1   | Claude Desktop & Cowork (Windows)      | `%APPDATA%\Claude\claude_desktop_config.json`                                              | JSON `mcpServers`                             |
| 2   | Claude Desktop & Cowork (Windows MSIX) | `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json` | JSON `mcpServers`                             |
| 3   | Claude Code (global)                   | `~/.claude.json`                                                                           | JSON `mcpServers`                             |
| 4   | Codex (CLI / Desktop)                  | `~/.codex/config.toml`                                                                     | TOML `mcp_servers` table                      |
| 5   | Gemini CLI                             | `~/.gemini/settings.json`                                                                  | JSON `mcpServers`                             |
| 6   | Antigravity (CLI / IDE)                | `~/.gemini/config/mcp_config.json`                                                         | JSON `mcpServers`, entry gets `type: "stdio"` |
| 7   | Cursor                                 | `~/.cursor/mcp.json`                                                                       | JSON `mcpServers`                             |

## Notes

- `~` = `os.homedir()`.
- All Options except 4 share the JSON `{ mcpServers: { … } }` schema; Antigravity additionally prepends `type: "stdio"` to the server entry
- Codex is the only non-JSON target — TOML with an `mcp_servers` table (underscore)
