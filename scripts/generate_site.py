#!/usr/bin/env python3
"""Build the public benchmark and legacy novel archive as a static site.

The build is deliberately offline: model output is read from ``results/`` and
``novels/`` while CSS/JS is copied from ``site/assets``.  No API credentials are
needed and every model-controlled string is escaped before it reaches HTML.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runner.generate import (  # noqa: E402
    PROTOCOL_VERSION,
    build_run_input,
    load_prompts as load_generation_prompts,
    protocol_policy_sha256,
    result_is_complete,
)
from runner.llm_api import with_provider_request_defaults  # noqa: E402
from runner.score import ScoreError, load_submission  # noqa: E402

SITE_TITLE = "让我康康你的文"
REPO_URL = "https://github.com/klarkxy/show-me-your-novel"
SAFE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
THINKING_BLOCK = re.compile(
    r"\[思考过程\]\s*\n?.*?\n?\s*\[/思考过程\]", re.DOTALL
)
UNCLOSED_THINKING_BLOCK = re.compile(r"\[思考过程\]\s*\n?.*\Z", re.DOTALL)
OUTLINE_BLOCK = re.compile(r"(?ms)^##\s*大纲\s*$.*?(?=^##\s*第\d+章|\Z)")
JUDGE_IDS = ("sol", "fable", "kimi")
SCORE_SCHEMA = "novel-eval.v2"
GENERATION_PROTOCOL = PROTOCOL_VERSION
REQUIRED_RESULT_ARTIFACTS = (
    "book.json",
    "macro_outline.json",
    "opening_outline.json",
    "novel.md",
    "manifest.json",
)
SAFE_OUTPUT_TOP_LEVELS = frozenset({"_site", ".site"})
CSP = (
    "default-src 'none'; style-src 'self'; script-src 'self'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'"
)


def esc(value: Any) -> str:
    """Escape arbitrary data for HTML text/attribute contexts."""

    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return html.escape(str(value), quote=True)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_newlines(value: str) -> str:
    return value.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _canonical_text(value: str) -> str:
    return _normalize_newlines(value).strip()


def _sha256_normalized_text_file(path: Path) -> str:
    return _sha256_text(_normalize_newlines(path.read_bytes().decode("utf-8-sig")))


def build_protocol_expectations(
    config_path: Path,
    model_by_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Recreate the generator's tracked-input hashes for offline publishing."""

    root = config_path.parent
    config = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    direction_path = root / "benchmark" / "reform-era" / "direction.md"
    prompt_dir = root / "runner" / "prompts" / "v2.1"
    direction = _canonical_text(direction_path.read_bytes().decode("utf-8-sig"))
    prompts = load_generation_prompts(prompt_dir)
    prompts_sha256 = _sha256_text(_canonical_json(prompts))
    expectations: dict[str, dict[str, str]] = {}
    for model_id, model_cfg in model_by_id.items():
        tracked_model_cfg = with_provider_request_defaults(config, model_cfg)
        run_input = build_run_input(
            "reform-era", direction, prompts, tracked_model_cfg
        )
        run_input_sha256 = _sha256_text(_canonical_json(run_input))
        expectations[model_id] = {
            "schema": GENERATION_PROTOCOL,
            "run_id": run_input_sha256[:12],
            "run_input_sha256": run_input_sha256,
            "protocol_policy_sha256": protocol_policy_sha256(),
            "code_sha256": str(run_input["runner_code_sha256"]),
            "direction_sha256": _sha256_text(direction),
            "prompts_sha256": prompts_sha256,
            "model_config_sha256": _sha256_text(
                _canonical_json(tracked_model_cfg)
            ),
        }
    return expectations


def _safe_score_model_config(model_cfg: dict[str, Any]) -> dict[str, Any]:
    blocked = {"api_key", "api_key_env", "base_url", "token", "secret"}
    return {
        str(key): value
        for key, value in model_cfg.items()
        if str(key).lower() not in blocked
    }


def build_score_expectations(
    config_path: Path,
    config: dict[str, Any],
) -> dict[str, dict[str, str]]:
    prompt_path = config_path.parent / "runner" / "prompts" / "v2" / "judge_system.md"
    prompt = _canonical_text(prompt_path.read_bytes().decode("utf-8-sig"))
    rubric_hash = _sha256_text(prompt)
    judges = config.get("judges") if isinstance(config.get("judges"), list) else []
    expectations: dict[str, dict[str, str]] = {}
    for raw in judges:
        if not isinstance(raw, dict) or raw.get("id") not in JUDGE_IDS:
            continue
        judge_id = str(raw["id"])
        request_overrides = raw.get("request_overrides")
        model_cfg = with_provider_request_defaults(config, {
            key: value for key, value in raw.items() if key != "request_overrides"
        })
        basis = {
            "model_config": _safe_score_model_config(model_cfg),
            "request_overrides": request_overrides or {},
        }
        expectations[judge_id] = {
            "rubric_hash": rubric_hash,
            "judge_config_sha256": _sha256_text(_canonical_json(basis)),
            "requested_model": str(model_cfg.get("model") or ""),
        }
    return expectations


