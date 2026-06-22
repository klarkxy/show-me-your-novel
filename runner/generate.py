#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runner/generate.py — 直接调用云端 LLM API 分章生成小说

流程：
  1. 读取 prompt.md
  2. 调用 API 生成结构化大纲（outline.json）
  3. 循环 1..10，每次生成一章，并把前文完整正文拼进上下文
  4. 合并章节为 novels/<story>/<model>.md，追加【未完待续】
  5. 最终校验（10 章、≥20000 字、结尾标记、无代码围栏）

用法：
  python3 runner/generate.py
  python3 runner/generate.py --story sci-fi-uplink
  python3 runner/generate.py --story sci-fi-uplink --model deepseek-v4-flash
  python3 runner/generate.py --story sci-fi-uplink --model deepseek-v4-flash --reset
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


# Windows 终端默认编码可能为 GBK，强制 stdout/stderr 使用 UTF-8，
# 避免日志中的中文和符号触发 'gbk codec can't encode' 异常。
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
else:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 默认常量
# ---------------------------------------------------------------------------
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 8192
# 只防过短，不过分限制上限：prompt 里建议 2000–3000，
# 质检时只要 ≥1500 即可，不超过 max_tokens 自然上限即可。
MIN_CHAPTER_CHARS = 1500
MAX_CHAPTER_CHARS = 99999
MIN_TOTAL_CHARS = 20000
MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[gen] {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"[gen] [OK] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[gen] [WARN] {msg}", flush=True)


