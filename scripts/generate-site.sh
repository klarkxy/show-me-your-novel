#!/usr/bin/env bash
# ============================================================================
# scripts/generate-site.sh — 从 novels/ 生成 GitHub Pages 静态站点
# ============================================================================
# 产出目录：docs/
#   docs/index.html                       首页（小说卡片列表）
#   docs/novels/<story>/index.html        小说详情页（prompt + 模型入口）
#   docs/novels/<story>/<model>.html      单个模型作品全文页
#   docs/assets/style.css                 站点样式（静态，已存在于仓库）
#
# 依赖：python3（CI runner 自带）。Markdown→HTML 用内置极简渲染，
#       只处理小说用到的语法（标题、段落、列表、加粗、引用）。
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

CONFIG="${CONFIG:-config.yaml}"
NOVELS_DIR="${NOVELS_DIR:-novels}"
DOCS_DIR="${DOCS_DIR:-docs}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 python3 来生成站点" >&2; exit 1
fi
if [[ ! -f "$CONFIG" ]]; then
  echo "未找到 $CONFIG" >&2; exit 1
fi

# 清掉旧的生成 HTML（保留 assets/）
rm -f "$DOCS_DIR/index.html"
rm -rf "$DOCS_DIR/novels"
mkdir -p "$DOCS_DIR/novels" "$DOCS_DIR/assets"

# 把所有逻辑交给一个 python 脚本，避免 bash 里拼 HTML 的脆弱性
python3 - "$ROOT_DIR" "$CONFIG" "$NOVELS_DIR" "$DOCS_DIR" <<'PY'
import sys, os, re, html, yaml, datetime

ROOT, CONFIG, NOVELS_DIR, DOCS_DIR = sys.argv[1:5]

# --------------------------------------------------------------------------
# 极简 Markdown → HTML（只覆盖小说用到的语法）
# --------------------------------------------------------------------------
def md_to_html(md: str) -> str:
    if md is None:
        return ""
    lines = md.splitlines()
    out = []
    i = 0
    in_ul = False
    in_ol = False

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>"); in_ul = False
        if in_ol:
            out.append("</ol>"); in_ol = False

    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*", r"<em>\1</em>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        return s

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 章节标题：## 第X章 ... → 作为正文标题渲染
        if stripped.startswith("# ") and out == [] :
            # 文档第一个 # 视为主标题，已在页头展示，这里跳过避免重复
            i += 1; continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            close_lists()
            level = len(m.group(1))
            text = inline(m.group(2))
            out.append(f"<h{level}>{text}</h{level}>")
            i += 1; continue

        # 无序列表
        if re.match(r"^[-*]\s+", stripped):
            if not in_ul:
                close_lists(); out.append("<ul>"); in_ul = True
            out.append(f"<li>{inline(stripped[2:].strip())}</li>")
            i += 1; continue

        # 有序列表
        if re.match(r"^\d+\.\s+", stripped):
            if not in_ol:
                close_lists(); out.append("<ol>"); in_ol = True
            text = re.sub(r"^\d+\.\s+", "", stripped)
            out.append(f"<li>{inline(text)}</li>")
            i += 1; continue

        # 引用
        if stripped.startswith("> "):
            close_lists()
            buf = [stripped[2:]]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("> "):
                buf.append(lines[i].strip()[2:]); i += 1
            out.append(f"<blockquote>{inline(' '.join(buf))}</blockquote>")
            continue

        # 空行
        if stripped == "":
            close_lists()
            i += 1; continue

        # 普通段落（合并连续非空行）
        close_lists()
        buf = [stripped]
        i += 1
        while i < len(lines) and lines[i].strip() != "" \
              and not re.match(r"^(#{1,6}\s|[-*]\s|\d+\.\s|>\s)", lines[i].strip()):
            buf.append(lines[i].strip()); i += 1
        out.append(f"<p>{inline(' '.join(buf))}</p>")

    close_lists()
    return "\n".join(out)

# --------------------------------------------------------------------------
# 元信息提取
# --------------------------------------------------------------------------
def count_chinese_chars(text: str) -> int:
    # 统计 CJK 字符 + 字母数字，作为"字数"近似
    return len(re.findall(r"[一-鿿　-〿A-Za-z0-9]", text or ""))

