#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
re-merge 12 个 work/ 中已有 10 章的 (story, model) 对到 novels/。

不调 API：直接读 work/<story>/<model>/chapter_NN.md 与 chapter_NN_thinking.md，
复用 runner.generate.merge_chapters() 重新生成合并文件。
"""

import json
import sys
from pathlib import Path

# 让脚本可以 import runner.generate
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))

from generate import merge_chapters  # noqa: E402

# 12 对 work/ 中已有完整 10 章、但 novels/ 中合并文件未通过校验的版本
TARGETS = [
    ("arknights-america", "deepseek-v4-flash"),
    ("arknights-america", "deepseek-v4-pro"),
    ("arknights-america", "glm-5.2"),
    ("arknights-america", "kimi-k2.7-code"),
    ("red-february",    "deepseek-v4-flash"),
    ("red-february",    "deepseek-v4-pro"),
    ("red-february",    "glm-5.2"),
    ("reform-era",      "deepseek-v4-flash"),
    ("reform-era",      "deepseek-v4-pro"),
    ("reform-era",      "glm-5.2"),
    ("reform-era",      "kimi-k2.7-code"),
    ("reform-era",      "qwen3.7-plus"),
]


def remerge(story: str, model: str) -> tuple[bool, str]:
    work = ROOT / "work" / story / model
    out = ROOT / "novels" / story / f"{model}.md"

    outline_path = work / "outline.json"
    if not outline_path.exists():
        return False, "outline.json 缺失"
    outline = json.loads(outline_path.read_text(encoding="utf-8"))

    chapters: list[str] = []
    thinkings: list[str] = []
    for n in range(1, 11):
        cf = work / f"chapter_{n:02d}.md"
        if not cf.exists():
            return False, f"chapter_{n:02d}.md 缺失"
        chapters.append(cf.read_text(encoding="utf-8"))
        tf = work / f"chapter_{n:02d}_thinking.md"
        thinkings.append(tf.read_text(encoding="utf-8") if tf.exists() else "")

    merged = merge_chapters(outline, chapters, thinkings)

    # 备份旧文件
    if out.exists():
        bak = work / f"backup_{model}.md"
        bak.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")

    out.write_text(merged, encoding="utf-8")

    # 简单校验
    import re
    ch_count = len(re.findall(r"^##\s+第\d+章", merged, re.MULTILINE))
    if ch_count != 10:
        return False, f"合并后章节数仍为 {ch_count}"
    return True, f"合并完成，{len(merged)} 字符"


def main() -> int:
    ok_count = fail_count = 0
    for story, model in TARGETS:
        ok, msg = remerge(story, model)
        mark = "OK " if ok else "ERR"
        print(f"[{mark}] {story}/{model}: {msg}")
        if ok:
            ok_count += 1
        else:
            fail_count += 1
    print(f"\n汇总：成功 {ok_count}，失败 {fail_count}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