def _manifest_protocol_matches(
    manifest: dict[str, Any], expected: dict[str, str] | None
) -> bool:
    return bool(expected) and all(manifest.get(key) == value for key, value in expected.items())


def _inline(markdown_text: str) -> str:
    """Render the tiny safe inline Markdown subset used by this repository."""

    rendered = html.escape(markdown_text, quote=True)
    rendered = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*", r"<em>\1</em>", rendered)
    rendered = re.sub(r"`([^`]+)`", r"<code>\1</code>", rendered)
    return rendered


def strip_reasoning(text: str | None) -> str:
    """Remove private reasoning blocks, including a truncated final block."""

    cleaned = THINKING_BLOCK.sub("", text or "")
    return UNCLOSED_THINKING_BLOCK.sub("", cleaned)


def md_to_html(md: str | None) -> str:
    """Render repository Markdown without allowing raw HTML pass-through."""

    md = strip_reasoning(md)
    if not md:
        return ""

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

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # The first H1 is displayed in the page header.
        if stripped.startswith("# ") and not out:
            i += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            close_lists()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue

        if re.match(r"^[-*]\s+", stripped):
            if not in_ul:
                close_lists()
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline(stripped[2:].strip())}</li>")
            i += 1
            continue

        if re.match(r"^\d+\.\s+", stripped):
            if not in_ol:
                close_lists()
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{_inline(re.sub(r'^\d+\.\s+', '', stripped))}</li>")
            i += 1
            continue

        if stripped.startswith("> "):
            close_lists()
            quote_lines = [stripped[2:]]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("> "):
                quote_lines.append(lines[i].strip()[2:])
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(quote_lines))}</blockquote>")
            continue

        if not stripped:
            close_lists()
            i += 1
            continue

        close_lists()
        paragraph = [stripped]
        i += 1
        while (
            i < len(lines)
            and lines[i].strip()
            and lines[i].strip() not in {"[思考过程]", "[/思考过程]"}
            and not re.match(r"^(#{1,6}\s|[-*]\s|\d+\.\s|>\s)", lines[i].strip())
        ):
            paragraph.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(paragraph))}</p>")

    close_lists()
    return "\n".join(out)


def prose_only(text: str | None) -> str:
    """Remove non-prose metadata before public length statistics are computed."""

    cleaned = strip_reasoning(text)
    return OUTLINE_BLOCK.sub("", cleaned)


def count_chinese_chars(text: str | None) -> int:
    """Count prose content like the generator, excluding Markdown headings."""

    body_lines = [
        line for line in prose_only(text).splitlines() if not line.lstrip().startswith("#")
    ]
    return len(
        re.findall(r"[\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9]", "\n".join(body_lines))
    )


def count_chapters(text: str | None) -> int:
    return len(re.findall(r"^##\s+第\d+章", prose_only(text), re.MULTILINE))


def first_h1(text: str | None) -> str:
    match = re.search(r"^#\s+(.+)$", strip_reasoning(text), re.MULTILINE)
    return match.group(1).strip() if match else ""


def story_meta(prompt_md: str) -> dict[str, str]:
    genre_match = re.search(r"##\s*题材\s*\n+(.+)", prompt_md)
    genre = genre_match.group(1).strip().splitlines()[0] if genre_match else ""
    intro_match = re.search(
        r"##\s*世界观设定\s*\n+(.*?)(?=\n##\s|\Z)", prompt_md, re.DOTALL
    )
    intro = ""
    if intro_match:
        paragraphs = [p.strip() for p in intro_match.group(1).split("\n\n") if p.strip()]
        if paragraphs:
            intro = re.sub(r"\s+", " ", paragraphs[0])[:140]
    return {"genre": genre, "intro": intro}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[site] 忽略无法解析的 JSON：{path}（{exc}）", file=sys.stderr)
        return {}
    return value if isinstance(value, dict) else {}


