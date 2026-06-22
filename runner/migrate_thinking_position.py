#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runner/migrate_thinking_position.py — 把已生成小说中「章末」的思考过程
搬到「章题下、正文前」，与新的 merge_chapters 输出保持一致。

对 novels/<story>/<model>.md：
- 在每个 "## 第N章" 节内，若该节末尾存在 "[思考过程]...[/思考过程]"，
  则把它移到该节首行（章题）之后、正文之前。
- 不修改文件中的正文文字，只调整 [思考过程] 块的位置。
- 自动跳过不含思考过程的文件。
- 用法：python3 runner/migrate_thinking_position.py [novel_slug ...]
        不传参则处理 novels/ 下所有含 <model>.md 的目录
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NOVELS_DIR = REPO_ROOT / "novels"

CHAPTER_RE = re.compile(r"^##\s+第\d+章", re.MULTILINE)
THINK_BLOCK_RE = re.compile(
    r"\n*\[思考过程\]\s*\n.*?\n\[/思考过程\]\s*$",
    re.DOTALL,
)


def find_repo_root() -> Path:
    return REPO_ROOT


def migrate_file(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")

    # 找出所有章节标题位置
    matches = list(CHAPTER_RE.finditer(text))
    if not matches:
        return False

    # 是否存在任意思考过程块
    if "[思考过程]" not in text or "[/思考过程]" not in text:
        return False

    new_parts: list[str] = []
    last_end = 0
    changed = False

    for idx, m in enumerate(matches):
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)

        # 节内内容 = start..end
        section = text[start:end]

        # 提取章末的 [思考过程]...[/思考过程]（必须在节末，且不跨过下一个标题）
        think_m = THINK_BLOCK_RE.search(section)
        if not think_m:
            new_parts.append(text[last_end:start])
            new_parts.append(section)
            last_end = end
            continue

        # 切分：标题行、思考块、剩余正文（标题与正文之间的内容）
        # section 形如：## 第1章 标题\n\n[正文...]\n\n[思考过程]\n...思考文本...\n[/思考过程]
        # 先去掉末尾的思考块
        section_without_think = section[: think_m.start()].rstrip() + "\n"

        # 找到章题行结束位置
        first_nl = section_without_think.find("\n")
        if first_nl < 0:
            title_line = section_without_think
            body = ""
        else:
            title_line = section_without_think[:first_nl]
            body = section_without_think[first_nl + 1 :]

        thinking_text = think_m.group(0)
        # 把 "[思考过程]" 块的内容剥出来（去掉外层围栏 + 收尾换行）
        inner = re.search(
            r"\[思考过程\]\s*\n(.*?)\n\[/思考过程\]",
            thinking_text,
            re.DOTALL,
        )
        inner_text = inner.group(1).strip() if inner else ""

        rebuilt_parts = [text[last_end:start], title_line]
        if inner_text:
            rebuilt_parts.append("")
            rebuilt_parts.append("[思考过程]")
            rebuilt_parts.append(inner_text)
            rebuilt_parts.append("[/思考过程]")
        if body.strip():
            rebuilt_parts.append("")
            rebuilt_parts.append(body.rstrip())

        new_parts.append("\n".join(rebuilt_parts))
        last_end = end
        changed = True

    new_parts.append(text[last_end:])

    new_text = "".join(new_parts)
    if not changed:
        return False
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    targets: list[Path] = []
    if args:
        for slug in args:
            d = NOVELS_DIR / slug
            if not d.is_dir():
                print(f"[skip] 未找到目录：{d}")
                continue
            targets.append(d)
    else:
        if not NOVELS_DIR.is_dir():
            print(f"[err] 未找到 {NOVELS_DIR}")
            return 1
        targets = [d for d in sorted(NOVELS_DIR.iterdir()) if d.is_dir()]

    if not targets:
        print("[err] 没有可处理的小说目录")
        return 1

    total_changed = 0
    total_files = 0
    for story_dir in targets:
        for md in sorted(story_dir.glob("*.md")):
            if md.name == "prompt.md":
                continue
            total_files += 1
            if migrate_file(md):
                print(f"[migrated] {md.relative_to(REPO_ROOT)}")
                total_changed += 1
            else:
                print(f"[skip]     {md.relative_to(REPO_ROOT)}")

    print(f"── 完成：{total_changed}/{total_files} 个文件被更新 ──")
    return 0


if __name__ == "__main__":
    sys.exit(main())