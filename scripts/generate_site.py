#!/usr/bin/env python3
"""scripts/generate_site.py — 从 novels/ 生成 GitHub Pages 静态站点

产出目录：docs/
  docs/index.html                       首页（小说卡片列表）
  docs/novels/<story>/index.html        小说详情页（prompt + 模型入口）
  docs/novels/<story>/<model>.html      单个模型作品全文页
  docs/assets/style.css                 站点样式（静态，已存在于仓库）

用法：
  python3 scripts/generate_site.py
  python3 scripts/generate_site.py --config config.yaml
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# 极简 Markdown → HTML（只覆盖小说用到的语法）
# ---------------------------------------------------------------------------
def md_to_html(md: str) -> str:
    if md is None:
        return ""

    # 预处理器：将 [思考过程] 块转换为 <details> HTML
    def _replace_thinking(m):
        content = m.group(1).strip()
        # 内容中的空行转为 <br>，段落保留
        html_content = content.replace("\n\n", "</p><p>").replace("\n", "<br>")
        return (f'<details class="thinking">'
                f'<summary><span class="thinking-icon">🤔</span> 模型思考过程</summary>'
                f'<div class="thinking-content"><p>{html_content}</p></div>'
                f'</details>')
    md = re.sub(
        r'\[思考过程\]\n(.*?)\n\[/思考过程\]',
        _replace_thinking,
        md,
        flags=re.DOTALL,
    )

    lines = md.splitlines()
    out: list[str] = []
    i = 0
    in_ul = False
    in_ol = False

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*", r"<em>\1</em>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        return s

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 第一个 # 视为主标题，已在页头展示，跳过避免重复
        if stripped.startswith("# ") and not out:
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            close_lists()
            level = len(m.group(1))
            text = inline(m.group(2))
            out.append(f"<h{level}>{text}</h{level}>")
            i += 1
            continue

        # 无序列表
        if re.match(r"^[-*]\s+", stripped):
            if not in_ul:
                close_lists()
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline(stripped[2:].strip())}</li>")
            i += 1
            continue

        # 有序列表
        if re.match(r"^\d+\.\s+", stripped):
            if not in_ol:
                close_lists()
                out.append("<ol>")
                in_ol = True
            text = re.sub(r"^\d+\.\s+", "", stripped)
            out.append(f"<li>{inline(text)}</li>")
            i += 1
            continue

        # Raw HTML pass-through（用于 <details>/<summary> 思考块）
        if any(stripped.startswith(t) for t in ("<details", "</details>", "<summary", "</summary>", '<div class="thinking-content"', "</div>")):
            close_lists()
            out.append(line)
            i += 1
            continue

        # 引用
        if stripped.startswith("> "):
            close_lists()
            buf = [stripped[2:]]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("> "):
                buf.append(lines[i].strip()[2:])
                i += 1
            out.append(f"<blockquote>{inline(' '.join(buf))}</blockquote>")
            continue

        # 空行
        if stripped == "":
            close_lists()
            i += 1
            continue

        # 普通段落（合并连续非空行）
        close_lists()
        buf = [stripped]
        i += 1
        while (
            i < len(lines)
            and lines[i].strip() != ""
            and not re.match(
                r"^(#{1,6}\s|[-*]\s|\d+\.\s|>\s)", lines[i].strip()
            )
        ):
            buf.append(lines[i].strip())
            i += 1
        out.append(f"<p>{inline(' '.join(buf))}</p>")

    close_lists()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 元信息提取
# ---------------------------------------------------------------------------
def count_chinese_chars(text: str) -> int:
    return len(re.findall(r"[一-鿿　-〿A-Za-z0-9]", text or ""))


def count_chapters(text: str) -> int:
    return len(re.findall(r"^##\s+第\d+章", text or "", re.M))


def first_h1(text: str) -> str:
    m = re.search(r"^#\s+(.+)$", text or "", re.M)
    return m.group(1).strip() if m else ""


def story_meta(prompt_md: str) -> dict:
    genre = ""
    m = re.search(r"##\s*题材\s*\n+(.+)", prompt_md)
    if m:
        genre = m.group(1).strip().splitlines()[0].strip()
    intro = ""
    m = re.search(
        r"##\s*世界观设定\s*\n+(.*?)(?=\n##\s|\Z)", prompt_md, re.S
    )
    if m:
        paras = [p.strip() for p in m.group(1).split("\n\n") if p.strip()]
        if paras:
            intro = re.sub(r"\s+", " ", paras[0])[:140]
    return {"genre": genre, "intro": intro}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="从 novels/ 生成 GitHub Pages 静态站点"
    )
    parser.add_argument("--novels-dir", default="novels", help="novels/ 目录路径")
    parser.add_argument("--docs-dir", default="docs", help="docs/ 输出目录路径")
    parser.add_argument("--config", default="config.yaml", help="config.yaml 路径")
    args = parser.parse_args()

    root = Path.cwd()
    novels_dir = Path(args.novels_dir)
    if not novels_dir.is_absolute():
        novels_dir = root / novels_dir
    docs_dir = Path(args.docs_dir)
    if not docs_dir.is_absolute():
        docs_dir = root / docs_dir
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path

    if not config_path.exists():
        print(f"[site] 未找到 {config_path}", file=sys.stderr)
        return 1

    with config_path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    models = cfg.get("models") or []
    model_by_id = {m["id"]: m for m in models if m.get("id")}
    model_order = [m["id"] for m in models if m.get("id")]

    # 清掉旧的生成 HTML（保留 assets/）
    index_html_path = docs_dir / "index.html"
    if index_html_path.exists():
        index_html_path.unlink()
    novels_html_dir = docs_dir / "novels"
    if novels_html_dir.exists():
        import shutil

        shutil.rmtree(novels_html_dir)
    novels_html_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "assets").mkdir(exist_ok=True)

    # -----------------------------------------------------------------------
    # 收集小说
    # -----------------------------------------------------------------------
    stories = []
    for name in sorted(os.listdir(novels_dir)):
        sp = novels_dir / name
        if not sp.is_dir():
            continue
        if not (sp / "prompt.md").exists():
            continue
        prompt_md = (sp / "prompt.md").read_text(encoding="utf-8")
        meta = story_meta(prompt_md)
        title = first_h1(prompt_md) or name
        versions = []
        for fname in sorted(os.listdir(sp)):
            if fname == "prompt.md" or not fname.endswith(".md"):
                continue
            mid = fname[: -len(".md")]
            if mid not in model_by_id:
                continue
            content = (sp / fname).read_text(encoding="utf-8")
            st = (sp / fname).stat()
            chapters = count_chapters(content)
            is_partial = chapters < 10
            versions.append(
                {
                    "model_id": mid,
                    "model_name": model_by_id[mid].get("name", mid),
                    "chars": count_chinese_chars(content),
                    "chapters": chapters,
                    "mtime": datetime.fromtimestamp(
                        st.st_mtime, tz=timezone.utc
                    ).isoformat(timespec="seconds"),
                    "content_html": md_to_html(content),
                    "novel_title": first_h1(content) or title,
                    "is_partial": is_partial,
                }
            )
        versions.sort(
            key=lambda v: model_order.index(v["model_id"])
            if v["model_id"] in model_order
            else 999
        )
        stories.append(
            {
                "slug": name,
                "title": title,
                "genre": meta["genre"],
                "intro": meta["intro"],
                "prompt_md": prompt_md,
                "prompt_html": md_to_html(prompt_md),
                "versions": versions,
            }
        )

    # -----------------------------------------------------------------------
    # HTML 模板
    # -----------------------------------------------------------------------
    SITE_TITLE = "Show Me Your Novel"
    SITE_SUB = "读的是小说，写的是代码"
    # 仓库地址：页头 GitHub 按钮、shields.io 徽章、软文里都从这里派生，避免四处散落
    REPO_URL = "https://github.com/klarkxy/show-me-your-novel"
    REPO_OWNER = "klarkxy"
    REPO_NAME = "show-me-your-novel"

    def github_button_html(depth: str) -> str:
        """页头右侧的 GitHub 入口。

        shields.io 徽章自带 Star 数实时刷新：用户看到的是一个会动的数字，比静态文字更能引起点击。
        点击跳到仓库页（不是 /stargazers），让用户自己决定要不要 Star / Fork。
        """
        star_img = (
            f"https://img.shields.io/github/stars/{REPO_OWNER}/{REPO_NAME}"
            f"?style=social"
        )
        return (
            f'<a class="gh-button" href="{REPO_URL}" target="_blank" rel="noopener" '
            f'title="看不上眼就来骂，反正你写不出更好的。">'
            f'<span class="gh-icon" aria-hidden="true">★</span>'
            f'<span class="gh-text">嫌丑？来打脸</span>'
            f'<img class="gh-badge" src="{star_img}" alt="GitHub stars" loading="lazy">'
            f'</a>'
        )

    def page_head(title: str, depth: str, body_class: str = "") -> str:
        body_attr = f' class="{body_class}"' if body_class else ""
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · {SITE_TITLE}</title>
<link rel="stylesheet" href="{depth}assets/style.css">
</head>
<body{body_attr}>
<header class="site-header">
  <div class="header-inner">
    <a class="brand" href="{depth}index.html">{SITE_TITLE}</a>
    <span class="tagline">{html.escape(SITE_SUB)}</span>
    {github_button_html(depth)}
  </div>
</header>
<main class="container">
"""

    PAGE_FOOT = f"""
</main>
<footer class="site-footer">
  <p><a href="{REPO_URL}" target="_blank" rel="noopener">klarkxy/show-me-your-novel</a> · 觉得写得烂就 PR</p>
</footer>
</body>
</html>
"""

    # -----------------------------------------------------------------------
    # 1. 首页
    # -----------------------------------------------------------------------
    cards = []
    for s in stories:
        done = len(s["versions"])
        # 用 model_by_id 的尺寸而不是 cfg.models：避免 config 出现重复 id 时把分母撑大
        total = len(model_by_id)
        badge = (
            "completed"
            if done == total and total
            else ("partial" if done else "pending")
        )
        cards.append(
            f"""<a class="card" href="novels/{html.escape(s['slug'])}/index.html">
  <div class="card-top">
    <span class="card-genre">{html.escape(s['genre'] or '小说')}</span>
    <span class="card-arrow" aria-hidden="true">→</span>
  </div>
  <h2 class="card-title">{html.escape(s['title'])}</h2>
  <p class="card-intro">{html.escape(s['intro'] or '（无简介）')}</p>
  <div class="card-meta">
    <span class="badge badge-{badge}">{done}/{total}</span>
  </div>
</a>"""
        )

    index_content = page_head(SITE_TITLE, "", "page-home") + f"""
<section class="hero">
  <h1 class="hero-title">Talk is cheap.<br>Show me your novel.</h1>
</section>

<section class="story-section">
  <div class="card-grid">
{chr(10).join(cards) if cards else '<p class="empty">小说还没出生。在 <code>novels/&lt;slug&gt;/prompt.md</code> 放一份提示词，自己生。</p>'}
  </div>
</section>
""" + PAGE_FOOT
    index_html_path.write_text(index_content, encoding="utf-8")

    # -----------------------------------------------------------------------
    # 2. 小说详情页 + 模型作品页
    # -----------------------------------------------------------------------
    for s in stories:
        sdir = novels_html_dir / s["slug"]
        sdir.mkdir(exist_ok=True)

        version_cards = []
        for v in s["versions"]:
            if v.get("is_partial"):
                stats = f"{v['chars']} 字 · {v['chapters']}/10 章，跑了"
            else:
                stats = f"{v['chars']} 字 · {v['chapters']} 章"
            version_cards.append(
                f"""<a class="version-card" href="{html.escape(v['model_id'])}.html">
  <div class="vc-name">{html.escape(v['model_name'])}</div>
  <div class="vc-stats">
    <span>{stats}</span>
  </div>
</a>"""
            )
        done_ids = {v["model_id"] for v in s["versions"]}
        for mid in model_order:
            if mid in done_ids:
                continue
            version_cards.append(
                f"""<div class="version-card version-pending">
  <div class="vc-name">{html.escape(model_by_id[mid].get('name', mid))}</div>
  <div class="vc-stats"><span>它还没接活。催它：PR。</span></div>
</div>"""
            )

        detail_content = page_head(s["title"], "../../", "page-detail") + f"""
<a class="back" href="../../index.html">← 滚回去</a>
<header class="story-header">
  <h1 class="story-title">{html.escape(s['title'])}</h1>
</header>

<section class="prompt-section">
  <div class="prompt-body markdown">
{s['prompt_html']}
  </div>
</section>

<section class="versions-section">
  <div class="version-grid">
{chr(10).join(version_cards) if version_cards else '<p class="empty">还没模型敢接这部小说。</p>'}
  </div>
</section>
""" + PAGE_FOOT
        (sdir / "index.html").write_text(detail_content, encoding="utf-8")

        for v in s["versions"]:
            partial_note = ""
            if v.get("is_partial"):
                partial_note = f'''
    <div class="partial-notice">
      ⚠️ {html.escape(v['model_name'])} 写到第 {v['chapters']} 章直接撂挑子。剩下的你接不接？
    </div>'''
            v_content = (
                page_head(
                    f"{s['title']} · {v['model_name']}", "../../", "page-reading"
                )
                + f"""
<a class="back" href="index.html">← 滚回去</a>
<article class="novel">
  <header class="novel-header">
    <p class="novel-model">{html.escape(v['model_name'])}</p>
    <h1 class="novel-title">{html.escape(v['novel_title'])}</h1>
    <div class="novel-meta">
      <span>{v['chars']} 字 · {v['chapters']} 章</span>
      <span class="dim">显卡终于凉下来：{html.escape(v['mtime'])}</span>
    </div>
    {partial_note}
  </header>
  <div class="novel-body markdown">
{v['content_html']}
  </div>
</article>
"""
                + PAGE_FOOT
            )
            (sdir / f"{v['model_id']}.html").write_text(
                v_content, encoding="utf-8"
            )

    print(
        f"[site] 生成完成：{len(stories)} 部小说，输出到 {docs_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
