param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
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
  throw "Python 3 was not found (checked python, py -3, and python3)."
}
Push-Location $RootDir
try {
  & $PythonCommand @PythonPrefix (Join-Path $RootDir "novel.py") @Arguments
  $ExitCode = $LASTEXITCODE
} finally {
  Pop-Location
}
exit $ExitCode