def err(msg: str) -> None:
    print(f"[gen] [ERR] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 路径与配置
# ---------------------------------------------------------------------------
def find_repo_root() -> Path:
    """脚本位于 runner/ 下，向上退一级即仓库根目录。"""
    return Path(__file__).resolve().parent.parent


def load_env_file(env_file: Path) -> dict[str, str]:
    """简易 .env 解析，支持 KEY=VALUE 与 KEY: VALUE。"""
    env: dict[str, str] = {}
    if not env_file.exists():
        return env
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*[:=]\s*(\S+)\s*$", line)
        if m:
            env[m.group(1)] = m.group(2)
    return env


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"未找到配置文件：{config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def get_model_config(cfg: dict[str, Any], model_id: str) -> dict[str, Any]:
    for m in cfg.get("models", []):
        if m.get("id") == model_id:
            return m
    raise ValueError(f"config.yaml 中未找到模型 id：{model_id}")


def get_provider_config(cfg: dict[str, Any], provider_id: str) -> dict[str, Any]:
    providers = cfg.get("providers") or {}
    if provider_id not in providers:
        raise ValueError(f"config.yaml 中未找到 provider：{provider_id}")
    return providers[provider_id]


# ---------------------------------------------------------------------------
# 文本统计与校验
# ---------------------------------------------------------------------------
def count_chinese_chars(text: str) -> int:
    """与 scripts/generate-site.ps1 保持一致：统计中文、全角、字母数字。"""
    return len(re.findall(r"[一-鿿　-〿A-Za-z0-9]", text or ""))


def count_chapters(text: str) -> int:
    """与站点脚本保持一致：统计独立的 ## 第N章 行。"""
    return len(re.findall(r"^##\s+第\d+章", text or "", re.MULTILINE))


def has_code_fence(text: str) -> bool:
    return "```" in text


CHINESE_DIGITS = {
    '零': 0, '一': 1, '二': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
}


def chinese_to_int(s: str) -> int | None:
    """简单中文数字转整数，支持一到十。"""
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total = 0
    for ch in s:
        if ch in CHINESE_DIGITS:
            total += CHINESE_DIGITS[ch]
    return total if total > 0 else None


def clean_chapter(text: str, expected_number: int, expected_title: str = "") -> str:
    """
    清洗模型输出：
    - 去掉首尾空白
    - 去掉可能被包裹的 markdown code fence
    - 去掉常见的 AI 前缀
    - 识别多种章节标题格式并统一为 ## 第N章 标题
    - 若仍无标题，自动补全
    """
    text = text.strip()

    # 去掉 ```markdown ... ``` 或 ``` ... ```
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    # 去掉常见 AI 前缀
    prefixes = [
        rf"^(好的[，。])?\s*(以下是|这是)?\s*第\s*{expected_number}\s*章[：:.]?\s*",
        r"^(以下是|这是)?\s*本章正文[：:.]?\s*",
        r"^(好的[，。])?\s*让我?来?继续?写[：:.]?\s*",
    ]
    for p in prefixes:
        text = re.sub(p, "", text, flags=re.IGNORECASE).strip()

    # 去掉末尾常见的字数统计/元评论行
    text = re.sub(r"\n*\s*（字数[统计]*[：:].*?）\s*$", "", text).strip()
    text = re.sub(r"\n*\s*\(字数[统计]*[：:].*?\)\s*$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\n*\s*字数[统计]*[：:]\s*约?\s*\d+[\d,]*\s*字?\s*$", "", text).strip()

    # 统一章节标题格式
    lines = text.splitlines()
    title_pattern = re.compile(
        rf"^##\s*第\s*(\d+|[一二三四五六七八九十]+)\s*章\s*(.*)$"
    )
    title_line_idx = None
    found_number = None
    found_title = ""
    for i, line in enumerate(lines):
        m = title_pattern.match(line.strip())
        if m:
            title_line_idx = i
            found_number = chinese_to_int(m.group(1))
            found_title = m.group(2).strip()
            break

    if title_line_idx is not None:
        # 去掉标题前的废话
        lines = lines[title_line_idx:]
        # 把标题统一为 ## 第N章 标题
        title_to_use = found_title or expected_title
        lines[0] = f"## 第{expected_number}章 {title_to_use}"
        text = "\n".join(lines).strip()
    else:
        # 没有找到标题，自动补全
        title_to_use = expected_title
        text = f"## 第{expected_number}章 {title_to_use}\n\n{text}"

    return text.strip()


def validate_chapter(text: str, number: int) -> tuple[bool, str]:
    """单章质检。返回 (是否通过, 失败原因)。"""
    chars = count_chinese_chars(text)
    if chars < MIN_CHAPTER_CHARS:
        return False, f"字数不足：{chars} < {MIN_CHAPTER_CHARS}"
    if chars > MAX_CHAPTER_CHARS:
        return False, f"字数超标：{chars} > {MAX_CHAPTER_CHARS}"
    if not re.search(rf"^##\s+第\s*{number}\s*章", text, re.MULTILINE):
        return False, "缺少或格式错误的章节标题"
    if has_code_fence(text):
        return False, "包含代码围栏"
    # 简单元评论检测
    # 只检测关键词出现在行首附近（前6个字符内）的情况，
    # 避免将正文中自然出现的"这是""字数"等误判为元评论。
    meta_keywords = ["好的", "以下是", "这是", "我来写", "字数", "扩写", "精简", "本章正文"]
    first_lines = "\n".join(text.splitlines()[:3])
    for kw in meta_keywords:
        if kw in first_lines and kw not in (re.search(rf"^##\s+第\s*{number}\s+章\s+(.+)$", text, re.MULTILINE) or [""])[0]:
            # 只检测行首附近有关键词的情况（前6个字符内）
            if re.search(rf"^[^#\s].{{0,5}}{kw}", first_lines, re.MULTILINE):
                return False, f"开头疑似含元评论：{kw}"
    return True, ""


def validate_novel(text: str) -> tuple[bool, str]:
    """全文质检。"""
    chapters = count_chapters(text)
    if chapters != 10:
        return False, f"章节数不为 10：{chapters}"
    chars = count_chinese_chars(text)
    if chars < MIN_TOTAL_CHARS:
        return False, f"总字数不足：{chars} < {MIN_TOTAL_CHARS}"
    if not text.rstrip().endswith("【未完待续】"):
        return False, "末尾缺少【未完待续】"
    if has_code_fence(text):
        return False, "全文包含代码围栏"
    return True, ""


# ---------------------------------------------------------------------------
# HTTP API 调用
# ---------------------------------------------------------------------------
def normalize_base_url(base_url: str) -> str:
    """确保 base_url 以 /v1 结尾，用于调用 /chat/completions。"""
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def chat_completion(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    response_format: dict[str, str] | None = None,
    timeout: int = 180,
    thinking: bool = False,
) -> tuple[str, str]:
    """同步调用 OpenAI-compatible chat completions。

    返回 (content, reasoning_content)，若模型未返回思考内容则 reasoning_content 为空。
    """
    url = f"{normalize_base_url(base_url)}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        payload["response_format"] = response_format
    if thinking:
        # 兼容不同的 thinking 格式：True → "enabled"，字符串直接使用
        thinking_val = thinking if isinstance(thinking, str) else "enabled"
        payload["thinking"] = {"type": thinking_val}

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "show-me-your-novel/1.0 (python-urllib)",
        },
        method="POST",
    )

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                choices = result.get("choices") or []
                if not choices:
                    raise RuntimeError("API 返回空 choices")
                msg = choices[0].get("message", {})
                content = msg.get("content", "")
                if not content.strip():
                    raise RuntimeError("API 返回空内容")
                reasoning = msg.get("reasoning_content", "")
                return content, reasoning
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")[:500]
            last_error = RuntimeError(f"HTTP {e.code}: {body}")
        except Exception as e:
            last_error = e
        if attempt < MAX_RETRIES:
            wait = 2 ** (attempt - 1)
            warn(f"API 调用失败（{attempt}/{MAX_RETRIES}），{wait}s 后重试：{last_error}")
            time.sleep(wait)

    raise last_error or RuntimeError("API 调用失败")


