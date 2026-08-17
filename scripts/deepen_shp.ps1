# Deepen SHP history to 16 quarters (~4 yr) for the cost-zone feature.
# Stops the email bot (single-writer DuckDB), runs the SHP-only backfill, restarts the bot.
# Run via the DeepenSHPBackfill Scheduled Task. Idempotent/resumable — safe to re-run.

$ErrorActionPreference = "Continue"
$proj = "D:\Desktop\Projects\aaryan-nakhat-equity-research"
$log  = "$proj\data\processed\deepen_shp.log"
function Log($m) { "$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss'))  $m" | Tee-Object -FilePath $log -Append }

Log "=== deepen SHP: stopping bot ==="
Stop-ScheduledTask -TaskName "EquityResearchEmailBot" -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*email_bot*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3

Log "=== running backfill (SHP only, 16 quarters) ==="
Set-Location $proj
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = "$env:USERPROFILE\.local\bin\uv.exe" }
Log "using uv at: $uv"
& $uv run python scripts\backfill_universe.py --skip-financials --quarters 16 *>> $log
Log "=== backfill exit code: $LASTEXITCODE ==="

Log "=== restarting bot ==="
Start-ScheduledTask -TaskName "EquityResearchEmailBot" -ErrorAction SilentlyContinue
Log "=== done ==="