def _manifest_artifacts_match(model_dir: Path, manifest: dict[str, Any]) -> bool:
    """Verify the generator's content-hash commit marker before publication."""

    hashes = manifest.get("artifact_sha256")
    if not isinstance(hashes, dict):
        return False
    required = {"book.json", "macro_outline.json", "opening_outline.json", "novel.md"}
    names = set(hashes)
    chapter_names = {name for name in names if re.fullmatch(r"chapters/\d{2}\.md", name)}
    if not required.issubset(names) or not chapter_names:
        return False
    for name, expected in hashes.items():
        if name not in required and name not in chapter_names:
            return False
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            return False
        path = model_dir / Path(name)
        if not path.is_file():
            return False
        digest = _sha256_normalized_text_file(path)
        if digest != expected:
            return False
    return True


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _blank_judge() -> dict[str, Any]:
    return {"score": None, "ai_flavor": None, "comment": "", "valid": False}


def _normalise_judge(
    raw: dict[str, Any],
    *,
    judge_id: str,
    candidate: str,
    expected: dict[str, str] | None,
) -> dict[str, Any]:
    """Validate the tracked V2 public-score schema without accepting coercions."""

    if raw.get("schema") != SCORE_SCHEMA:
        return _blank_judge()
    if raw.get("judge") != judge_id or raw.get("candidate") != candidate:
        return _blank_judge()
    if not expected or any(raw.get(key) != value for key, value in expected.items()):
        return _blank_judge()
    if not isinstance(raw.get("input_hash"), str) or not raw["input_hash"].strip():
        return _blank_judge()
    if not isinstance(raw.get("cache_key"), str) or not raw["cache_key"].strip():
        return _blank_judge()

    score = raw.get("score")
    ai_flavor = raw.get("ai_flavor")
    if type(score) is not int or type(ai_flavor) is not int:  # bool is not a score
        return _blank_judge()
    if not 0 <= score <= 100 or not 0 <= ai_flavor <= 100:
        return _blank_judge()

    comment = raw.get("comment")
    if not isinstance(comment, str):
        return _blank_judge()
    comment = re.sub(r"\s+", " ", comment).strip()
    if not comment or len(comment) > 200:
        return _blank_judge()
    return {
        "score": float(score),
        "ai_flavor": float(ai_flavor),
        "comment": comment,
        "valid": True,
        "input_hash": raw["input_hash"],
    }


