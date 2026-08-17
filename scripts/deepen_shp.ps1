# Deepen SHP history to 16 quarters (~4 yr) for the cost-zone feature.
# DISABLES the email bot (so its keepalive can't relaunch it mid-run), stops it, runs the SHP-only
# backfill (resumable via --skip-deep) BOUNDED to a time limit so a stalled symbol can't run forever,
# then ALWAYS re-enables + restarts the bot. Idempotent/resumable — safe to re-run after any drop.

$ErrorActionPreference = "Continue"
$proj = "D:\Desktop\Projects\aaryan-nakhat-equity-research"
$log  = "$proj\data\processed\deepen_shp.log"
$maxMinutes = 60
function Log($m) { "$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss'))  $m" | Tee-Object -FilePath $log -Append }

function Restore-Bot {
    Log "=== re-enabling + restarting bot ==="
    Enable-ScheduledTask -TaskName "EquityResearchEmailBot" -ErrorAction SilentlyContinue | Out-Null
    Start-ScheduledTask -TaskName "EquityResearchEmailBot" -ErrorAction SilentlyContinue
    Log "=== done ==="
}

try {
    Log "=== deepen SHP: disabling + stopping bot (keepalive must not relaunch it) ==="
    Disable-ScheduledTask -TaskName "EquityResearchEmailBot" -ErrorAction SilentlyContinue | Out-Null
    Stop-ScheduledTask -TaskName "EquityResearchEmailBot" -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
      Where-Object { $_.CommandLine -like '*email_bot*' } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3

    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) { $uv = "$env:USERPROFILE\.local\bin\uv.exe" }
    Log "=== running backfill (SHP only, 16q, resume skip>=12q) via $uv — bounded $maxMinutes min ==="

    $job = Start-Job -ScriptBlock {
        param($uv, $proj, $log)
        Set-Location $proj
        & $uv run python scripts\backfill_universe.py --skip-financials --quarters 16 --skip-deep 12 *>> $log
    } -ArgumentList $uv, $proj, $log

    if (Wait-Job $job -Timeout ($maxMinutes * 60)) {
        Log "=== backfill finished within the time limit ==="
    } else {
        Log "=== backfill exceeded $maxMinutes min — stopping it (resume next run) ==="
        Stop-Job $job -ErrorAction SilentlyContinue
    }
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    # kill any lingering backfill python child so it can't hold the DB lock past the bot restart
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
      Where-Object { $_.CommandLine -like '*backfill_universe*' } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
}
finally {
    Restore-Bot          # ALWAYS bring the bot back, even if the backfill hung/errored
}
