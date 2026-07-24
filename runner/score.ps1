<#
.SYNOPSIS
  PowerShell wrapper for the Sol/Grok/Kimi V3 multidimensional scorer.
.EXAMPLE
  .\runner\score.ps1 -Model deepseek-v4-flash -Judge sol -DryRun
.EXAMPLE
  .\runner\score.ps1 -All
#>

[CmdletBinding(DefaultParameterSetName = "Model")]
param(
  [Parameter(ParameterSetName = "Model", Mandatory = $true)]
  [string[]]$Model,
  [Parameter(ParameterSetName = "All", Mandatory = $true)]
  [switch]$All,
  [ValidateSet("sol", "grok", "kimi")]
  [string[]]$Judge,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptPath = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$RootDir = Split-Path -Parent $ScriptPath
$PythonScript = Join-Path $RootDir "runner\score.py"

$PythonCommand = $null
$PythonPrefix = @()
foreach ($Candidate in @("python", "py", "python3")) {
  if (-not (Get-Command $Candidate -ErrorAction SilentlyContinue)) { continue }
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

$CliArgs = @($PythonScript)
if ($All) {
  $CliArgs += "--all"
} else {
  foreach ($CandidateModel in $Model) { $CliArgs += @("--model", $CandidateModel) }
}
foreach ($SelectedJudge in $Judge) { $CliArgs += @("--judge", $SelectedJudge) }
if ($DryRun) { $CliArgs += "--dry-run" }

Push-Location $RootDir
try {
  & $PythonCommand @PythonPrefix @CliArgs
  $ExitCode = $LASTEXITCODE
} finally {
  Pop-Location
}
exit $ExitCode
