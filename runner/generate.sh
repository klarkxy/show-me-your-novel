#!/usr/bin/env bash
# ============================================================================
# runner/generate.sh — 薄包装：调用 runner/generate.py 分章生成小说
# ============================================================================
# 原 opencode CLI 一次性生成逻辑已迁移到 runner/generate.py。
# 本脚本保留旧命令行入口，便于 CI 与本地习惯无缝过渡。
#
# 用法：
#   bash runner/generate.sh                # 生成全部缺失项
#   bash runner/generate.sh <story-slug>   # 只处理某一部小说
#   bash runner/generate.sh <slug> --reset # 强制重新生成
#
# 环境变量：
#   OPENCODE_API_KEY 等，见 config.yaml 中 providers 的 api_key_env。
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "[gen] ✗ 需要 $PYTHON" >&2
  exit 1
fi

# 位置参数视为 story slug；其余参数（如 --reset）透传给 Python 脚本
STORIES=()
PASS_THROUGH=()
for arg in "$@"; do
  if [[ "$arg" == -* ]]; then
    PASS_THROUGH+=("$arg")
  else
    STORIES+=("$arg")
  fi
done

ARGS=("$PYTHON" "runner/generate.py")
if [[ ${#STORIES[@]} -gt 0 ]]; then
  for s in "${STORIES[@]}"; do
    ARGS+=("--story" "$s")
  done
fi
ARGS+=("${PASS_THROUGH[@]}")

exec "${ARGS[@]}"
