#!/usr/bin/env bash
# ============================================================================
# runner/generate.sh — 遍历 novels/* × config models，缺啥补啥
# ============================================================================
# 对每部小说的每个模型：
#   - 若 novels/<story>/<model.id>.md 已存在 → 跳过
#   - 否则 → 用 OpenCode 在隔离工作目录里生成
#
# 幂等：删除某个 .md 文件后再次运行，会重新生成它。
#
# 用法：
#   bash runner/generate.sh                # 生成全部缺失项
#   bash runner/generate.sh <story-slug>   # 只处理某一部小说
#
# 环境变量（由调用方提供，如 CI secrets 或本地 export）：
#   ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY ...
# ============================================================================

set -euo pipefail

# 切到仓库根目录（脚本可能从任意位置调用）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

CONFIG="${CONFIG:-config.yaml}"
NOVELS_DIR="${NOVELS_DIR:-novels}"
WORK_DIR="${WORK_DIR:-work}"

# 颜色输出（CI 里也无所谓）
if [[ -t 1 ]]; then
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_CYAN=$'\033[36m'
  C_RED=$'\033[31m'; C_DIM=$'\033[2m'; C_RESET=$'\033[0m'
else
  C_GREEN=""; C_YELLOW=""; C_CYAN=""; C_RED=""; C_DIM=""; C_RESET=""
fi

log()  { printf '%s[gen]%s %s\n' "$C_CYAN" "$C_RESET" "$*"; }
ok()   { printf '%s[gen]%s %s%s%s\n' "$C_CYAN" "$C_RESET" "$C_GREEN" "$*" "$C_RESET"; }
skip() { printf '%s[gen]%s %s%s%s\n' "$C_CYAN" "$C_RESET" "$C_YELLOW" "$*" "$C_RESET"; }
err()  { printf '%s[gen]%s %s%s%s\n' "$C_CYAN" "$C_RESET" "$C_RED" "$*" "$C_RESET" >&2; }

# ----------------------------------------------------------------------------
# 依赖检查
# ----------------------------------------------------------------------------
if ! command -v opencode >/dev/null 2>&1; then
  err "未找到 opencode 命令。请先安装：npm install -g opencode-ai"
  exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
  err "未找到配置文件 $CONFIG"
  exit 1
fi

# ----------------------------------------------------------------------------
# 解析 config.yaml —— 提取 model 列表（id 与 model 两列，制表符分隔）
# 依赖 python3（CI runner 自带）；解析失败时给出明确报错。
# ----------------------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  err "需要 python3 来解析 $CONFIG"
  exit 1
fi

read_model_pairs() {
  python3 - "$CONFIG" <<'PY'
import sys, yaml
try:
    cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:
    sys.stderr.write(f"解析 {sys.argv[1]} 失败: {e}\n")
    sys.exit(1)
models = (cfg or {}).get("models") or []
for m in models:
    mid = (m.get("id") or "").strip()
    mdl = (m.get("model") or "").strip()
    name = (m.get("name") or mid).strip()
    if not mid or not mdl:
        sys.stderr.write(f"跳过无效条目（缺少 id 或 model）: {m}\n")
        continue
    # 用 TAB 分隔，name 里若含 TAB 极少见，这里不处理
    sys.stdout.write(f"{mid}\t{mdl}\t{name}\n")
PY
}

# ----------------------------------------------------------------------------
# 收集要处理的小说目录
# ----------------------------------------------------------------------------
target_stories=()
if [[ $# -gt 0 ]]; then
  for s in "$@"; do
    if [[ -f "$NOVELS_DIR/$s/prompt.md" ]]; then
      target_stories+=("$NOVELS_DIR/$s")
    else
      err "未找到小说 $s（应有 $NOVELS_DIR/$s/prompt.md）"
      exit 1
    fi
  done
else
  shopt -s nullglob
  for d in "$NOVELS_DIR"/*/; do
    [[ -f "$d/prompt.md" ]] && target_stories+=("${d%/}")
  done
  shopt -u nullglob
fi

if [[ ${#target_stories[@]} -eq 0 ]]; then
  err "没有找到任何小说（$NOVELS_DIR/*/prompt.md）"
  exit 0
fi

# ----------------------------------------------------------------------------
# 主循环
# ----------------------------------------------------------------------------
total=0; done_count=0; skipped=0; failed=0

# 把 model 列表读进数组（每行 "id<TAB>model<TAB>name"）
mapfile -t MODEL_LINES < <(read_model_pairs)

for story in "${target_stories[@]}"; do
  story_slug="$(basename "$story")"
  prompt_file="$story/prompt.md"
  log "小说：$story_slug"

  for line in "${MODEL_LINES[@]}"; do
    total=$((total + 1))
    IFS=$'\t' read -r model_id model_spec model_name <<<"$line"
    output_file="$story/$model_id.md"
    workdir="$WORK_DIR/$story_slug/$model_id"

    if [[ -f "$output_file" ]]; then
      skip "  └ $model_name → 已存在，跳过 ($output_file)"
      skipped=$((skipped + 1))
      continue
    fi

    log "  └ $model_name → 生成中…（model=$model_spec）"
    mkdir -p "$workdir"

    # OpenCode 非交互模式：
    #   run "<prompt>"            脚本模式，处理完打印结果并退出
    #   --model provider/model    指定模型
    #   --dir <dir>               把工作区限制在隔离目录内，模型间互不可见
    #   --dangerously-skip-permissions  CI/脚本下自动批准（隔离目录内安全）
    set +e
    opencode run \
      --model "$model_spec" \
      --dir "$workdir" \
      --dangerously-skip-permissions \
      "$(cat "$prompt_file")" \
      > "$output_file" 2> "$workdir/.opencode.log"
    rc=$?
    set -e

    if [[ $rc -ne 0 ]] || [[ ! -s "$output_file" ]]; then
      err "  └ $model_name → 生成失败（rc=$rc），详见 $workdir/.opencode.log"
      # 失败时清空半成品，保证下次运行能重试
      rm -f "$output_file"
      failed=$((failed + 1))
      continue
    fi

    ok "  └ $model_name → 完成（$(wc -m < "$output_file" | tr -d ' ') 字符 → $output_file）"
    done_count=$((done_count + 1))
  done
done

# ----------------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------------
log "────────────────────────────────────────"
log "完成：$done_count  跳过：$skipped  失败：$failed  总计：$total"
if [[ $failed -gt 0 ]]; then
  err "有 $failed 个生成失败，请查看 work/*/.opencode.log"
  exit 1
fi