def count_chapters(text: str) -> int:
    return len(re.findall(r"^##\s+第\d+章", text or "", re.M))

def first_h1(text: str) -> str:
    m = re.search(r"^#\s+(.+)$", text or "", re.M)
    return m.group(1).strip() if m else ""

def story_meta(prompt_md: str) -> dict:
    """从 prompt.md 里抽一个简介（第一个 ## 段落后的第一段非空文本）。"""
    genre = ""
    m = re.search(r"##\s*题材\s*\n+(.+)", prompt_md)
    if m:
        genre = m.group(1).strip().splitlines()[0].strip()
    # 简介取「世界观设定」段第一段
    intro = ""
    m = re.search(r"##\s*世界观设定\s*\n+(.*?)(?=\n##\s|\Z)", prompt_md, re.S)
    if m:
        paras = [p.strip() for p in m.group(1).split("\n\n") if p.strip()]
        if paras:
            intro = re.sub(r"\s+", " ", paras[0])[:140]
    return {"genre": genre, "intro": intro}

# --------------------------------------------------------------------------
# 模型配置
# --------------------------------------------------------------------------
with open(CONFIG, encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}
MODELS = cfg.get("models") or []
MODEL_BY_ID = {m["id"]: m for m in MODELS if m.get("id")}
MODEL_ORDER = [m["id"] for m in MODELS if m.get("id")]

# --------------------------------------------------------------------------
# 收集小说
# --------------------------------------------------------------------------
stories = []
for name in sorted(os.listdir(NOVELS_DIR)):
    sp = os.path.join(NOVELS_DIR, name)
    if not os.path.isdir(sp):
        continue
    if not os.path.isfile(os.path.join(sp, "prompt.md")):
        continue
    prompt_md = open(os.path.join(sp, "prompt.md"), encoding="utf-8").read()
    meta = story_meta(prompt_md)
    title = first_h1(prompt_md) or name
    versions = []
    for fname in sorted(os.listdir(sp)):
        if fname == "prompt.md" or not fname.endswith(".md"):
            continue
        mid = fname[:-3]
        if mid not in MODEL_BY_ID:
            continue
        content = open(os.path.join(sp, fname), encoding="utf-8").read()
        st = os.stat(os.path.join(sp, fname))
        versions.append({
            "model_id": mid,
            "model_name": MODEL_BY_ID[mid].get("name", mid),
            "chars": count_chinese_chars(content),
            "chapters": count_chapters(content),
            "mtime": datetime.datetime.fromtimestamp(st.st_mtime, tz=datetime.timezone.utc).isoformat(timespec="seconds"),
            "content_html": md_to_html(content),
            "novel_title": first_h1(content) or title,
        })
    versions.sort(key=lambda v: MODEL_ORDER.index(v["model_id"]) if v["model_id"] in MODEL_ORDER else 999)
    stories.append({
        "slug": name,
        "title": title,
        "genre": meta["genre"],
        "intro": meta["intro"],
        "prompt_md": prompt_md,
        "prompt_html": md_to_html(prompt_md),
        "versions": versions,
    })

# --------------------------------------------------------------------------
# HTML 模板
# --------------------------------------------------------------------------
SITE_TITLE = "Show Me Your Novel"
SITE_SUB = "同一个提示词，不同模型写的小说"

