<#
.SYNOPSIS
  Deterministically builds the V3 dimension leaderboard and Legacy pages.
.EXAMPLE
  .\scripts\generate-site.ps1 -DocsDir .site/preview
#>

[CmdletBinding()]
param(
  [string]$ConfigPath = "",
  [string]$NovelsDir = "",
  [string]$ResultsDir = "",
  [string]$AssetsDir = "",
  [string]$DocsDir = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptPath = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$RootDir = Split-Path -Parent $ScriptPath
$PythonScript = Join-Path $RootDir "scripts\generate_site.py"

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
if ($ConfigPath) { $CliArgs += @("--config", $ConfigPath) }
if ($NovelsDir) { $CliArgs += @("--novels-dir", $NovelsDir) }
if ($ResultsDir) { $CliArgs += @("--results-dir", $ResultsDir) }
if ($AssetsDir) { $CliArgs += @("--assets-dir", $AssetsDir) }
if ($DocsDir) { $CliArgs += @("--docs-dir", $DocsDir) }

Push-Location $RootDir
try {
  & $PythonCommand @PythonPrefix @CliArgs
  $ExitCode = $LASTEXITCODE
} finally {
  Pop-Location
}
exit $ExitCode
