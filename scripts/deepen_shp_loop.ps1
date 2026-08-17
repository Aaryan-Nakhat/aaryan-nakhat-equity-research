# Auto-resuming SHP deepen loop — robust against a flaky network.
# Disables the bot, then repeatedly runs the SHP-only backfill (resumable via --skip-deep); if the
# log stalls (a hung fetch on a network blip), it kills + resumes. Stops when a pass completes with
# no stall (whole universe processed), then re-enables + restarts the bot. PS5.1-safe (no try/finally).

$ErrorActionPreference = "Continue"
$proj = "D:\Desktop\Projects\aaryan-nakhat-equity-research"
$log  = "$proj\data\processed\deepen_shp.log"
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = "$env:USERPROFILE\.local\bin\uv.exe" }
$stallSeconds = 120
$maxCycles = 40
function Log($m) { "$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss'))  LOOP: $m" | Tee-Object -FilePath $log -Append }
function Kill-Backfill {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
      Where-Object { $_.CommandLine -like '*backfill_universe*' } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

Log "disabling + stopping bot"
Disable-ScheduledTask -TaskName "EquityResearchEmailBot" -ErrorAction SilentlyContinue | Out-Null
Stop-ScheduledTask -TaskName "EquityResearchEmailBot" -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*email_bot*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Kill-Backfill
Start-Sleep -Seconds 3

$complete = $false
for ($c = 1; $c -le $maxCycles; $c++) {
    Log "cycle $c - starting backfill (skip-deep 12)"
    $job = Start-Job -ScriptBlock {
        param($uv, $proj, $log)
        Set-Location $proj
        & $uv run python scripts\backfill_universe.py --skip-financials --quarters 16 --skip-deep 12 *>> $log
    } -ArgumentList $uv, $proj, $log

    $stalled = $false
    while ($true) {
        Start-Sleep -Seconds 20
        if ((Get-Job -Id $job.Id).State -ne 'Running') { break }   # completed / failed
        $idle = ((Get-Date) - (Get-Item $log).LastWriteTime).TotalSeconds
        if ($idle -gt $stallSeconds) {
            Log "cycle $c - stalled (idle $([int]$idle)s), killing + resuming"
            $stalled = $true
            Stop-Job $job -ErrorAction SilentlyContinue
            Kill-Backfill
            break
        }
    }
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    Kill-Backfill
    Start-Sleep -Seconds 3
    if (-not $stalled) { Log "cycle $c - backfill completed a full pass"; $complete = $true; break }
}

Log "loop finished (complete=$complete) - re-enabling + restarting bot"
Enable-ScheduledTask -TaskName "EquityResearchEmailBot" -ErrorAction SilentlyContinue | Out-Null
Start-ScheduledTask -TaskName "EquityResearchEmailBot" -ErrorAction SilentlyContinue
Log "=== done (loop) ==="
