# Installs this repo's skills to the Claude Code user-level folder (~/.claude/skills).
# Each skill is a subfolder of .claude/skills that contains a SKILL.md; this rule
# automatically skips the *-workspace scratch folder, which has no SKILL.md.
$ErrorActionPreference = 'Stop'

$source = Join-Path $PSScriptRoot '.claude/skills'
$dest   = Join-Path $HOME '.claude/skills'

New-Item -ItemType Directory -Force -Path $dest | Out-Null

Get-ChildItem -Path $source -Directory |
    Where-Object { Test-Path (Join-Path $_.FullName 'SKILL.md') } |
    ForEach-Object {
        $target = Join-Path $dest $_.Name
        if (Test-Path $target) { Remove-Item $target -Recurse -Force }
        Copy-Item -Path $_.FullName -Destination $target -Recurse -Force
        Write-Host "Installed $($_.Name) -> $target"
    }

Write-Host "Done."
