# Deepen SHP history to 16 quarters (~4 yr) for the cost-zone feature.
# DISABLES the email bot (so its keepalive can't relaunch it mid-run), stops it, runs the SHP-only
# backfill (resumable via --skip-deep), then re-enables + restarts the bot.
# Idempotent/resumable — safe to re-run after any interruption (it skips names already deepened).

$ErrorActionPreference = "Continue"
$proj = "D:\Desktop\Projects\aaryan-nakhat-equity-research"
$log  = "$proj\data\processed\deepen_shp.log"
function Log($m) { "$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss'))  $m" | Tee-Object -FilePath $log -Append }

Log "=== deepen SHP: disabling + stopping bot (keepalive must not relaunch it) ==="
Disable-ScheduledTask -TaskName "EquityResearchEmailBot" -ErrorAction SilentlyContinue | Out-Null
Stop-ScheduledTask -TaskName "EquityResearchEmailBot" -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*email_bot*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3

Log "=== running backfill (SHP only, 16 quarters, resume: skip symbols already >=12q) ==="
Set-Location $proj
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = "$env:USERPROFILE\.local\bin\uv.exe" }
Log "using uv at: $uv"
& $uv run python scripts\backfill_universe.py --skip-financials --quarters 16 --skip-deep 12 *>> $log
Log "=== backfill exit code: $LASTEXITCODE ==="

Log "=== re-enabling + restarting bot ==="
Enable-ScheduledTask -TaskName "EquityResearchEmailBot" -ErrorAction SilentlyContinue | Out-Null
Start-ScheduledTask -TaskName "EquityResearchEmailBot" -ErrorAction SilentlyContinue
Log "=== done ==="
