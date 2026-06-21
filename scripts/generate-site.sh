#!/usr/bin/env bash
# ============================================================================
# scripts/generate-site.sh — 薄包装：调用 scripts/generate_site.py
# ============================================================================
# 产出目录：docs/
#   docs/index.html                       首页（小说卡片列表）
#   docs/novels/<story>/index.html        小说详情页（prompt + 模型入口）
#   docs/novels/<story>/<model>.html      单个模型作品全文页
#   docs/assets/style.css                 站点样式（静态，已存在于仓库）
#
# 依赖：python3（pip install pyyaml）。
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$ROOT_DIR"

python3 scripts/generate_site.py "$@"
