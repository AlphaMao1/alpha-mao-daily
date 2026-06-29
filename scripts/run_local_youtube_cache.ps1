$ErrorActionPreference = "Stop"

[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONIOENCODING = "utf-8"

$RepoRoot = "D:\Project\alpha-mao-daily"
$CookieFile = "D:\Secrets\alpha-mao-daily\youtube.cookies.txt"
$LogDir = "D:\Secrets\alpha-mao-daily\logs"
$LogFile = Join-Path $LogDir "youtube-cache-task.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-TaskLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogFile -Encoding UTF8 -Value "[$timestamp] $Message"
}

Write-TaskLog "Started Alpha Mao Daily local YouTube cache task."

if (-not (Test-Path $RepoRoot)) {
    throw "Repo root not found: $RepoRoot"
}

if (-not (Test-Path $CookieFile)) {
    throw "YouTube cookies file not found: $CookieFile"
}

Set-Location $RepoRoot

$env:YOUTUBE_COOKIES_FILE = $CookieFile
$env:YOUTUBE_COOKIES_FROM_BROWSER = "firefox:2ykxqsfh.default-release"
if ([string]::IsNullOrWhiteSpace($env:YOUTUBE_PROXY_URL)) {
    $env:YOUTUBE_PROXY_URL = "http://127.0.0.1:7890"
}

git pull --ff-only
python -m pip install -r requirements.txt
python src\collector.py --youtube-cache-only

git add data\youtube_cache
$pending = git status --short data\youtube_cache
if (-not [string]::IsNullOrWhiteSpace($pending)) {
    git commit -m "Update YouTube transcript cache"
    try {
        git push
    }
    catch {
        Write-TaskLog "Initial git push failed; retrying after pull --rebase --autostash."
        git pull --rebase --autostash
        git push
    }
    Write-TaskLog "Committed and pushed YouTube cache."
}
else {
    Write-TaskLog "No YouTube cache changes to commit."
}

Write-TaskLog "Finished Alpha Mao Daily local YouTube cache task."
