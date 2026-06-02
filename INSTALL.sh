#!/usr/bin/env bash
# Installs this repo's skills to the Claude Code user-level folder (~/.claude/skills).
# Each skill is a subfolder of .claude/skills that contains a SKILL.md; this rule
# automatically skips the *-workspace scratch folder, which has no SKILL.md.
set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.claude/skills"
dest_dir="$HOME/.claude/skills"

mkdir -p "$dest_dir"

for skill in "$source_dir"/*/; do
    [ -f "$skill/SKILL.md" ] || continue
    name="$(basename "$skill")"
    rm -rf "$dest_dir/$name"
    cp -R "$skill" "$dest_dir/$name"
    echo "Installed $name -> $dest_dir/$name"
done

# --- Heads-up: is there a Python the skills' engines can actually run? -----------------
# The reconcilers need Python 3.11+ (stdlib tomllib). Non-fatal: the skills install either
# way; this only warns so a missing/old Python doesn't later make Codex sync fail silently.
py=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then py="$cand"; break; fi
done
if [ -z "$py" ]; then
    echo "WARNING: no python3 on PATH — the skills need Python 3.11+ to run." >&2
    echo "         Install one, e.g. 'brew install python@3.12' or your distro's python3.12." >&2
elif ! "$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    ver="$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    echo "WARNING: $py is Python $ver, but the skills need 3.11+ (stdlib tomllib)." >&2
    echo "         JSON clients still sync, but Codex is skipped. Install a newer Python" >&2
    echo "         (e.g. 'brew install python@3.12' or pyenv)." >&2
fi

echo "Done."
