# Launcher for the Telegram bot — loads .env and keeps the bot running.
# Used by the "EquityResearchTelegramBot" scheduled task (runs at logon).
# Manual run:  powershell -ExecutionPolicy Bypass -File scripts\run_bot.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

# Load .env into the process environment (skip blanks/comments).
Get-Content (Join-Path $root ".env") | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
        $i = $line.IndexOf("=")
        $k = $line.Substring(0, $i).Trim()
        $v = $line.Substring($i + 1).Trim()
        if ($k) { Set-Item -Path ("Env:" + $k) -Value $v }
    }
}
$env:PYTHONUNBUFFERED = "1"

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = "C:\Users\Aaryan Nakhat\.local\bin\uv.exe" }

# Launcher markers go to a separate file; the bot logs to telegram_bot.log itself.
$launchlog = Join-Path $root "data\processed\bot_launcher.log"
New-Item -ItemType Directory -Force -Path (Split-Path $launchlog) | Out-Null

# Auto-restart loop: if the bot exits/crashes, wait and relaunch.
while ($true) {
    "$(Get-Date -Format o)  starting bot" | Out-File -Append -FilePath $launchlog -Encoding utf8
    & $uv run python scripts/telegram_bot.py
    "$(Get-Date -Format o)  bot exited (code $LASTEXITCODE); restarting in 15s" | Out-File -Append -FilePath $launchlog -Encoding utf8
    Start-Sleep -Seconds 15
}
