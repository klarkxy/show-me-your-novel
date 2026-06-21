<#
.DESCRIPTION
  scripts/generate-site.ps1 — 薄包装：调用 scripts/generate_site.py
  等价于 generate-site.sh 的 PowerShell 版。

  产出目录：docs/
    docs/index.html                       首页（小说卡片列表）
    docs/novels/<story>/index.html        小说详情页（prompt + 模型入口）
    docs/novels/<story>/<model>.html      单个模型作品全文页
    docs/assets/style.css                 站点样式（静态，已存在于仓库）

  依赖：python3（pip install pyyaml）。
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptPath = if ($PSCriptRoot) { $PSCriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$RootDir = Split-Path -Parent $ScriptPath

if (-not (Get-Command python3 -ErrorAction SilentlyContinue)) {
  Write-Error "需要 python3 来生成站点（pip install pyyaml）"
  exit 1
}

Write-Host "[site] 调用: python3 scripts/generate_site.py" -ForegroundColor Cyan
& python3 (Join-Path $RootDir "scripts\generate_site.py")
exit $LASTEXITCODE
