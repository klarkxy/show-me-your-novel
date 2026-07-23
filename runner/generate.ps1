<#
.DESCRIPTION
  PowerShell wrapper for the autonomous long-form V2 generator.

  Examples:
    .\runner\generate.ps1 -Models deepseek-v4-flash
    .\runner\generate.ps1 -All -DryRun
    .\runner\generate.ps1 -All -NewRun
#>

[CmdletBinding(DefaultParameterSetName = "Models")]
param(
  [Parameter(ParameterSetName = "Models", Mandatory = $true)]
  [string[]]$Models,
  [Parameter(ParameterSetName = "All", Mandatory = $true)]
  [switch]$All,
  [string]$Benchmark = "reform-era",
  [string]$EnvFile = "",
  [string]$ConfigPath = "",
  [ValidatePattern("^(book|macro-outline|opening-outline|chapter:[1-9][0-9]*)$")]
  [string]$StopAfter = "",
  [switch]$DryRun,
  [switch]$NewRun
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ScriptPath = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$RootDir = Split-Path -Parent $ScriptPath

$PythonCommand = $null
$PythonPrefix = @()
foreach ($Candidate in @("python", "py", "python3")) {
  $Found = Get-Command $Candidate -ErrorAction SilentlyContinue
  if (-not $Found) { continue }
  $Prefix = if ($Candidate -eq "py") { @("-3") } else { @() }
  try {
    & $Candidate @Prefix --version *> $null
    if ($LASTEXITCODE -eq 0) {
      $PythonCommand = $Candidate
      $PythonPrefix = $Prefix
      break
    }
  } catch { }
}
if (-not $PythonCommand) {
  throw "未找到可用的 Python 3（已检查 python、py -3、python3）"
}

$CliArgs = @((Join-Path $RootDir "runner\generate.py"), "--benchmark", $Benchmark)
if ($All) {
  $CliArgs += "--all"
} else {
  foreach ($Model in $Models) { $CliArgs += @("--model", $Model) }
}
if ($EnvFile) { $CliArgs += @("--env", $EnvFile) }
if ($ConfigPath) { $CliArgs += @("--config", $ConfigPath) }
if ($StopAfter) { $CliArgs += @("--stop-after", $StopAfter) }
if ($DryRun) { $CliArgs += "--dry-run" }
if ($NewRun) { $CliArgs += "--new-run" }

Push-Location $RootDir
try {
  & $PythonCommand @PythonPrefix @CliArgs
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
