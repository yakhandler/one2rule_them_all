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

echo "Done."
