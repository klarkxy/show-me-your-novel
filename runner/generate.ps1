<#
.DESCRIPTION
  runner/generate.ps1 — 遍历 novels/* × config models，缺啥补啥 (PowerShell 版)
  所有模型都走 opencode-go 订阅服务（provider 锁死，见仓库根目录 opencode.json）。
  API key 从 .env 的 OPENCODE_API_KEY 读取并注入到 opencode 进程环境。

  幂等：删除某个 .md 文件后再次运行，会重新生成它。

  用法：
    .\runner\generate.ps1                          # 生成全部缺失项（全部模型）
    .\runner\generate.ps1 -Story sci-fi-uplink     # 只处理某一部小说
    .\runner\generate.ps1 -Story sci-fi-uplink,foo # 多部小说
    .\runner\generate.ps1 -Models qwen3.7-max      # 只用指定模型（逗号分隔）
#>

[CmdletBinding()]
param(
  [string[]]$Story,              # 可选：只处理指定小说 slug
  [string[]]$Models,             # 可选：只用指定模型 id（如 qwen3.7-max,kimi-k2.7-code）
  [string]$EnvFile   = "",       # .env 路径，默认 <repo>/.env
  [string]$ConfigPath = "",      # config.yaml 路径，默认 <repo>/config.yaml
  [string]$NovelsDir = "",       # novels/ 路径，默认 <repo>/novels
  [string]$WorkDir   = ""        # work/ 路径，默认 <repo>/work
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Write-Log   { Write-Host "[gen] $args" -ForegroundColor Cyan }
function Write-Ok     { Write-Host "[gen] $args" -ForegroundColor Green }
function Write-Skip   { Write-Host "[gen] $args" -ForegroundColor Yellow }
function Write-Err    { Write-Host "[gen] $args" -ForegroundColor Red }

# ---------------------------------------------------------------------------
# 定位仓库根目录（脚本可能从任意位置调用）
# ---------------------------------------------------------------------------
$ScriptPath = if ($PSCriptRoot) { $PSCriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$RootDir = Split-Path -Parent $ScriptPath  # runner/.. → 根目录

if (-not $EnvFile)     { $EnvFile     = Join-Path $RootDir ".env" }
if (-not $ConfigPath)  { $ConfigPath  = Join-Path $RootDir "config.yaml" }
if (-not $NovelsDir)   { $NovelsDir   = Join-Path $RootDir "novels" }
if (-not $WorkDir)     { $WorkDir     = Join-Path $RootDir "work" }

# ---------------------------------------------------------------------------
# 依赖检查
# ---------------------------------------------------------------------------
if (-not (Get-Command opencode -ErrorAction SilentlyContinue)) {
  Write-Err "未找到 opencode 命令。请先安装：npm install -g opencode-ai"
  exit 1
}
if (-not (Get-Command python3 -ErrorAction SilentlyContinue)) {
  Write-Err "需要 python3 来解析 $ConfigPath（pip install pyyaml）"
  exit 1
}
if (-not (Test-Path $ConfigPath)) { Write-Err "未找到配置文件 $ConfigPath"; exit 1 }
if (-not (Test-Path $EnvFile))    { Write-Err "未找到 .env（$EnvFile）。请创建并写入 OPENCODE_API_KEY=sk-..."; exit 1 }
if (-not (Test-Path (Join-Path $RootDir "opencode.json"))) {
  Write-Err "未找到 opencode.json —— 它把 opencode-go 的 key 指向 {env:OPENCODE_API_KEY}，缺少它会被本机 auth.json 里的旧 key 覆盖。"
  exit 1
}

# ---------------------------------------------------------------------------
# 读取 .env —— 提取 OPENCODE_API_KEY
# 支持 KEY=VALUE 与 KEY: VALUE 两种写法；只取第一个匹配项。
# ---------------------------------------------------------------------------
$apiKey = $null
foreach ($line in (Get-Content $EnvFile -Encoding UTF8)) {
  if ($line -match '^\s*OPENCODE_API_KEY\s*[:=]\s*(\S+)\s*$') { $apiKey = $Matches[1]; break }
}
if (-not $apiKey) { Write-Err ".env 里没有 OPENCODE_API_KEY（应形如 OPENCODE_API_KEY=sk-...）"; exit 1 }
$env:OPENCODE_API_KEY = $apiKey
Write-Log "已从 .env 注入 OPENCODE_API_KEY（前缀 $($apiKey.Substring(0,[Math]::Min(6,$apiKey.Length)))...）"

# ---------------------------------------------------------------------------
# 解析 config.yaml —— 提取 model 列表（id / model / name，制表符分隔）
# 依赖 python3 + pyyaml。解析失败时给出明确报错。
# 注意：PowerShell 里不能像 bash 那样用 here-string 喂 stdin（python3 会进 REPL），
#       所以把脚本写到临时 .py 文件再执行。
# ---------------------------------------------------------------------------
$pyReadModels = @'
import sys, yaml
try:
    cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:
    sys.stderr.write(f"解析 {sys.argv[1]} 失败: {e}\n"); sys.exit(1)
models = (cfg or {}).get("models") or []
for m in models:
    mid  = (m.get("id") or "").strip()
    mdl  = (m.get("model") or "").strip()
    name = (m.get("name") or mid).strip()
    if not mid or not mdl:
        sys.stderr.write(f"跳过无效条目（缺少 id 或 model）: {m}\n"); continue
    sys.stdout.write(f"{mid}\t{mdl}\t{name}\n")
'@
function Read-ModelPairs {
  param([string]$ConfigFile)
  $pyFile = Join-Path $env:TEMP "show-me-your-novel-readmodels.py"
  [System.IO.File]::WriteAllText($pyFile, $pyReadModels, [System.Text.Encoding]::UTF8)
  $result = & python3 $pyFile $ConfigFile
  $rc = $LASTEXITCODE
  Remove-Item $pyFile -ErrorAction SilentlyContinue
  if ($rc -ne 0) { Write-Err "解析 config.yaml 失败（python3 退出 $rc）"; exit 1 }
  return ($result | Where-Object { $_ -ne "" })
}

# ---------------------------------------------------------------------------
# 收集要处理的小说目录
# ---------------------------------------------------------------------------
$storyDirs = @()
if ($Story) {
  foreach ($s in $Story) {
    $dir = Join-Path $NovelsDir $s
    if (Test-Path (Join-Path $dir "prompt.md")) { $storyDirs += $dir }
    else { Write-Err "未找到小说 $s（应有 $dir/prompt.md）"; exit 1 }
  }
} else {
  if (Test-Path $NovelsDir) {
    Get-ChildItem -Directory $NovelsDir | ForEach-Object {
      if (Test-Path (Join-Path $_.FullName "prompt.md")) { $storyDirs += $_.FullName }
    }
  }
}
if ($storyDirs.Count -eq 0) { Write-Err "没有找到任何小说（$NovelsDir/*/prompt.md）"; exit 0 }