def head(title: str, depth: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · {SITE_TITLE}</title>
<link rel="stylesheet" href="{depth}assets/style.css">
</head>
<body>
<header class="site-header">
  <a class="brand" href="{depth}index.html">{SITE_TITLE}</a>
  <span class="tagline">{html.escape(SITE_SUB)}</span>
</header>
<main class="container">
"""

FOOT = """
</main>
<footer class="site-footer">
  <p>由 <code>show-me-your-novel</code> 生成 · 统一提示词 + OpenCode CLI · 多模型对比</p>
</footer>
</body>
</html>
"""

# --------------------------------------------------------------------------
# 1. 首页
# --------------------------------------------------------------------------
cards = []
for s in stories:
    done = len(s["versions"])
    total = len(MODELS)
    badge = "completed" if done == total and total else ("partial" if done else "pending")
    cards.append(f"""<a class="card" href="novels/{html.escape(s['slug'])}/index.html">
  <div class="card-genre">{html.escape(s['genre'] or '小说')}</div>
  <h2 class="card-title">{html.escape(s['title'])}</h2>
  <p class="card-intro">{html.escape(s['intro'] or '（无简介）')}</p>
  <div class="card-meta">
    <span class="badge badge-{badge}">{done}/{total} 模型</span>
  </div>
</a>""")

index_html = head(SITE_TITLE, "") + f"""
<h1 class="page-title">小说列表</h1>
<p class="page-desc">每一部小说用同一份提示词，交给不同的模型去写，看看各自的笔法。</p>
<div class="card-grid">
{chr(10).join(cards) if cards else '<p class="empty">还没有小说。在 <code>novels/&lt;slug&gt;/prompt.md</code> 放一份提示词，然后运行 <code>bash runner/generate.sh</code>。</p>'}
</div>
""" + FOOT
open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8").write(index_html)

# --------------------------------------------------------------------------
# 2. 小说详情页 + 模型作品页
# --------------------------------------------------------------------------
for s in stories:
    sdir = os.path.join(DOCS_DIR, "novels", s["slug"])
    os.makedirs(sdir, exist_ok=True)

    # 模型入口卡片
    version_cards = []
    for v in s["versions"]:
        version_cards.append(f"""<a class="version-card" href="{html.escape(v['model_id'])}.html">
  <div class="vc-name">{html.escape(v['model_name'])}</div>
  <div class="vc-stats">
    <span>{v['chars']} 字</span>
    <span>{v['chapters']} 章</span>
  </div>
</a>""")
    # 还未生成的模型也显示出来（灰色）
    done_ids = {v["model_id"] for v in s["versions"]}
    for mid in MODEL_ORDER:
        if mid in done_ids:
            continue
        version_cards.append(f"""<div class="version-card version-pending">
  <div class="vc-name">{html.escape(MODEL_BY_ID[mid].get('name', mid))}</div>
  <div class="vc-stats"><span>待生成</span></div>
</a>""")

    detail_html = head(s["title"], "../../") + f"""
<a class="back" href="../../index.html">← 返回小说列表</a>
<h1 class="page-title">{html.escape(s['title'])}</h1>
<div class="story-meta-row">
  <span class="badge badge-genre">{html.escape(s['genre'] or '小说')}</span>
  <span class="story-count">{len(s['versions'])}/{len(MODELS)} 个模型已完成</span>
</div>

<section class="prompt-section">
  <h2 class="section-title">提示词</h2>
  <div class="prompt-body markdown">
{s['prompt_html']}
  </div>
</section>

<section class="versions-section">
  <h2 class="section-title">各模型作品</h2>
  <div class="version-grid">
{chr(10).join(version_cards) if version_cards else '<p class="empty">还没有模型生成这部小说。</p>'}
  </div>
</section>
""" + FOOT
    open(os.path.join(sdir, "index.html"), "w", encoding="utf-8").write(detail_html)

    # 单个模型作品页
    for v in s["versions"]:
        vhtml = head(f"{s['title']} · {v['model_name']}", "../") + f"""
<a class="back" href="index.html">← 返回《{html.escape(s['title'])}》</a>
<article class="novel">
  <header class="novel-header">
    <h1 class="novel-title">{html.escape(v['novel_title'])}</h1>
    <div class="novel-meta">
      <span class="badge badge-genre">{html.escape(s['genre'] or '小说')}</span>
      <span>模型：{html.escape(v['model_name'])}</span>
      <span>{v['chars']} 字 · {v['chapters']} 章</span>
      <span class="dim">生成于 {html.escape(v['mtime'])}</span>
    </div>
  </header>
  <div class="novel-body markdown">
{v['content_html']}
  </div>
</article>
""" + FOOT
        open(os.path.join(sdir, f"{v['model_id']}.html"), "w", encoding="utf-8").write(vhtml)

print(f"[site] 生成完成：{len(stories)} 部小说，输出到 {DOCS_DIR}")
PY
