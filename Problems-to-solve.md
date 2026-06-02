# Problems to solve

A pre-flight code review from the perspective of an experienced macOS/Linux dev who
just cloned the repo and was about to run `INSTALL.sh`. Findings are ranked by impact.
Each notes whether it bites at **install time** or **first real run** (when the skill
invokes an engine against live configs), and whether it's a real bug vs. expected
behavior worth documenting.

## What's already fine (no action needed)

- **`INSTALL.sh` copy is cross-platform-safe.** The GNU-vs-BSD `cp -R` trailing-slash
  gotcha doesn't bite because `dest` is `rm -rf`'d first, so `cp -R src/ dest` lands
  contents in a fresh `dest` identically on macOS and Linux.
- **Stale `__pycache__/*.cpython-314.pyc` is gitignored** (`__pycache__/`, `*.py[cod]`),
  so a fresh clone won't carry it. The skills engine also skips `.pyc`/`__pycache__` on
  copy, so it never propagates.

---

## 1. `python3` on macOS/Linux is almost certainly too old → Codex silently dropped
**When:** first run · **Type:** real friction / doc gap · **Status:** ✅ RESOLVED

> **Fix applied (self-bootstrap):** `reconcile_mcp.py` now re-execs under the newest
> `python3.x` it finds on PATH when launched on a pre-3.11 interpreter, so the documented
> `python3 reconcile_mcp.py` invocation "just works" without the caller hunting for the
> right Python (`_reexec_under_modern_python()`, called from `__main__`). It's a no-op on
> 3.11+, on Windows, and when already bootstrapped (sentinel env var prevents loops); if
> nothing newer exists it runs degraded and the Codex-skip message now names the fix
> (`brew install python@3.12`, etc.). Deliberately **not** added to `reconcile_skills.py`
> — that engine genuinely runs on 3.9+ (no `tomllib`), so the machinery would be dead code
> there. Verified: sandbox dry→apply→dry is idempotent and reads/writes Codex TOML; the
> re-exec branch unit-tested via mocks (picks newest, honors sentinel/Windows/modern guards).
> Docs follow-up done: tightened every place that wrongly implied the *skills* engine needs
> 3.11+ (README "How it runs" + Requirements, both `SKILL.md` notes, CLAUDE.md). They now
> state the skills engine runs on **3.9+** and only the MCP/Codex path needs 3.11+ — and
> note that the MCP engine auto-re-execs into a newer `python3.x` when the default is older.

Original analysis below, for the record:


- Install only *warns* about Python version, then proceeds. Every example in both
  `SKILL.md` files and the README hardcodes bare **`python3`**.
- macOS Command Line Tools ships 3.9; Ubuntu 22.04 ships 3.10. On either,
  `reconcile_mcp.py` fails to import `tomllib`, falls back to `tomli` (not installed),
  sets `tomllib = None`, and **skips Codex** — its servers never join the union and it
  never receives others'.
- Even after `brew install python@3.12`, the binary is `python3.12`, not bare `python3`,
  so the agent driving the skill keeps calling the old interpreter.
- **Doc nuance:** the requirement is overstated. `reconcile_skills.py` runs fine on
  3.9/3.10 (no `tomllib`; it guards `rmtree` `onexc`/`onerror` on `sys.version_info`).
  Only the MCP/Codex path truly needs 3.11+.

**Possible fixes:** have the engines/skills auto-detect the newest available interpreter
(`python3.12`/`py -3.12`/etc.) instead of bare `python3`; or make the install hard-fail
(opt-in override) instead of warn; or document the interpreter-selection step explicitly.

## 2. MCP engine rewrites `~/.claude.json` (the live Claude Code config) while it's running
**When:** first run · **Type:** real risk

- `claude-code` resolves to `~/.claude.json`, read whole and re-dumped with `indent=2`
  via atomic `os.replace`.
- A running Claude Code holds its own in-memory copy and flushes on state change/exit, so
  it can **clobber the engine's `mcpServers` edit**, or new servers won't appear until
  restart. SKILL.md mentions "restart to pick up servers" but not the inverse clobber.
- Whole-file reformatting: `~/.claude.json` and `~/.gemini/settings.json` get
  re-serialized top-to-bottom. Lossless and backed up, but produces a huge diff if these
  live in a dotfiles repo. (`~/.claude.json` also holds OAuth tokens/history — preserved,
  but the file is rewritten.)

**Possible fixes:** detect a running Claude Code and warn/defer; document "apply when
Claude Code isn't the live writer, then restart."

## 3. `~/.agents/` is materialized on apply whether or not you use the `.agents` standard
**When:** first run · **Type:** expected behavior, surprising

- Both engines flag `agents` with `always_create=True`, honored independently of
  `--create-missing`. First `--apply` with a non-empty union creates
  `~/.agents/mcp.json` and `~/.agents/skills/` even if the user never uses dotagents.

**Possible fix:** gate `always_create` behind a flag, or call it out in the plan output
before writing.

## 4. First real run will likely block on conflicts (exit 2)
**When:** first run · **Type:** expected, not a bug

- With many existing skills/servers, any same-named-but-different definition across two
  tools makes the engine refuse to guess, print both, write nothing, and exit 2.
- Plan for an iterative first run with `--prefer <keys>` or hand-reconciliation, not a
  clean one-shot.

**Possible fix:** none needed (by design); maybe surface a one-line "resolve with
--prefer" hint earlier / more prominently.

## 5. Latent Codex-TOML corruption edge case
**When:** first run · **Type:** real bug, low probability

- `_strip_mcp_tables` (`reconcile_mcp.py`) decides "am I inside an `[mcp_servers.*]`
  block" purely by whether a stripped line *starts with `[`*.
- If an existing `[mcp_servers.x]` block contains a continuation line beginning (after
  indentation) with `[` — a nested-array element on its own line:

  ```toml
  [mcp_servers.foo]
  args = [
    ["nested"],   # <- starts with '[' after strip
  ]
  ```

  …that line is misparsed as a table header, flips `in_mcp` off early, and the leftover
  `["nested"],` / `]` lines are **kept** while a fresh `[mcp_servers.foo]` is appended →
  junk / invalid TOML.
- Normal MCP entries (single-line `args`) never hit this, and the `.bak` saves you, but
  hand-formatted multi-line nested arrays are vulnerable.

**Possible fix:** track table-block boundaries using the `tomllib` parse (which the
engine already has) rather than re-scanning raw lines, or make the line scanner
array-aware.

## 6. Some destination paths are the repo's own admitted unknowns
**When:** first run · **Type:** unverified assumption

- README "To Do" and CLAUDE.md flag that historical path/format research was partly wrong
  and that the Cursor skills path and Antigravity's `~/.gemini/...` sharing aren't fully
  verified.
- Failure mode is **ineffective, not destructive**: servers/skills written to a dir the
  tool doesn't actually read.

**Possible fix:** verify on a real machine that each target tool *sees* a newly-synced
server/skill; add macOS/Linux destination tables to both `references/` docs.

---

## Minor notes

- **Run `bash INSTALL.sh`, not `sh INSTALL.sh`** — `set -u` + `${BASH_SOURCE[0]}` errors
  under dash. README says `bash`; just don't deviate.
- **`INSTALL.sh` doesn't back up** skills it overwrites (`rm -rf` then copy), unlike the
  engines. Only matters if a prior install of this repo exists; still inconsistent with
  the project's own "back up before overwrite" guarantee.
- **Memory:** `reconcile_skills.py` reads every file of every skill into RAM as bytes to
  fingerprint. Fine for dozens of skills, but not streaming; large assets add up.