# ---------------------------------------------------------------------------
# 大纲生成
# ---------------------------------------------------------------------------
def build_outline_messages(prompt_text: str) -> list[dict[str, str]]:
    system = (
        "你是一位专业中文小说作家。请根据用户提供的小说设定，输出一份结构化大纲。"
        "只输出 JSON，不要任何解释、注释或 markdown 代码围栏。"
    )
    user = (
        f"{prompt_text}\n\n"
        "请为这部小说生成 10 章的详细大纲，按 JSON 格式返回：\n"
        "{\n"
        '  "title": "小说标题",\n'
        '  "total_chapters": 10,\n'
        '  "chapters": [\n'
        "    {\n"
        '      "number": 1,\n'
        '      "title": "章节标题",\n'
        '      "summary": "本章核心情节（50–100 字）",\n'
        '      "target_words": 2500\n'
        "    },\n"
        "    ...\n"
        "  ]\n"
        "}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_outline(text: str) -> dict[str, Any]:
    """解析大纲 JSON，支持从 code fence 中提取。"""
    text = text.strip()
    # 优先提取 ```json ... ```
    m = re.search(r"```(?:json)?\s*\n(.*?)\n?```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("大纲不是 JSON 对象")
    if data.get("total_chapters") != 10:
        raise ValueError(f"大纲章节数不是 10：{data.get('total_chapters')}")
    chapters = data.get("chapters") or []
    if len(chapters) != 10:
        raise ValueError(f"大纲 chapters 长度不是 10：{len(chapters)}")
    for i, ch in enumerate(chapters, start=1):
        if ch.get("number") != i:
            raise ValueError(f"第 {i} 章 number 字段不匹配：{ch}")
        if not ch.get("title"):
            raise ValueError(f"第 {i} 章缺少 title")
        if not ch.get("summary"):
            raise ValueError(f"第 {i} 章缺少 summary")
    return data


def generate_outline(
    prompt_text: str,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    max_tokens: int,
    thinking: bool = False,
    outline_json: bool = True,
) -> dict[str, Any]:
    messages = build_outline_messages(prompt_text)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            text, _reasoning = chat_completion(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"} if outline_json else None,
                thinking=thinking,
            )
            return parse_outline(text)
        except Exception as e:
            err(f"生成大纲失败（{attempt}/{MAX_RETRIES}）：{e}")
            if attempt == MAX_RETRIES:
                raise
    raise RuntimeError("生成大纲失败")


# ---------------------------------------------------------------------------
# 章节生成
# ---------------------------------------------------------------------------
def build_chapter_messages(
    prompt_text: str,
    chapter_info: dict[str, Any],
    previous_chapters: list[str],
) -> list[dict[str, str]]:
    number = chapter_info["number"]
    title = chapter_info["title"]
    summary = chapter_info["summary"]
    target = chapter_info.get("target_words", 2500)

    system = (
        "你是一位专业中文小说作家。请严格按用户要求只输出一章小说正文，"
        "不要输出任何写作过程、总结、解释或元评论。"
    )

    user_parts = [
        prompt_text,
        "",
        f"请只写第 {number} 章《{title}》的正文。",
        f"本章摘要：{summary}",
        f"目标字数：{target} 字（允许范围 {MIN_CHAPTER_CHARS}–{MAX_CHAPTER_CHARS} 字）。",
        "",
        "要求：",
        f'1. 以 Markdown 标题 "## 第{number}章 {title}" 开头',
        f"2. 字数控制在 {MIN_CHAPTER_CHARS}–{MAX_CHAPTER_CHARS} 字之间",
        "3. 紧接前文情节，保持人物设定与世界观一致",
        "4. 叙事紧凑，有细节、有悬念、有主角心理变化",
        "5. 不要输出任何写作过程、总结、解释、或元评论",
        "6. 只返回本章正文，不要额外内容",
    ]

    if previous_chapters:
        user_parts.extend([
            "",
            "===== 前文已生成章节（供你保持连贯）=====",
            "",
        ])
        for idx, ch_text in enumerate(previous_chapters, start=1):
            user_parts.append(f"--- 第 {idx} 章 ---")
            user_parts.append(ch_text)
            user_parts.append("")

    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(user_parts)}]