# ---------------------------------------------------------------------------
# 读取模型列表；-Models 可过滤子集
# ---------------------------------------------------------------------------
$modelObjs = @()
foreach ($line in (Read-ModelPairs -ConfigFile $ConfigPath)) {
  $parts = $line -split "`t"
  $id, $spec = $parts[0], $parts[1]
  $name = if ($parts.Count -ge 3) { $parts[2] } else { $id }
  if ($Models -and ($Models -notcontains $id)) { continue }
  $modelObjs += [pscustomobject]@{ id=$id; spec=$spec; name=$name }
}
if ($modelObjs.Count -eq 0) {
  Write-Err "config.yaml 里没有可用模型$(if($Models){'（-Models 过滤后为空）'})"; exit 1
}

# provider 前缀锁死为 opencode-go（见仓库根目录 opencode.json）
$PROVIDER = "opencode-go"

# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
$total = 0; $doneCount = 0; $skipped = 0; $failed = 0
$failures = @()

# 临时目录放每个模型的 stdout/stderr 重定向文件
$tmpDir = Join-Path $WorkDir ".tmp"
$null = New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null

foreach ($storyDir in $storyDirs) {
  $storySlug = Split-Path -Leaf $storyDir
  $promptFile = Join-Path $storyDir "prompt.md"
  Write-Log "小说：$storySlug"

  foreach ($m in $modelObjs) {
    $total++
    $outputFile = Join-Path $storyDir "$($m.id).md"
    $sandbox    = Join-Path $WorkDir $storySlug $m.id
    $fullModel  = "${PROVIDER}/$($m.spec)"  # → opencode-go/<model>

    if (Test-Path $outputFile) {
      Write-Skip "  └ $($m.name) → 已存在，跳过"
      $skipped++; continue
    }

    Write-Log "  └ $($m.name) → 生成中…（model=$fullModel）"
    $null = New-Item -ItemType Directory -Force -Path $sandbox | Out-Null

    $prompt = Get-Content $promptFile -Raw -Encoding UTF8
    # 让模型在当前目录自由写，脚本之后会复制出去
    $prompt = "$prompt`n`n在当前目录下写小说正文，文件名任意。"
    $outFile = Join-Path $tmpDir "$storySlug__$($m.id).out"
    $errFile = Join-Path $tmpDir "$storySlug__$($m.id).err"
    foreach ($f in @($outFile,$errFile)) { if (Test-Path $f) { Remove-Item $f } }

    # Windows 命令行长度上限约 8191 字符；prompt 被嵌入带引号的命令行里，
    # 若含字面双引号会破坏 cmd 解析、或过长会截断。这里做一道防护。
    if ($prompt.Contains('"')) {
      Write-Err "  └ $($m.name) → prompt 含双引号，无法安全嵌入命令行，跳过（请改写 prompt 或换 bash runner）"
      $failed++; $failures += "$($m.name) [$storySlug, prompt含双引号]"; continue
    }
    if ($prompt.Length -gt 7000) {
      Write-Err "  └ $($m.name) → prompt 过长（$($prompt.Length) 字符），超出 Windows 命令行承载，跳过"
      $failed++; $failures += "$($m.name) [$storySlug, prompt过长]"; continue
    }

    # 模型在自己的 work 目录里写，写完再复制出来，防止它看到其他模型的输出。
    # prompt 已经作为消息传入，模型不需要读 prompt.md。
    $cmdLine = '/c opencode run --model "' + $fullModel + '" --dir "' + $sandbox + '" "' + $prompt + '"'
    $p = Start-Process -FilePath "cmd.exe" `
      -ArgumentList $cmdLine `
      -RedirectStandardOutput $outFile -RedirectStandardError $errFile `
      -NoNewWindow -Wait -PassThru
    $rc = $p.ExitCode
    $stdout = if (Test-Path $outFile) { [System.IO.File]::ReadAllText($outFile, [System.Text.Encoding]::UTF8) } else { "" }
    $stderr = if (Test-Path $errFile) { [System.IO.File]::ReadAllText($errFile, [System.Text.Encoding]::UTF8) } else { "" }

    # 保留完整日志到 sandbox：工具调用过程（stderr）+ 模型对话（stdout）+ 发送的 prompt
    $convLog = Join-Path $sandbox ".conversation.log"
    [System.IO.File]::WriteAllText($convLog, $stdout, [System.Text.Encoding]::UTF8)
    $toolLog = Join-Path $sandbox ".opencode.log"
    [System.IO.File]::WriteAllText($toolLog, $stderr, [System.Text.Encoding]::UTF8)
    $promptLog = Join-Path $sandbox ".prompt.txt"
    [System.IO.File]::WriteAllText($promptLog, $prompt, [System.Text.Encoding]::UTF8)

    if ($rc -ne 0) {
      Write-Err "  └ $($m.name) → 生成失败（rc=$rc）"
      $errShort = ($stderr -replace '\x1b\[[0-9;]*m','') -replace '\s+',' '
      if ($errShort.Length -gt 240) { $errShort = $errShort.Substring(0,240) + "…" }
      if ($errShort) { Write-Err "      错误摘要：$errShort" }
      Write-Err "      日志目录：$sandbox"
      $failed++; $failures += "$($m.name) [$storySlug]"; continue
    }

    # 从 workdir 找模型写的 .md 文件，复制到 novels 目录
    $candidates = Get-ChildItem -Path $sandbox -Filter "*.md" -File -Recurse -ErrorAction SilentlyContinue |
                  Sort-Object Length -Descending
    if ($candidates) {
      $src = $candidates[0]
      Copy-Item -Path $src.FullName -Destination $outputFile -Force
      Write-Log "  └ 从 $($src.Name) 复制到 $($m.id).md"
    }

    if (-not (Test-Path $outputFile)) {
      Write-Err "  └ $($m.name) → 模型未产出 .md 文件（work 目录无生成结果）"
      Write-Err "      日志目录：$sandbox"
      $failed++; $failures += "$($m.name) [$storySlug]"; continue
    }

    $fileSize = (Get-Item $outputFile).Length
    if ($fileSize -lt 500) {
      Write-Err "  └ $($m.name) → 输出过短（$fileSize 字节），可能只含对话摘要而非小说正文"
      Write-Err "      日志目录：$sandbox"
      Remove-Item -Path $outputFile -ErrorAction SilentlyContinue
      $failed++; $failures += "$($m.name) [$storySlug]"; continue
    }

    Write-Ok "  └ $($m.name) → 完成（$fileSize 字节）"
    $doneCount++
  }
}

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
Write-Log "────────────────────────────────────────"
Write-Log "完成：$doneCount  跳过：$skipped  失败：$failed  总计：$total"
if ($failed -gt 0) {
  Write-Err "失败列表：$($failures -join '、')"
  Write-Err "请查看 work/<slug>/<model>/ 目录下的 .opencode.log 和 .conversation.log。"
  exit 1
}