def _mean(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def _format_score(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _data_number(value: float | None) -> str:
    return "" if value is None else f"{value:.8g}"


def load_reform_results(
    results_dir: Path,
    model_by_id: dict[str, dict[str, Any]],
    model_order: list[str] | None = None,
    protocol_by_model: dict[str, dict[str, str]] | None = None,
    score_by_judge: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Return one row per configured V2 model, including pending models."""

    ordered_ids = model_order or list(model_by_id)
    results: list[dict[str, Any]] = []
    for config_order, model_id in enumerate(ordered_ids):
        model_dir = results_dir / model_id
        book = _read_json(model_dir / "book.json")
        macro_outline = _read_json(model_dir / "macro_outline.json")
        opening_outline = _read_json(model_dir / "opening_outline.json")
        manifest = _read_json(model_dir / "manifest.json")
        novel_path = model_dir / "novel.md"
        try:
            novel = novel_path.read_text(encoding="utf-8") if novel_path.is_file() else ""
        except OSError as exc:
            print(f"[site] 忽略无法读取的正文：{novel_path}（{exc}）", file=sys.stderr)
            novel = ""

        manifest_status = str(manifest.get("status", "")).strip().lower()
        artifacts_present = all(
            (model_dir / filename).is_file() for filename in REQUIRED_RESULT_ARTIFACTS
        )
        detail_available = (
            artifacts_present
            and manifest_status in {"complete", "completed"}
            and _manifest_protocol_matches(
                manifest,
                (protocol_by_model or {}).get(model_id),
            )
            and _manifest_artifacts_match(model_dir, manifest)
            and result_is_complete(
                model_dir,
                ((protocol_by_model or {}).get(model_id) or {}).get("run_id", ""),
            )
            and bool(book)
            and bool(macro_outline)
            and bool(opening_outline)
            and bool(novel.strip())
        )

        judges: dict[str, dict[str, Any]] = {}
        for judge_id in JUDGE_IDS:
            raw = _read_json(model_dir / "scores" / f"{judge_id}.json")
            judges[judge_id] = _normalise_judge(
                raw,
                judge_id=judge_id,
                candidate=model_id,
                expected=(score_by_judge or {}).get(judge_id),
            )

        all_judges_valid = all(judges[judge_id]["valid"] for judge_id in JUDGE_IDS)
        score_input_hash = None
        if all_judges_valid:
            input_hashes = {judges[judge_id]["input_hash"] for judge_id in JUDGE_IDS}
            all_judges_valid = len(input_hashes) == 1
            if all_judges_valid:
                score_input_hash = next(iter(input_hashes))

        current_input_hash = None
        if detail_available:
            try:
                current_input_hash = load_submission(
                    results_dir.parents[1], results_dir.name, model_id
                ).input_hash
            except (ScoreError, OSError, ValueError):
                detail_available = False
        all_judges_valid = (
            all_judges_valid
            and current_input_hash is not None
            and score_input_hash == current_input_hash
        )

        aggregate_path = model_dir / "scores" / "aggregate.json"
        aggregate = _read_json(aggregate_path)
        aggregate_allows_ranking = all_judges_valid
        if aggregate_path.is_file():
            aggregate_status = str(aggregate.get("status", "")).strip().lower()
            explicit_eligible = aggregate.get("eligible_for_ranking")
            if explicit_eligible is False or aggregate_status == "incomplete":
                aggregate_allows_ranking = False
            elif explicit_eligible is True:
                completed = aggregate.get("completed_judges")
                aggregate_allows_ranking = (
                    all_judges_valid
                    and aggregate_status in {"complete", "completed"}
                    and isinstance(completed, list)
                    and completed == list(JUDGE_IDS)
                    and aggregate.get("input_hash") == score_input_hash
                )
            else:
                # Older/missing aggregate metadata is accepted only when the
                # three independent tracked score files strictly agree.
                aggregate_allows_ranking = all_judges_valid

        rankable = detail_available and all_judges_valid and aggregate_allows_ranking
        average_score = (
            _mean([judges[judge_id]["score"] for judge_id in JUDGE_IDS])
            if rankable
            else None
        )
        average_ai = (
            _mean([judges[judge_id]["ai_flavor"] for judge_id in JUDGE_IDS])
            if rankable
            else None
        )

        config_model = model_by_id.get(model_id, {})
        title = _first(book, "title") or first_h1(novel) or "待生成"
        blurb = _first(book, "blurb", "intro", "synopsis") or "尚未生成简介。"
        results.append(
            {
                "model_id": model_id,
                "model_name": config_model.get("name", model_id),
                "config_order": config_order,
                "title": title,
                "blurb": blurb,
                "book": book,
                "macro_outline": macro_outline,
                "opening_outline": opening_outline,
                "manifest": manifest,
                "novel": novel,
                "novel_html": md_to_html(prose_only(novel)),
                "body_chars": count_chinese_chars(novel),
                "chapters": count_chapters(novel),
                "judges": judges,
                "average_score": average_score,
                "average_ai": average_ai,
                "detail_available": detail_available,
                "rankable": rankable,
            }
        )

    results.sort(
        key=lambda item: (
            not item["rankable"],
            -(item["average_score"] or 0),
            item["config_order"],
        )
    )
    return results


def load_legacy_stories(
    novels_dir: Path,
    model_by_id: dict[str, dict[str, Any]],
    model_order: list[str],
) -> list[dict[str, Any]]:
    stories: list[dict[str, Any]] = []
    if not novels_dir.is_dir():
        return stories

    for story_dir in sorted(novels_dir.iterdir(), key=lambda path: path.name.casefold()):
        if not story_dir.is_dir() or not SAFE_SLUG.fullmatch(story_dir.name):
            continue
        prompt_path = story_dir / "prompt.md"
        if not prompt_path.is_file():
            continue
        prompt = prompt_path.read_text(encoding="utf-8")
        meta = story_meta(prompt)
        versions: list[dict[str, Any]] = []
        for novel_path in sorted(story_dir.glob("*.md")):
            if novel_path.name == "prompt.md" or not SAFE_SLUG.fullmatch(novel_path.stem):
                continue
            model_id = novel_path.stem
            novel = novel_path.read_text(encoding="utf-8")
            chapters = count_chapters(novel)
            versions.append(
                {
                    "model_id": model_id,
                    "model_name": model_by_id.get(model_id, {}).get("name", model_id),
                    "title": first_h1(novel) or first_h1(prompt) or story_dir.name,
                    "chars": count_chinese_chars(novel),
                    "chapters": chapters,
                    "partial": chapters < 10,
                    "content_html": md_to_html(prose_only(novel)),
                }
            )
        order = {model_id: index for index, model_id in enumerate(model_order)}
        versions.sort(
            key=lambda item: (
                order.get(item["model_id"], len(order)),
                item["model_id"].casefold(),
            )
        )
        stories.append(
            {
                "slug": story_dir.name,
                "title": first_h1(prompt) or story_dir.name,
                "genre": meta["genre"] or "小说",
                "intro": meta["intro"] or "暂无简介。",
                "prompt_html": md_to_html(prompt),
                "versions": versions,
            }
        )
    return stories


def page_head(
    title: str,
    root_prefix: str,
    body_class: str,
    *,
    leaderboard_script: bool = False,
) -> str:
    script = (
        f'\n<script src="{root_prefix}assets/leaderboard.js" defer></script>'
        if leaderboard_script
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="{CSP}">
  <meta name="color-scheme" content="light">
  <title>{esc(title)} · {SITE_TITLE}</title>
  <link rel="stylesheet" href="{root_prefix}assets/style.css">{script}
</head>
<body class="{esc(body_class)}">
<a class="skip-link" href="#main">跳到正文</a>
<header class="site-header">
  <div class="header-inner">
    <a class="brand" href="{root_prefix}index.html" aria-label="{SITE_TITLE}首页">
      <span class="brand-mark" aria-hidden="true">R84</span>
      <span>{SITE_TITLE}</span>
    </a>
    <nav class="site-nav" aria-label="主导航">
      <a href="{root_prefix}index.html">改革开放榜单</a>
      <a href="{root_prefix}novels/index.html">Legacy</a>
      <a href="{REPO_URL}" rel="noopener" target="_blank">GitHub</a>
    </nav>
  </div>
</header>
<main class="container" id="main">
"""


PAGE_FOOT = f"""
</main>
<footer class="site-footer">
  <span>本地生成 · 离线汇编 · 结果可追溯</span>
  <a href="{REPO_URL}" rel="noopener">源文件</a>
</footer>
</body>
</html>
"""


def _leaderboard_row(result: dict[str, Any], rank: int | None) -> str:
    judges = result["judges"]
    if result["detail_available"]:
        entry = f"""<a class="entry-link" href="results/reform-era/{result['model_id']}.html">
      <span class="entry-model">{esc(result['model_name'])}</span>
      <span class="entry-title">《{esc(result['title'])}》</span>
    </a>"""
    else:
        entry = f"""<span class="entry-link unavailable">
      <span class="entry-model">{esc(result['model_name'])}</span>
      <span class="entry-title">{esc(result['title'])}</span>
    </span>"""
    rank_text = f"{rank:02d}" if rank is not None else ""
    return f"""<tr data-model-id="{esc(result['model_id'])}" data-model="{esc(result['model_name'])}"
    data-config-order="{result['config_order']}"
    data-rankable="{'true' if result['rankable'] else 'false'}"
    data-average="{_data_number(result['average_score'])}"
    data-sol="{_data_number(judges['sol']['score'])}"
    data-fable="{_data_number(judges['fable']['score'])}"
    data-kimi="{_data_number(judges['kimi']['score'])}"
    data-ai-flavor="{_data_number(result['average_ai'])}">
  <td class="rank-cell" data-rank>{rank_text}</td>
  <th scope="row">
    {entry}
  </th>
  <td data-label="综合">{_format_score(result['average_score'])}</td>
  <td data-label="Sol">{_format_score(judges['sol']['score'])}</td>
  <td data-label="Fable">{_format_score(judges['fable']['score'])}</td>
  <td data-label="Kimi">{_format_score(judges['kimi']['score'])}</td>
  <td data-label="AI味">{_format_score(result['average_ai'])}</td>
</tr>"""


def render_home(results: list[dict[str, Any]], legacy_count: int) -> str:
    ranked_count = sum(1 for result in results if result["rankable"])
    rank = 0
    rendered_rows: list[str] = []
    for result in results:
        if result["rankable"]:
            rank += 1
            rendered_rows.append(_leaderboard_row(result, rank))
        else:
            rendered_rows.append(_leaderboard_row(result, None))
    rows = "\n".join(rendered_rows)
    model_count = len(results)
    if rows:
        board = f"""
<div class="table-shell">
  <table class="leaderboard" aria-describedby="ranking-note">
    <thead><tr>
      <th scope="col">档位</th><th scope="col">模型 / 书名</th>
      <th scope="col">综合</th><th scope="col">Sol</th>
      <th scope="col">Fable</th><th scope="col">Kimi</th><th scope="col">AI味</th>
    </tr></thead>
    <tbody id="leaderboard-body">{rows}</tbody>
  </table>
</div>"""
    else:
        board = """<div class="empty-state" role="status">
  <strong>榜单卷宗尚未归档。</strong>
  <span>把追踪结果放入 <code>results/reform-era/&lt;model&gt;/</code> 后重新构建站点。</span>
</div>"""

    return page_head(
        "改革开放长篇模型榜", "", "page-leaderboard", leaderboard_script=True
    ) + f"""
<section class="archive-hero" aria-labelledby="page-title">
  <div class="file-tab">PROJECT R-84 · LONGFORM REGISTER</div>
  <div class="hero-grid">
    <div>
      <p class="eyebrow">改革开放 · 二百万字全纲 · 五万字开篇</p>
      <h1 id="page-title">同一道时代命题，<br>看谁真正写得下去。</h1>
    </div>
    <dl class="archive-facts">
      <div><dt>归档模型</dt><dd>{model_count:02d}</dd></div>
      <div><dt>固定评委</dt><dd>03</dd></div>
      <div><dt>旧题材</dt><dd>{legacy_count:02d}</dd></div>
    </dl>
  </div>
</section>

<section class="ranking-panel" aria-labelledby="ranking-title">
  <header class="section-heading">
    <div><p class="eyebrow">EVALUATION LEDGER</p><h2 id="ranking-title">模型排名登记表</h2></div>
    <p id="ranking-note">点击指标重排；AI味按数值从低到高，其余按分数从高到低。</p>
  </header>
  <div class="metric-switch" role="group" aria-label="排名指标">
    <button type="button" data-sort="average" aria-pressed="true">综合平均</button>
    <button type="button" data-sort="sol" aria-pressed="false">Sol</button>
    <button type="button" data-sort="fable" aria-pressed="false">Fable</button>
    <button type="button" data-sort="kimi" aria-pressed="false">Kimi</button>
    <button type="button" data-sort="ai-flavor" aria-pressed="false">AI味最低</button>
  </div>
  <div class="rank-ruler" data-rank-ruler>
    <div class="ruler-copy">
      <label for="rank-limit">排名刻度尺</label>
      <output for="rank-limit" id="rank-limit-output">{'显示全部 ' + str(ranked_count) + ' 个已排名作品；未完成始终显示' if ranked_count else '暂无可排名作品；未完成始终显示'}</output>
    </div>
    <input id="rank-limit" type="range" min="1" max="{max(ranked_count, 1)}"
      value="{max(ranked_count, 1)}" {'disabled' if not ranked_count else ''}>
    <div class="ruler-ticks" aria-hidden="true"></div>
  </div>
  {board}
</section>

<aside class="legacy-callout">
  <span class="file-code">ARCHIVE / L-03</span>
  <div><strong>旧版三题材仍在。</strong><p>原始十章实验保持原路由，不混入本期榜单。</p></div>
  <a class="text-button" href="novels/index.html">进入 Legacy →</a>
</aside>
""" + PAGE_FOOT


def _judge_card(label: str, judge: dict[str, Any]) -> str:
    comment = judge.get("comment") or "尚未提交评语。"
    return f"""<article class="judge-card">
  <header><span>{esc(label)}</span><strong>{_format_score(judge.get('score'))}</strong></header>
  <p class="ai-flavor">AI味 <b>{_format_score(judge.get('ai_flavor'))}</b></p>
  <p>{esc(comment)}</p>
</article>"""


def _json_drawer(title: str, code: str, value: dict[str, Any]) -> str:
    if value:
        payload = esc(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        payload = "尚未生成。"
    return f"""<details class="outline-drawer">
  <summary><span>{esc(code)}</span>{esc(title)}</summary>
  <pre><code>{payload}</code></pre>
</details>"""


def render_result_detail(result: dict[str, Any]) -> str:
    judges = result["judges"]
    body_chars = result["body_chars"]
    status = result["manifest"].get("status", "complete" if result["novel"] else "pending")
    novel_html = result["novel_html"] or '<p class="empty-copy">正文尚未归档。</p>'
    return page_head(
        f"{result['title']} · {result['model_name']}", "../../", "page-result"
    ) + f"""
<a class="back-link" href="../../index.html">← 返回改革开放榜单</a>
<article class="result-file">
  <header class="result-header">
    <div class="file-tab">MODEL FILE · {esc(result['model_id'])}</div>
    <p class="eyebrow">{esc(result['model_name'])}</p>
    <h1>《{esc(result['title'])}》</h1>
    <p class="result-blurb">{esc(result['blurb'])}</p>
    <dl class="result-stats">
      <div><dt>综合平均</dt><dd>{_format_score(result['average_score'])}</dd></div>
      <div><dt>AI味</dt><dd>{_format_score(result['average_ai'])}</dd></div>
      <div><dt>正文字符</dt><dd>{body_chars:,}</dd></div>
      <div><dt>状态</dt><dd>{esc(status)}</dd></div>
    </dl>
  </header>

  <section class="judge-section" aria-labelledby="judge-title">
    <div class="section-heading"><div><p class="eyebrow">THREE-READER PANEL</p><h2 id="judge-title">三评委记录</h2></div></div>
    <div class="judge-grid">
      {_judge_card('Sol', judges['sol'])}
      {_judge_card('Fable', judges['fable'])}
      {_judge_card('Kimi', judges['kimi'])}
    </div>
  </section>

  <section class="outline-section" aria-labelledby="outline-title">
    <div class="section-heading"><div><p class="eyebrow">PLANNING FILES</p><h2 id="outline-title">规划卷宗</h2></div></div>
    {_json_drawer('二百万字全纲', 'A-200', result['macro_outline'])}
    {_json_drawer('前五万字细纲', 'B-050', result['opening_outline'])}
  </section>

  <section class="reading-section" aria-labelledby="novel-title">
    <header class="section-heading"><div><p class="eyebrow">OPENING MANUSCRIPT</p><h2 id="novel-title">前五万字正文</h2></div><p>{result['chapters']} 章 · {body_chars:,} 字符</p></header>
    <div class="novel-body markdown">{novel_html}</div>
  </section>
</article>
""" + PAGE_FOOT


def render_legacy_index(stories: list[dict[str, Any]]) -> str:
    cards = []
    for story in stories:
        cards.append(f"""<a class="legacy-card" href="{story['slug']}/index.html">
  <span class="file-code">{esc(story['genre'])}</span>
  <h2>{esc(story['title'])}</h2>
  <p>{esc(story['intro'])}</p>
  <small>{len(story['versions'])} 个模型版本</small>
</a>""")
    cards_html = "\n".join(cards) or '<p class="empty-copy">没有可展示的旧题材。</p>'
    return page_head("Legacy 十章实验", "../", "page-legacy") + f"""
<a class="back-link" href="../index.html">← 返回改革开放榜单</a>
<section class="legacy-hero">
  <p class="eyebrow">ARCHIVE / LEGACY RUNS</p>
  <h1>旧版十章实验</h1>
  <p>这些页面保留原始提示词、模型正文与 URL，不参与当前改革开放长篇排名。</p>
</section>
<div class="legacy-grid">{cards_html}</div>
""" + PAGE_FOOT


def render_legacy_story(
    story: dict[str, Any], model_by_id: dict[str, dict[str, Any]], model_order: list[str]
) -> str:
    cards = []
    for version in story["versions"]:
        suffix = " · 未满十章" if version["partial"] else ""
        cards.append(f"""<a class="version-card" href="{version['model_id']}.html">
  <strong>{esc(version['model_name'])}</strong>
  <span>{version['chars']:,} 正文字符 · {version['chapters']} 章{suffix}</span>
</a>""")
    return page_head(story["title"], "../../", "page-legacy-story") + f"""
<a class="back-link" href="../index.html">← Legacy 目录</a>
<header class="legacy-story-header"><p class="eyebrow">TEN-CHAPTER RUN</p><h1>{esc(story['title'])}</h1></header>
<details class="prompt-drawer"><summary>查看统一提示词</summary><div class="markdown">{story['prompt_html']}</div></details>
<section class="version-grid">{''.join(cards)}</section>
""" + PAGE_FOOT


def render_legacy_novel(story: dict[str, Any], version: dict[str, Any]) -> str:
    partial = '<p class="partial-notice">该版本未完成十章，按原样归档。</p>' if version["partial"] else ""
    return page_head(
        f"{version['title']} · {version['model_name']}", "../../", "page-reading"
    ) + f"""
<a class="back-link" href="index.html">← {esc(story['title'])}</a>
<article class="legacy-novel">
  <header class="novel-header">
    <p class="eyebrow">{esc(version['model_name'])} · LEGACY</p>
    <h1>{esc(version['title'])}</h1>
    <p>{version['chars']:,} 正文字符 · {version['chapters']} 章</p>{partial}
  </header>
  <div class="novel-body markdown">{version['content_html']}</div>
</article>
""" + PAGE_FOOT


def build_site(
    *,
    config_path: Path,
    novels_dir: Path,
    results_dir: Path,
    assets_dir: Path,
    output_dir: Path,
) -> dict[str, int]:
    """Build a complete site into an empty output directory."""

    if not config_path.is_file():
        raise FileNotFoundError(f"未找到配置文件：{config_path}")
    if not novels_dir.is_dir():
        raise FileNotFoundError(f"未找到旧小说目录：{novels_dir}")
    if not assets_dir.is_dir():
        raise FileNotFoundError(f"未找到站点素材目录：{assets_dir}")
    for required_asset in ("style.css", "leaderboard.js"):
        if not (assets_dir / required_asset).is_file():
            raise FileNotFoundError(f"缺少站点素材：{assets_dir / required_asset}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"构建目录必须为空：{output_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(assets_dir, output_dir / "assets")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    models = config.get("models") if isinstance(config.get("models"), list) else []
    model_by_id = {
        model["id"]: model
        for model in models
        if isinstance(model, dict) and model.get("id") and SAFE_SLUG.fullmatch(str(model["id"]))
    }
    model_order = list(model_by_id)
    protocol_by_model = build_protocol_expectations(config_path, model_by_id)
    score_by_judge = build_score_expectations(config_path, config)

    results = load_reform_results(
        results_dir,
        model_by_id,
        model_order,
        protocol_by_model,
        score_by_judge,
    )
    legacy_stories = load_legacy_stories(novels_dir, model_by_id, model_order)

    (output_dir / "index.html").write_text(
        render_home(results, len(legacy_stories)), encoding="utf-8"
    )
    result_output = output_dir / "results" / "reform-era"
    result_output.mkdir(parents=True, exist_ok=True)
    for result in results:
        if not result["detail_available"]:
            continue
        (result_output / f"{result['model_id']}.html").write_text(
            render_result_detail(result), encoding="utf-8"
        )

    legacy_output = output_dir / "novels"
    legacy_output.mkdir(parents=True, exist_ok=True)
    (legacy_output / "index.html").write_text(
        render_legacy_index(legacy_stories), encoding="utf-8"
    )
    for story in legacy_stories:
        story_output = legacy_output / story["slug"]
        story_output.mkdir(exist_ok=True)
        (story_output / "index.html").write_text(
            render_legacy_story(story, model_by_id, model_order), encoding="utf-8"
        )
        for version in story["versions"]:
            (story_output / f"{version['model_id']}.html").write_text(
                render_legacy_novel(story, version), encoding="utf-8"
            )

    return {
        "results": len(results),
        "legacy_stories": len(legacy_stories),
        "legacy_versions": sum(len(story["versions"]) for story in legacy_stories),
    }


def _resolve_from_root(value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()


def _validate_publish_target(target: Path) -> Path:
    """Limit destructive directory replacement to dedicated repo build roots."""

    resolved = target.resolve()
    try:
        relative = resolved.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise ValueError(f"输出目录必须位于仓库内：{resolved}") from exc
    if not relative.parts or relative.parts[0] not in SAFE_OUTPUT_TOP_LEVELS:
        allowed = "、".join(sorted(SAFE_OUTPUT_TOP_LEVELS))
        raise ValueError(f"输出目录仅允许位于这些构建目录：{allowed}")
    return resolved


def _publish_directory(stage: Path, target: Path) -> None:
    """Replace the public directory only after the staged build has succeeded."""

    target = _validate_publish_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_name(f".{target.name}.previous-{os.getpid()}")
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        os.replace(target, backup)
    try:
        os.replace(stage, target)
    except Exception:
        if backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线生成改革开放榜单与 Legacy 站点")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--novels-dir", default="novels")
    parser.add_argument("--results-dir", default="results/reform-era")
    parser.add_argument("--assets-dir", default="site/assets")
    parser.add_argument("--docs-dir", default=".site/preview")
    args = parser.parse_args(argv)

    config_path = _resolve_from_root(args.config)
    novels_dir = _resolve_from_root(args.novels_dir)
    results_dir = _resolve_from_root(args.results_dir)
    assets_dir = _resolve_from_root(args.assets_dir)
    try:
        docs_dir = _validate_publish_target(_resolve_from_root(args.docs_dir))
    except ValueError as exc:
        print(f"[site] 拒绝使用危险输出目录：{exc}", file=sys.stderr)
        return 1

    stage = docs_dir.with_name(f".{docs_dir.name}.build-{os.getpid()}")
    if stage.exists():
        shutil.rmtree(stage)
    try:
        summary = build_site(
            config_path=config_path,
            novels_dir=novels_dir,
            results_dir=results_dir,
            assets_dir=assets_dir,
            output_dir=stage,
        )
        _publish_directory(stage, docs_dir)
    except Exception as exc:
        if stage.exists():
            shutil.rmtree(stage)
        print(f"[site] 构建失败：{exc}", file=sys.stderr)
        return 1

    print(
        "[site] 生成完成："
        f"改革开放结果 {summary['results']} 个，"
        f"Legacy {summary['legacy_stories']} 部 / {summary['legacy_versions']} 个版本，"
        f"输出到 {docs_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
