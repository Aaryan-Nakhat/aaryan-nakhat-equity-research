# Launcher for the email bot — loads .env and keeps the bot running.
# Used by the "EquityResearchEmailBot" scheduled task (runs at logon).
# Manual run:  powershell -ExecutionPolicy Bypass -File scripts\run_email_bot.ps1

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

# Launcher markers go to a separate file; the bot logs to email_bot.log itself.
$launchlog = Join-Path $root "data\processed\email_launcher.log"
New-Item -ItemType Directory -Force -Path (Split-Path $launchlog) | Out-Null

# Single-instance guard. Stopping the scheduled task does NOT kill the uv-spawned
# bot child, so a stale bot can survive and then fight this one over the
# single-writer DuckDB (IOException: file is being used by another process). Kill
# any stale bot python + any other launcher before we start, so exactly one runs.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*email_bot.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like '*run_email_bot*' -and $_.ProcessId -ne $PID } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

# Auto-restart loop: if the bot exits/crashes, wait and relaunch.
while ($true) {
    "$(Get-Date -Format o)  starting email bot" | Out-File -Append -FilePath $launchlog -Encoding utf8
    & $uv run python scripts/email_bot.py
    "$(Get-Date -Format o)  email bot exited (code $LASTEXITCODE); restarting in 60s" | Out-File -Append -FilePath $launchlog -Encoding utf8
    Start-Sleep -Seconds 60
}
