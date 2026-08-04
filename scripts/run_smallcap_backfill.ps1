# One-shot orchestrator: pause the email bot, seed + backfill the small-cap universe
# (Nifty Smallcap 250 + Microcap 250 → financials + 4Q SHP), then restart the bot.
# DuckDB is single-writer, so the bot MUST be down for the whole ingest. Run via the
# "EquityResearchSmallcapBackfill" scheduled task so it survives independently for hours.

$ErrorActionPreference = "Continue"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$log = Join-Path $root "data\processed\smallcap_backfill.log"     # python logging -> stderr (progress)
$out = Join-Path $root "data\processed\smallcap_backfill.out.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
function Note($m) { "$(Get-Date -Format o)  $m" | Out-File -Append -Encoding utf8 $log }

# Load .env (harmless for the ingest; keeps parity with the bot launcher).
Get-Content (Join-Path $root ".env") | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
        $i = $line.IndexOf("="); $k = $line.Substring(0, $i).Trim(); $v = $line.Substring($i + 1).Trim()
        if ($k) { Set-Item -Path ("Env:" + $k) -Value $v }
    }
}
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = "$env:USERPROFILE\.local\bin\uv.exe" }

try {
    Note "pausing the email bot (single-writer DuckDB)…"
    try { Disable-ScheduledTask -TaskName EquityResearchEmailBot -ErrorAction Stop | Out-Null } catch { Note "disable task: $_" }
    try { Stop-ScheduledTask   -TaskName EquityResearchEmailBot -ErrorAction Stop } catch {}
    # kill the launcher loop + the uv/python bot tree so the DB lock is released
    foreach ($n in @('powershell.exe','uv.exe','python.exe')) {
        Get-CimInstance Win32_Process -Filter ("Name='" + $n + "'") |
            Where-Object { $_.CommandLine -like '*email_bot*' -or $_.CommandLine -like '*run_email_bot*' } |
            ForEach-Object { & taskkill /PID $_.ProcessId /T /F 2>$null | Out-Null }
    }
    Start-Sleep -Seconds 5
    $stillBot = @(Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -eq 'python.exe' -or $_.Name -eq 'uv.exe') -and $_.CommandLine -like '*email_bot*' })
    Note ("bot processes remaining after kill: " + $stillBot.Count)

    Note "starting seed + backfill (Nifty Smallcap 250 + Microcap 250, only-missing)…"
    $p = Start-Process -FilePath $uv `
        -ArgumentList @('run','python','scripts/backfill_universe.py','--seed-smallcaps','--only-missing') `
        -WorkingDirectory $root -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $log
    Note ("backfill finished, exit code " + $p.ExitCode)
}
finally {
    Note "restarting the email bot…"
    try { Enable-ScheduledTask -TaskName EquityResearchEmailBot -ErrorAction Stop | Out-Null } catch { Note "enable task: $_" }
    try { Start-ScheduledTask  -TaskName EquityResearchEmailBot -ErrorAction Stop } catch { Note "start task: $_" }
    Note "done."
}
