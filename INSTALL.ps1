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

# --- Heads-up: is there a Python the skills' engines can actually run? -----------------
# The reconcilers need Python 3.11+ (stdlib tomllib). Non-fatal: the skills install either
# way; this only warns so a missing/old Python doesn't later make Codex sync fail silently.
$pyExe = $null; $pyArgs = @()
foreach ($cand in 'python', 'py') {
    if (Get-Command $cand -ErrorAction SilentlyContinue) {
        $pyExe = $cand
        if ($cand -eq 'py') { $pyArgs = @('-3') }
        break
    }
}
if (-not $pyExe) {
    Write-Warning "No Python on PATH - the skills need Python 3.11+ (install from python.org or the Microsoft Store)."
} else {
    try {
        $probe = 'import sys; print("%d.%d" % sys.version_info[:2]); print(1 if sys.version_info >= (3,11) else 0)'
        $lines = & $pyExe @pyArgs -c $probe 2>$null
        if ($lines -and $lines.Count -ge 2 -and "$($lines[1])".Trim() -ne '1') {
            Write-Warning "$pyExe is Python $($lines[0]), but the skills need 3.11+ (stdlib tomllib). JSON clients still sync, but Codex will be skipped."
        }
    } catch {
        Write-Warning "Could not determine the Python version. Ensure Python 3.11+ is installed to run the skills."
    }
}

Write-Host "Done."
