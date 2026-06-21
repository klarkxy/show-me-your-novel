<#
.DESCRIPTION
  runner/generate.ps1 — 薄包装：调用 runner/generate.py 分章生成小说

  原 opencode CLI 一次性生成逻辑已迁移到 runner/generate.py。
  本脚本保留旧命令行入口与参数名，便于 PowerShell 用户无缝过渡。

  幂等： novels/<story>/<model>.md 已存在且校验通过则跳过；
        加 -Reset 可强制重新生成。

  用法：
    .\runner\generate.ps1                          # 生成全部缺失项
    .\runner\generate.ps1 -Story sci-fi-uplink     # 只处理某一部小说
    .\runner\generate.ps1 -Story sci-fi-uplink,foo # 多部小说
    .\runner\generate.ps1 -Models qwen3.7-max      # 只用指定模型
    .\runner\generate.ps1 -Reset                   # 强制重跑
#>

[CmdletBinding()]
param(
  [string[]]$Story,              # 可选：只处理指定小说 slug
  [string[]]$Models,             # 可选：只用指定模型 id（如 qwen3.7-max,kimi-k2.7-code）
  [string]$EnvFile   = "",       # .env 路径，默认 <repo>/.env
  [string]$ConfigPath = "",      # config.yaml 路径，默认 <repo>/config.yaml
  [string]$NovelsDir = "",       # novels/ 路径，默认 <repo>/novels（透传给 Python）
  [string]$WorkDir   = "",       # work/ 路径，默认 <repo>/work（透传给 Python）
  [switch]$Reset                 # 强制清空中间产物并重新生成
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 定位仓库根目录
$ScriptPath = if ($PSCriptRoot) { $PSCriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$RootDir = Split-Path -Parent $ScriptPath

# 依赖检查
if (-not (Get-Command python3 -ErrorAction SilentlyContinue)) {
  Write-Host "[gen] ✗ 需要 python3" -ForegroundColor Red
  exit 1
}

$ArgsList = @("runner/generate.py")

foreach ($s in $Story) {
  $ArgsList += "--story"
  $ArgsList += $s
}

foreach ($m in $Models) {
  $ArgsList += "--model"
  $ArgsList += $m
}

if ($EnvFile)   { $ArgsList += "--env"; $ArgsList += $EnvFile }
if ($ConfigPath){ $ArgsList += "--config"; $ArgsList += $ConfigPath }
if ($NovelsDir) { $ArgsList += "--novels-dir"; $ArgsList += $NovelsDir }
if ($WorkDir)   { $ArgsList += "--work-dir"; $ArgsList += $WorkDir }
if ($Reset)     { $ArgsList += "--reset" }

Write-Host "[gen] 调用: python3 $([string]::Join(' ', $ArgsList))" -ForegroundColor Cyan
& python3 @ArgsList
exit $LASTEXITCODE
