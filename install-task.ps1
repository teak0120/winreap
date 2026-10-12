<#
.SYNOPSIS
  Register an hourly Task Scheduler task that runs "winreap.py --kill --quiet" as the current user.
.PARAMETER Python
  python.exe to use (pythonw.exe next to it is preferred so no console window flashes).
.PARAMETER Config
  Optional JSON config passed as --config.
.PARAMETER TaskName
  Task name (default "winreap").
#>
param(
    [string]$Python = 'python',
    [string]$Config = '',
    [string]$TaskName = 'winreap'
)
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'winreap.py'
if (-not (Test-Path -LiteralPath $script)) { throw "winreap.py not found next to this script: $script" }
$py = (Get-Command $Python -ErrorAction Stop).Source
$pyw = Join-Path (Split-Path -Parent $py) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pyw)) { $pyw = $py }
$arg = '-B "' + $script + '" --kill --quiet'
if ($Config) { $arg += ' --config "' + (Resolve-Path -LiteralPath $Config).Path + '"' }
$action = New-ScheduledTaskAction -Execute $pyw -Argument $arg
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Registered task '$TaskName': hourly, first run in 5 minutes."
Write-Host "  $pyw $arg"