def _build_revision_messages(
    chapter_info: dict[str, Any],
    current_text: str,
    reason: str,
) -> list[dict[str, str]]:
    """根据质检失败原因，构建针对性修改 prompt；本地已精确统计字数并反馈给模型。"""
    number = chapter_info["number"]
    title = chapter_info["title"]
    system = "你是一位专业中文小说编辑。请只输出修改后的章节正文，不要解释。"

    current_chars = count_chinese_chars(current_text)
    target_low = MIN_CHAPTER_CHARS
    target_high = MAX_CHAPTER_CHARS
    target_mid = chapter_info.get("target_words", 2500)

    base_feedback = (
        f"【本地字数统计】当前本章共 {current_chars} 个中文字符（含字母数字）。"
        f"合格范围为 {target_low}–{target_high} 字，建议目标 {target_mid} 字。\n"
    )

    if "字数超标" in reason:
        over = current_chars - target_high
        user = (
            f"以下是小说的第 {number} 章《{title}》。\n\n"
            f"{base_feedback}"
            f"当前已超出上限 {over} 字。\n\n"
            f"===== 原文 =====\n{current_text}\n\n"
            f"要求：在不丢失核心情节、关键细节和人物心理的前提下，"
            f"精简语言、合并冗余描写、删减次要场景，把字数压缩到 {target_low}–{target_high} 字之间。"
            f"保持章节标题 \"## 第{number}章 {title}\" 在最前面。只输出修改后的正文。"
        )
    elif "字数不足" in reason:
        short = target_low - current_chars
        user = (
            f"以下是小说的第 {number} 章《{title}》。\n\n"
            f"{base_feedback}"
            f"当前还缺 {short} 字才达到下限。\n\n"
            f"===== 原文 =====\n{current_text}\n\n"
            f"要求：在保持情节连贯的前提下，增加细节描写、环境渲染、人物心理或对话，"
            f"把字数扩充到 {target_low}–{target_high} 字之间。"
            f"保持章节标题 \"## 第{number}章 {title}\" 在最前面。只输出修改后的正文。"
        )
    else:
        # 标题丢失/格式错误/元评论等，重新整理格式
        user = (
            f"以下是小说的第 {number} 章《{title}》。\n\n"
            f"{base_feedback}\n\n"
            f"===== 原文 =====\n{current_text}\n\n"
            f"要求：整理格式，确保正文以 \"## 第{number}章 {title}\" 开头，"
            f"删除任何写作过程、解释或元评论，只保留小说正文。"
            f"字数控制在 {target_low}–{target_high} 字之间。"
        )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def generate_single_chapter(
    prompt_text: str,
    chapter_info: dict[str, Any],
    previous_chapters: list[str],
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    max_tokens: int,
    thinking: bool = False,
    work_dir: Path | None = None,
) -> str:
    """生成一章，含清洗与质检，失败会重试。"""
    number = chapter_info["number"]
    messages = build_chapter_messages(prompt_text, chapter_info, previous_chapters)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw_text, reasoning = chat_completion(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                thinking=thinking,
            )
            if work_dir is not None:
                raw_dir = work_dir / "raw"
                raw_dir.mkdir(exist_ok=True)
                (raw_dir / f"chapter_{number:02d}_attempt_{attempt}.md").write_text(
                    raw_text, encoding="utf-8"
                )
                if reasoning:
                    (raw_dir / f"chapter_{number:02d}_attempt_{attempt}_thinking.md").write_text(
                        reasoning, encoding="utf-8"
                    )
            text = clean_chapter(raw_text, number, chapter_info.get("title", ""))
            ok_, reason = validate_chapter(text, number)
            if ok_:
                return text, reasoning
            err(f"第 {number} 章质检未通过（{attempt}/{MAX_RETRIES}）：{reason}")

            # 第一次失败后，基于已有文本做针对性修改，而不是让模型从头乱写
            messages = _build_revision_messages(chapter_info, text, reason)
        except Exception as e:
            err(f"第 {number} 章生成失败（{attempt}/{MAX_RETRIES}）：{e}")
            if attempt == MAX_RETRIES:
                raise

    raise RuntimeError(f"第 {number} 章 {MAX_RETRIES} 次尝试后仍未通过质检")


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------
def merge_chapters(outline: dict[str, Any], chapters: list[str], thinkings: list[str] | None = None) -> str:
    """合并章节为最终小说正文，大纲作为目录夹在标题与正文之间。

    若提供了 thinkings 列表，会在每章标题后插入思考内容（[思考过程]...[/思考过程]）。
    """
    lines: list[str] = []
    title = outline.get("title", "")
    if title:
        lines.append(f"# {title}")
        lines.append("")

    # 大纲目录
    outline_chapters = outline.get("chapters") or []
    if outline_chapters:
        lines.append("## 大纲")
        lines.append("")
        for ch in outline_chapters:
            num = ch.get("number", "")
            ch_title = ch.get("title", "")
            summary = ch.get("summary", "")
            lines.append(f"{num}. **{ch_title}** — {summary}")
        lines.append("")

    for i, ch_text in enumerate(chapters, start=1):
        if i > 1:
            lines.append("")
        lines.append(ch_text)
        # 在每章末尾插入思考内容（折叠展示用）
        if thinkings and i <= len(thinkings) and thinkings[i - 1].strip():
            lines.append("")
            lines.append("[思考过程]")
            lines.append(thinkings[i - 1].strip())
            lines.append("[/思考过程]")

    # 确保结尾有【未完待续】
    full_text = "\n".join(lines).rstrip()

    # 若章节数 > 10，截断到第 10 章（模型有时会多写）
    ch_pattern = re.compile(r"^##\s+第1?\d章", re.MULTILINE)
    ch_matches = list(ch_pattern.finditer(full_text))
    expected = len(outline.get("chapters") or [10])
    if len(ch_matches) > expected:
        cut = ch_matches[expected].start()
        full_text = full_text[:cut].rstrip()

    if not full_text.endswith("【未完待续】"):
        full_text += "\n\n【未完待续】"
    return full_text


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="直接调用云端 LLM API 分章生成小说")
    parser.add_argument("--story", help="只生成指定小说 slug（如 sci-fi-uplink）")
    parser.add_argument("--model", action="append", help="只使用指定模型 id（可多次指定）")
    parser.add_argument("--config", help="config.yaml 路径")
    parser.add_argument("--env", dest="env_file", help=".env 路径")
    parser.add_argument("--novels-dir", help="novels/ 目录路径")
    parser.add_argument("--work-dir", help="中间产物目录")
    parser.add_argument("--reset", action="store_true", help="强制清空中间产物并重新生成")
    args = parser.parse_args()

    root_dir = find_repo_root()
    config_path = Path(args.config) if args.config else root_dir / "config.yaml"
    env_file = Path(args.env_file) if args.env_file else root_dir / ".env"
    novels_dir = Path(args.novels_dir) if args.novels_dir else root_dir / "novels"
    work_dir = Path(args.work_dir) if args.work_dir else root_dir / "work"

    # 加载配置
    cfg = load_config(config_path)
    env = load_env_file(env_file)
    env.update(os.environ)

    # 确定要处理的小说
    stories: list[Path] = []
    if args.story:
        story_dir = novels_dir / args.story
        if not (story_dir / "prompt.md").exists():
            err(f"未找到小说 {args.story}（应有 {story_dir / 'prompt.md'}）")
            return 1
        stories.append(story_dir)
    else:
        for d in sorted(novels_dir.iterdir()):
            if d.is_dir() and (d / "prompt.md").exists():
                stories.append(d)
    if not stories:
        err(f"没有找到任何小说（{novels_dir}/*/prompt.md）")
        return 1

    # 确定要使用的模型
    model_ids: list[str] = []
    if args.model:
        for mid in args.model:
            get_model_config(cfg, mid)
        model_ids = args.model
    else:
        model_ids = [m["id"] for m in cfg.get("models", []) if m.get("id")]
    if not model_ids:
        err("config.yaml 中没有可用模型")
        return 1

    total = len(stories) * len(model_ids)
    done_count = skipped_count = failed_count = 0
    failures: list[str] = []

    for story_dir in stories:
        story_slug = story_dir.name
        prompt_file = story_dir / "prompt.md"
        prompt_text = prompt_file.read_text(encoding="utf-8")
        log(f"小说：{story_slug}")

        for model_id in model_ids:
            model_cfg = get_model_config(cfg, model_id)
            provider_id = model_cfg.get("provider") or "opencode-go"
            provider_cfg = get_provider_config(cfg, provider_id)
            base_url = provider_cfg["base_url"]
            api_key_env = provider_cfg.get("api_key_env", "OPENCODE_API_KEY")
            api_key = env.get(api_key_env)
            if not api_key:
                err(f"  └ {model_cfg['name']} → 缺少环境变量 {api_key_env}")
                failed_count += 1
                failures.append(f"{model_cfg['name']} [{story_slug}, 缺少 {api_key_env}]")
                continue

            output_file = story_dir / f"{model_id}.md"
            story_work_dir = work_dir / story_slug / model_id

            if output_file.exists() and not args.reset:
                # 先简单校验最终文件，通过则跳过
                existing = output_file.read_text(encoding="utf-8")
                ok_, reason = validate_novel(existing)
                if ok_:
                    warn(f"  └ {model_cfg['name']} → 已存在且校验通过，跳过")
                    skipped_count += 1
                    continue
                warn(f"  └ {model_cfg['name']} → 已存在但校验未通过（{reason}），重新生成")

            if args.reset and story_work_dir.exists():
                import shutil
                shutil.rmtree(story_work_dir)
            story_work_dir.mkdir(parents=True, exist_ok=True)

            meta_path = story_work_dir / "meta.json"
            meta: dict[str, Any] = {
                "story": story_slug,
                "model": model_id,
                "status": "in_progress",
                "outline_generated": False,
                "last_completed_chapter": 0,
                "attempts": {},
                "started_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if meta_path.exists():
                try:
                    meta.update(json.loads(meta_path.read_text(encoding="utf-8")))
                except Exception:
                    pass

            temperature = model_cfg.get("temperature", DEFAULT_TEMPERATURE)
            max_tokens = model_cfg.get("max_tokens", DEFAULT_MAX_TOKENS)
            thinking_enabled = model_cfg.get("thinking", False)
            outline_json = model_cfg.get("outline_json", True)

            try:
                log(f"  └ {model_cfg['name']} → 生成中…（provider={provider_id}, model={model_cfg['model']}）")

                # 1) 大纲
                outline_path = story_work_dir / "outline.json"
                if meta.get("outline_generated") and outline_path.exists():
                    outline = json.loads(outline_path.read_text(encoding="utf-8"))
                    log(f"      复用已有大纲")
                else:
                    outline = generate_outline(
                        prompt_text=prompt_text,
                        base_url=base_url,
                        api_key=api_key,
                        model=model_cfg["model"],
                        temperature=temperature,
                        max_tokens=max_tokens,
                        thinking=thinking_enabled,
                        outline_json=outline_json,
                    )
                    outline_path.write_text(json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8")
                    meta["outline_generated"] = True
                    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
                    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                    ok(f"      大纲生成完成：{outline['title']}")

                # 2) 逐章生成
                chapters: list[str] = []
                thinkings: list[str] = []
                last_done = meta.get("last_completed_chapter", 0)
                for ch in outline["chapters"]:
                    number = ch["number"]
                    chapter_file = story_work_dir / f"chapter_{number:02d}.md"
                    thinking_file = story_work_dir / f"chapter_{number:02d}_thinking.md"

                    if number <= last_done and chapter_file.exists():
                        ch_text = chapter_file.read_text(encoding="utf-8")
                        chapters.append(ch_text)
                        if thinking_file.exists():
                            thinkings.append(thinking_file.read_text(encoding="utf-8"))
                        else:
                            thinkings.append("")
                        log(f"      第 {number} 章已存在，复用")
                        continue

                    ch_text, reasoning = generate_single_chapter(
                        prompt_text=prompt_text,
                        chapter_info=ch,
                        previous_chapters=chapters[:],
                        base_url=base_url,
                        api_key=api_key,
                        model=model_cfg["model"],
                        temperature=temperature,
                        max_tokens=max_tokens,
                        thinking=thinking_enabled,
                        work_dir=story_work_dir,
                    )
                    chapter_file.write_text(ch_text, encoding="utf-8")
                    if reasoning:
                        thinking_file.write_text(reasoning, encoding="utf-8")
                    chapters.append(ch_text)
                    thinkings.append(reasoning)

                    meta["last_completed_chapter"] = number
                    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
                    attempts = meta.get("attempts", {})
                    attempts[f"chapter_{number:02d}"] = attempts.get(f"chapter_{number:02d}", 0) + 1
                    meta["attempts"] = attempts
                    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                    ok(f"      第 {number} 章生成完成（{count_chinese_chars(ch_text)} 字）")

                # 3) 合并
                full_text = merge_chapters(outline, chapters, thinkings)

                # 备份旧产物
                if output_file.exists():
                    backup_path = story_work_dir / f"backup_{model_id}.md"
                    backup_path.write_text(output_file.read_text(encoding="utf-8"), encoding="utf-8")

                output_file.write_text(full_text, encoding="utf-8")

                # 4) 最终校验
                ok_, reason = validate_novel(full_text)
                if not ok_:
                    err(f"      最终校验未通过：{reason}")
                    failed_count += 1
                    failures.append(f"{model_cfg['name']} [{story_slug}, {reason}]")
                    continue

                meta["status"] = "completed"
                meta["updated_at"] = datetime.now(timezone.utc).isoformat()
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

                chars = count_chinese_chars(full_text)
                ok(f"  └ {model_cfg['name']} → 完成（{chars} 字，10 章）")
                done_count += 1

            except Exception as e:
                err(f"  └ {model_cfg['name']} → 失败：{e}")
                failed_count += 1
                failures.append(f"{model_cfg['name']} [{story_slug}]")
                # 保留中间产物供排查
                meta["status"] = "failed"
                meta["error"] = str(e)
                meta["updated_at"] = datetime.now(timezone.utc).isoformat()
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 汇总
    log("────────────────────────────────────────")
    log(f"完成：{done_count}  跳过：{skipped_count}  失败：{failed_count}  总计：{total}")
    if failed_count > 0:
        err("失败列表：" + "、".join(failures))
        err("请查看 work/<slug>/<model>/ 目录下的 meta.json 与中间产物。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
