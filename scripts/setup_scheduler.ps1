<#
  Register (or remove) a Windows Task Scheduler job that runs the pipeline on a throttled cadence.
  This is the safe version of "24/7 while power is on": small batches every few hours, NOT a hot loop.

  Enable  (every 6h, with collection):   .\setup_scheduler.ps1
  Enable  (custom interval):             .\setup_scheduler.ps1 -IntervalHours 4
  Enable  (enrich+qualify only, no new scraping):  .\setup_scheduler.ps1 -NoCollect
  Disable/remove:                        .\setup_scheduler.ps1 -Remove

  It runs:  run_pipeline.py [--collect]   using the shared wf3_python\.venv Python.
#>
param(
    [int]$IntervalHours = 6,
    [switch]$NoCollect,
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$base = $PSScriptRoot                       # scripts/ (this file's folder — holds run_pipeline.py)
$root = Split-Path $PSScriptRoot -Parent    # project root (holds wf3_python\.venv)
$taskName = "GranjurPipeline"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$taskName'."
    return
}

$py = Join-Path $root "wf3_python\.venv\Scripts\python.exe"
$script = Join-Path $base "run_pipeline.py"
if (-not (Test-Path $py)) { throw "venv Python not found at $py — create it first (see Guide.md)." }

$argline = if ($NoCollect) { "`"$script`"" } else { "`"$script`" --collect" }
$action = New-ScheduledTaskAction -Execute $py -Argument $argline -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(3) `
    -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Granjur B2B pipeline: collect -> enrich -> qualify (throttled, every $IntervalHours h)" `
    -Force | Out-Null

Write-Host "Registered '$taskName' to run every $IntervalHours h (first run ~3 min from now)."
Write-Host "Watch it in Task Scheduler, or on the dashboard. Remove with:  .\setup_scheduler.ps1 -Remove"
