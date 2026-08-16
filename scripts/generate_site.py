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
import math
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, NamedTuple

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runner.generate import (  # noqa: E402
    ARCHIVE_DIR_NAME,
    PROTOCOL_VERSION,
    build_run_input,
    load_prompts as load_generation_prompts,
    protocol_policy_sha256,
    result_is_complete,
)
from runner.llm_api import with_provider_request_defaults  # noqa: E402
from runner.score import (  # noqa: E402
    AGGREGATE_SCHEMA_VERSION,
    DIMENSION_KEYS,
    DIMENSION_SPECS,
    JUDGE_IDS,
    JUDGE_LABELS,
    SCHEMA_VERSION as SCORE_SCHEMA,
    ScoreError,
    aggregate_dimension_scores,
    dimension_radar_value,
    judge_request_overrides,
    load_submission,
    load_submission_from_dir,
    load_system_prompt,
    overall_score_from_medians,
    parse_score_response,
)
from runner.score_v4 import (  # noqa: E402
    V4_REQUEST_OVERRIDES,
    _valid_outline_audit,
    expected_aggregate_provenance,
    load_outline_audit_submission,
    load_outline_prompt as load_v4_outline_prompt,
    load_submission as load_v4_submission,
    load_submission_from_dir as load_v4_submission_from_dir,
    outline_audit_cache_key,
    outline_audit_identity,
    resolve_judge_configs as resolve_v4_judge_configs,
)
from runner.llm_api import load_config as load_llm_config  # noqa: E402
from runner.compare_v4 import (  # noqa: E402
    ALL_CANDIDATES as V4_ALL_CANDIDATES,
    load_system_prompt as load_v4_pairwise_prompt,
)
from runner.reagg_v3 import (  # noqa: E402
    COMPLETE as REAGG_COMPLETE,
    attach_reagg_v3,
)

SITE_TITLE = "让我康康你的文"
REPO_URL = "https://github.com/klarkxy/show-me-your-novel"
SAFE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
THINKING_BLOCK = re.compile(
    r"\[思考过程\]\s*\n?.*?\n?\s*\[/思考过程\]", re.DOTALL
)
UNCLOSED_THINKING_BLOCK = re.compile(r"\[思考过程\]\s*\n?.*\Z", re.DOTALL)
OUTLINE_BLOCK = re.compile(r"(?ms)^##\s*大纲\s*$.*?(?=^##\s*第\d+章|\Z)")
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
    prompt = load_system_prompt(config_path.parent)
    rubric_hash = _sha256_text(prompt)
    judges = config.get("judges") if isinstance(config.get("judges"), list) else []
    expectations: dict[str, dict[str, str]] = {}
    for raw in judges:
        if not isinstance(raw, dict) or raw.get("id") not in JUDGE_IDS:
            continue
        judge_id = str(raw["id"])
        request_overrides = judge_request_overrides(
            judge_id, raw.get("request_overrides")
        )
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


def _blank_dimensions() -> dict[str, dict[str, Any]]:
    return {
        spec.key: {"score": None, "comment": ""}
        for spec in DIMENSION_SPECS
    }


def _blank_judge() -> dict[str, Any]:
    return {"dimensions": _blank_dimensions(), "valid": False}


def _normalise_judge(
    raw: dict[str, Any],
    *,
    benchmark: str,
    judge_id: str,
    candidate: str,
    expected: dict[str, str] | None,
) -> dict[str, Any]:
    """Validate one tracked V3 score record using the runner's parser."""

    if raw.get("schema") != SCORE_SCHEMA:
        return _blank_judge()
    if raw.get("benchmark") != benchmark:
        return _blank_judge()
    if raw.get("judge") != judge_id or raw.get("candidate") != candidate:
        return _blank_judge()
    if not expected or any(raw.get(key) != value for key, value in expected.items()):
        return _blank_judge()
    if not isinstance(raw.get("input_hash"), str) or not raw["input_hash"].strip():
        return _blank_judge()
    if not isinstance(raw.get("cache_key"), str) or not raw["cache_key"].strip():
        return _blank_judge()
    if (
        not isinstance(raw.get("response_model"), str)
        or not raw["response_model"].strip()
    ):
        return _blank_judge()
    allowed_fields = {
        "schema",
        "benchmark",
        "candidate",
        "judge",
        "requested_model",
        "input_hash",
        "rubric_hash",
        "judge_config_sha256",
        "response_model",
        "cache_key",
        "dimensions",
    }
    if set(raw) != allowed_fields:
        return _blank_judge()

    try:
        parsed = parse_score_response(
            _canonical_json({"dimensions": raw.get("dimensions")})
        )
    except ScoreError:
        return _blank_judge()
    dimensions = raw.get("dimensions")
    if not isinstance(dimensions, dict):
        return _blank_judge()
    for key in DIMENSION_KEYS:
        entry = dimensions.get(key)
        if not isinstance(entry, dict) or type(entry.get("score")) is not float:
            return _blank_judge()
        if entry != parsed["dimensions"][key]:
            return _blank_judge()
    return {"dimensions": dimensions, "valid": True, "input_hash": raw["input_hash"]}


def _format_score(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    return f"{value:.1f}"


def _manuscript_timestamp(manifest: dict[str, Any]) -> str:
    value = manifest.get("manuscript_completed_at") or manifest.get("completed_at")
    return str(value).strip() if value else ""


def _format_manuscript_date(manifest: dict[str, Any]) -> str:
    """Display the immutable completion timestamp as a Beijing calendar date."""

    value = _manuscript_timestamp(manifest)
    if not value:
        return "日期未知"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    except ValueError:
        return value[:10] if len(value) >= 10 else value


def _data_number(value: float | None) -> str:
    return "" if value is None or not math.isfinite(value) else f"{value:.1f}"


V4_AGGREGATE_SCHEMA = "novel-eval-aggregate.v4"
V4_RANKING_SCHEMA = "novel-ranking.v4"


class V4DimensionSpec(NamedTuple):
    key: str
    label: str
    weight: float
    subscores: tuple[str, str, str]


# Deliberately separate from V3: naturalness is a positive V4 dimension.
V4_DIMENSION_SPECS = (
    V4DimensionSpec("theme_fulfillment", "题材与主题兑现", 0.10, ("direction", "integration", "depth")),
    V4DimensionSpec("historical_grounding", "时代与现实质感", 0.10, ("plausibility", "specificity", "causal_context")),
    V4DimensionSpec("characters", "人物与关系", 0.15, ("agency", "differentiation", "relationships")),
    V4DimensionSpec("plot_causality", "情节驱动与因果", 0.15, ("conflict", "causality", "escalation")),
    V4DimensionSpec("longform_structure", "长篇结构与连续性", 0.15, ("continuity", "pacing", "payoff")),
    V4DimensionSpec("scene_execution", "场景与叙事效能", 0.15, ("dramatization", "viewpoint", "action_dialogue")),
    V4DimensionSpec("style_control", "文风管理", 0.10, ("precision", "rhythm", "register")),
    V4DimensionSpec("naturalness", "自然度与非模板化", 0.10, ("specificity", "variation", "nonformulaic")),
)
V4_DIMENSION_KEYS = tuple(spec.key for spec in V4_DIMENSION_SPECS)
V4_JUDGE_IDS = ("sol", "grok", "opus", "k3", "ds-v4-pro")
HISTORICAL_JUDGE_LABELS = {"fable": "Fable"}
V4_ALL_EDGE_COUNT = 25
V4_SEVERITIES = frozenset({"none", "minor", "major", "critical"})
V4_SEVERITY_CAPS = {"major": 50.0, "critical": 25.0}


def _v4_number(value: Any) -> float | None:
    """Return a finite 0--100 score, rejecting coercions and booleans."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _format_probability(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _v4_range(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    median = _v4_number(raw.get("median"))
    minimum = _v4_number(raw.get("min"))
    maximum = _v4_number(raw.get("max"))
    if None in (median, minimum, maximum) or minimum > median or median > maximum:
        return None
    return {"median": median, "min": minimum, "max": maximum}


def _blank_v4() -> dict[str, Any]:
    return {
        "valid": False,
        "rank": None,
        "relative_score": None,
        "win_probability": None,
        "ci95": None,
        "ci_overlaps_next": False,
        "overall_score": None,
        "dimensions": {},
        "judges": {},
        "outline_audit": None,
        "percentiles": {},
    }


def _normalise_v4_aggregate(
    raw: dict[str, Any],
    *,
    benchmark: str,
    candidate: str,
    input_hash: str | None,
    provenance: dict[str, Any] | None = None,
    chapters: dict[str, str] | None = None,
    judge_ids: tuple[str, ...] = V4_JUDGE_IDS,
) -> dict[str, Any]:
    """Validate the public V4 aggregate narrowly enough to fail closed.

    V4 is intentionally isolated here while its scorer is developed in parallel.
    Only the published aggregate shape is consumed; raw evaluator output never is.
    """

    blank = _blank_v4()
    if (
        raw.get("schema") != V4_AGGREGATE_SCHEMA
        or raw.get("benchmark") != benchmark
        or raw.get("candidate") != candidate
        or raw.get("status") != "complete"
        or raw.get("eligible_for_ranking") is not True
        or not isinstance(input_hash, str)
        or raw.get("input_hash") != input_hash
        or not isinstance(provenance, dict)
        or raw.get("provenance") != provenance
    ):
        return blank
    overall_score = _v4_number(raw.get("overall_score"))
    raw_dimensions = raw.get("dimensions")
    raw_judges = raw.get("judges")
    if (
        overall_score is None
        or raw.get("expected_judges") != list(judge_ids)
        or raw.get("completed_judges") != list(judge_ids)
        or not isinstance(raw_dimensions, dict)
        or not isinstance(raw_judges, dict)
        or set(raw_judges) != set(judge_ids)
    ):
        return blank
    if set(raw_dimensions) != set(V4_DIMENSION_KEYS):
        return blank

    judges: dict[str, dict[str, Any]] = {}
    for judge_id in judge_ids:
        judge = raw_judges[judge_id]
        if not isinstance(judge, dict) or set(judge) != {"dimensions"}:
            return blank
        judge_dimensions = judge.get("dimensions")
        if not isinstance(judge_dimensions, dict) or set(judge_dimensions) != set(V4_DIMENSION_KEYS):
            return blank
        clean_dimensions: dict[str, dict[str, Any]] = {}
        for spec in V4_DIMENSION_SPECS:
            entry = judge_dimensions[spec.key]
            if not isinstance(entry, dict) or set(entry) != {"subscores", "evidence", "major_defect", "confidence", "score"}:
                return blank
            score = _v4_number(entry.get("score"))
            subscores = entry.get("subscores")
            confidence = _v4_number(entry.get("confidence"))
            if (
                score is None
                or confidence is None
                or not 0 <= confidence <= 1
                or not isinstance(subscores, dict)
                or set(subscores) != set(spec.subscores)
            ):
                return blank
            clean_subscores: dict[str, int] = {}
            for subkey, subscore in subscores.items():
                if (
                    not isinstance(subkey, str)
                    or subkey not in spec.subscores
                    or type(subscore) is not int
                    or not 0 <= subscore <= 4
                ):
                    return blank
                clean_subscores[subkey] = subscore
            if set(clean_subscores) != set(spec.subscores):
                return blank
            evidence = entry.get("evidence")
            defect = entry.get("major_defect")
            if not isinstance(evidence, list) or len(evidence) != 2 or not isinstance(defect, dict):
                return blank
            clean_evidence: list[dict[str, str]] = []
            for item in evidence:
                if not isinstance(item, dict) or set(item) != {"chapter", "excerpt"}:
                    return blank
                chapter, excerpt = item.get("chapter"), item.get("excerpt")
                if not isinstance(chapter, str) or not isinstance(excerpt, str):
                    return blank
                excerpt = re.sub(r"\s+", " ", excerpt).strip()
                if not excerpt or len(excerpt) > 180 or (chapters is not None and (chapter not in chapters or excerpt not in re.sub(r"\s+", " ", chapters[chapter]).strip())):
                    return blank
                clean_evidence.append({"chapter": chapter, "excerpt": excerpt})
            if not {"severity", "description"}.issubset(defect) or not set(defect).issubset({"severity", "description", "chapter"}):
                return blank
            severity, description = defect.get("severity"), defect.get("description")
            description = re.sub(r"\s+", " ", description).strip() if isinstance(description, str) else ""
            if severity not in V4_SEVERITIES or not description or len(description) > 240:
                return blank
            clean_defect: dict[str, Any] = {"severity": severity, "description": description}
            if "chapter" in defect:
                defect_chapter = defect["chapter"]
                if not isinstance(defect_chapter, str) or (chapters is not None and defect_chapter not in chapters):
                    return blank
                clean_defect["chapter"] = defect_chapter
            derived_score = round(sum(clean_subscores.values()) / 12 * 100, 1)
            derived_score = min(derived_score, V4_SEVERITY_CAPS.get(severity, derived_score))
            if score != derived_score:
                return blank
            clean_dimensions[spec.key] = {
                "score": score,
                "subscores": clean_subscores,
                "evidence": clean_evidence,
                "major_defect": clean_defect,
                "confidence": confidence,
            }
        judges[judge_id] = {"dimensions": clean_dimensions}

    dimensions: dict[str, dict[str, Any]] = {}
    for spec in V4_DIMENSION_SPECS:
        aggregate_entry = raw_dimensions[spec.key]
        if not isinstance(aggregate_entry, dict) or set(aggregate_entry) != {"label", "weight", "median", "min", "max", "subscores"}:
            return blank
        if aggregate_entry.get("label") != spec.label or _number(aggregate_entry.get("weight")) != spec.weight:
            return blank
        scores = sorted(judges[judge_id]["dimensions"][spec.key]["score"] for judge_id in judge_ids)
        middle = len(scores) // 2
        expected_median = scores[middle] if len(scores) % 2 else round((scores[middle - 1] + scores[middle]) / 2, 1)
        expected_range = {"median": expected_median, "min": scores[0], "max": scores[-1]}
        if _v4_range(aggregate_entry) != expected_range:
            return blank
        aggregate_subscores = aggregate_entry.get("subscores")
        if not isinstance(aggregate_subscores, dict) or set(aggregate_subscores) != set(spec.subscores):
            return blank
        clean_subscores: dict[str, dict[str, float]] = {}
        for subkey in spec.subscores:
            entry = aggregate_subscores[subkey]
            values = sorted(judges[judge_id]["dimensions"][spec.key]["subscores"][subkey] for judge_id in judge_ids)
            middle = len(values) // 2
            expected_median = float(values[middle]) if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
            expected = {"median": expected_median, "min": values[0], "max": values[-1]}
            if not isinstance(entry, dict) or set(entry) != {"median", "min", "max"} or _v4_range(entry) != expected:
                return blank
            clean_subscores[subkey] = expected
        dimensions[spec.key] = {**expected_range, "label": spec.label, "weight": spec.weight, "subscores": clean_subscores}

    derived_overall = round(sum(dimensions[spec.key]["median"] * spec.weight for spec in V4_DIMENSION_SPECS), 1)
    if overall_score != derived_overall:
        return blank

    return {
        **blank,
        "valid": True,
        "overall_score": derived_overall,
        "dimensions": dimensions,
        "judges": judges,
        "outline_audit": None,
    }


def _normalise_v4_ranking(raw: dict[str, Any], benchmark: str) -> dict[str, dict[str, Any]]:
    """Accept the compact V4 ranking forms while requiring every numeric claim."""

    if raw.get("schema") != V4_RANKING_SCHEMA or raw.get("benchmark") != benchmark:
        return {}
    entries = _first(raw, "ranking", "candidates", "rankings", "models")
    if isinstance(entries, dict):
        entries = [{"candidate": key, **value} for key, value in entries.items() if isinstance(value, dict)]
    if not isinstance(entries, list):
        return {}
    ranked: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return {}
        candidate = _first(entry, "candidate", "model", "model_id", "id")
        rank = _number(entry.get("rank"))
        relative = _finite_number(_first(entry, "relative_score", "relative", "rating", "score"))
        win_probability = _finite_number(_first(entry, "win_probability", "win_rate", "win"))
        ci = _first(entry, "ci95", "rating_ci95", "confidence_interval", "ci")
        if isinstance(ci, dict):
            ci = [_first(ci, "low", "lower", "min"), _first(ci, "high", "upper", "max")]
        if (
            not isinstance(candidate, str)
            or not SAFE_SLUG.fullmatch(candidate)
            or rank is None
            or rank < 1
            or rank != int(rank)
            or win_probability is None
            or relative is None
            or not 0 <= win_probability <= 1
            or not isinstance(ci, list)
            or len(ci) != 2
            or type(entry.get("ci_overlaps_next")) is not bool
        ):
            return {}
        low, high = _finite_number(ci[0]), _finite_number(ci[1])
        if low is None or high is None or low > high or candidate in ranked:
            return {}
        ranked[candidate] = {
            "rank": int(rank),
            "relative_score": relative,
            "win_probability": win_probability,
            "ci95": (low, high),
            "ci_overlaps_next": entry["ci_overlaps_next"],
        }
    return ranked


def _v4_default_ranking_quality(raw: dict[str, Any], results: list[dict[str, Any]]) -> bool:
    """A pilot, disconnected, or unsaturated V4 ranking is preview-only."""

    graph = raw.get("graph")
    graph_connected = (
        raw.get("connected_graph") is True
        or raw.get("graph_connected") is True
        or (isinstance(graph, dict) and graph.get("connected") is True)
    )
    if (
        raw.get("status") != "complete"
        or raw.get("scope") != "all"
        or raw.get("eligible_for_default") is not True
        or not graph_connected
    ):
        return False
    for spec in V4_DIMENSION_SPECS:
        values = sorted(result["v4"]["dimensions"][spec.key]["median"] for result in results)
        if len(set(values)) < 3:
            return False
        # Tukey hinges are sufficient for the default gate: a zero IQR means
        # this dimension cannot distinguish the candidate cohort.
        lower = values[: len(values) // 2]
        upper = values[(len(values) + 1) // 2 :]
        q1 = lower[len(lower) // 2]
        q3 = upper[len(upper) // 2]
        if q3 <= q1:
            return False
    return True


def _load_v4_outline_audit(
    path: Path,
    outline_input_hash: str | None,
    *,
    expected_key: str | None = None,
    expected_identity: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Read the optional audit only when it belongs to the current manuscript."""

    raw = _read_json(path)
    if (
        not isinstance(outline_input_hash, str)
        or not isinstance(expected_key, str)
        or not isinstance(expected_identity, dict)
        or raw.get("outline_input_hash") != outline_input_hash
        or not _valid_outline_audit(raw, expected_key, expected_identity)
    ):
        return None
    return raw["audit"]


def _v4_ranking_binding_current(
    raw: dict[str, Any],
    aggregate_hashes: dict[str, str],
    pairs_dir: Path,
    root: Path,
) -> bool:
    binding = raw.get("input_binding")
    if not isinstance(binding, dict) or set(binding) != {
        "pairwise_rubric_hash", "aggregate_hashes", "pair_record_hashes", "binding_hash"
    }:
        return False
    pair_hashes = binding.get("pair_record_hashes")
    if (
        set(aggregate_hashes) != set(V4_ALL_CANDIDATES)
        or binding.get("aggregate_hashes") != aggregate_hashes
        or not isinstance(pair_hashes, dict)
        or len(pair_hashes) != V4_ALL_EDGE_COUNT
    ):
        return False
    try:
        rubric_hash = _sha256_text(load_v4_pairwise_prompt(root))
    except Exception:
        return False
    if binding.get("pairwise_rubric_hash") != rubric_hash:
        return False
    current_pair_hashes: dict[str, str] = {}
    for pair_id, expected_hash in pair_hashes.items():
        if not isinstance(pair_id, str) or not re.fullmatch(r"[0-9a-f]{20}", pair_id) or not isinstance(expected_hash, str):
            return False
        path = pairs_dir / f"{pair_id}.json"
        if not path.is_file():
            return False
        current_pair_hashes[pair_id] = _sha256_text(_canonical_json(_read_json(path)))
    payload = {
        "pairwise_rubric_hash": rubric_hash,
        "aggregate_hashes": aggregate_hashes,
        "pair_record_hashes": current_pair_hashes,
    }
    return (
        current_pair_hashes == pair_hashes
        and binding.get("binding_hash") == _sha256_text(_canonical_json(payload))
        and len(pair_hashes) == raw.get("completed_edges") == raw.get("expected_edges") == V4_ALL_EDGE_COUNT
    )


def attach_v4_results(results: list[dict[str, Any]], results_dir: Path) -> bool:
    """Attach valid V4 data and return whether it is safe to be the default."""

    aggregate_hashes: dict[str, str] = {}
    for result in results:
        v4_input_hash = None
        outline_input_hash = None
        v4_chapters = None
        v4_provenance = None
        outline_key = None
        outline_identity = None
        submission = None
        if result["detail_available"]:
            try:
                submission = load_v4_submission(
                    results_dir.parents[1], results_dir.name, result["model_id"]
                )
                v4_input_hash = submission.input_hash
                v4_chapters = submission.chapters
                v4_provenance = expected_aggregate_provenance(
                    results_dir.parents[1], submission
                )
            except Exception:
                # Main V4 validity is bound only to direction + verified prose.
                submission = None
                v4_input_hash = None
                v4_chapters = None
                v4_provenance = None
            try:
                if submission is None:
                    raise ValueError("main V4 submission is unavailable")
                outline_submission = load_outline_audit_submission(
                    results_dir.parents[1], submission
                )
                outline_input_hash = outline_submission.outline_input_hash
                cfg = load_llm_config(results_dir.parents[1] / "config.yaml")
                sol = resolve_v4_judge_configs(cfg)["sol"]
                outline_prompt = load_v4_outline_prompt(results_dir.parents[1])
                outline_key = outline_audit_cache_key(
                    outline_submission, outline_prompt, sol, V4_REQUEST_OVERRIDES
                )
                outline_identity = outline_audit_identity(
                    outline_submission, outline_prompt, sol, V4_REQUEST_OVERRIDES
                )
            except Exception:
                # The advisory outline audit never blocks main-score validity.
                outline_input_hash = None
                outline_key = None
                outline_identity = None
        aggregate = _read_json(results_dir / result["model_id"] / "scores-v4" / "aggregate.json")
        result["v4"] = _normalise_v4_aggregate(
            aggregate,
            benchmark=results_dir.name,
            candidate=result["model_id"],
            input_hash=v4_input_hash,
            provenance=v4_provenance,
            chapters=v4_chapters,
        )
        if result["v4"]["valid"]:
            aggregate_hashes[result["model_id"]] = _sha256_text(_canonical_json(aggregate))
        result["v4"]["outline_audit"] = _load_v4_outline_audit(
            results_dir / result["model_id"] / "scores-v4" / "outline-audit.json",
            outline_input_hash,
            expected_key=outline_key,
            expected_identity=outline_identity,
        )
    ranking_raw = _read_json(results_dir / "_pairwise-v4" / "ranking.json")
    binding_valid = _v4_ranking_binding_current(
        ranking_raw,
        aggregate_hashes,
        results_dir / "_pairwise-v4" / "pairs",
        results_dir.parents[1],
    )
    ranking = _normalise_v4_ranking(ranking_raw, results_dir.name) if binding_valid else {}
    _attach_v4_percentiles([result for result in results if result["v4"]["valid"]])
    published = [result for result in results if result["detail_available"]]
    if not published or any(not result["v4"]["valid"] for result in published):
        return False
    if set(ranking) != {result["model_id"] for result in published}:
        return False
    ranks = {ranking[result["model_id"]]["rank"] for result in published}
    if ranks != set(range(1, len(published) + 1)):
        return False
    if not _v4_default_ranking_quality(ranking_raw, published):
        return False
    for result in results:
        entry = ranking.get(result["model_id"])
        if entry:
            result["v4"].update(entry)
    return True


def _attach_v4_percentiles(results: list[dict[str, Any]]) -> None:
    """Use average ordinal rank; the cohort median is therefore conceptually 50."""

    for spec in V4_DIMENSION_SPECS:
        values = sorted(
            (result["v4"]["dimensions"][spec.key]["median"], result)
            for result in results
        )
        size = len(values)
        offset = 0
        while offset < size:
            end = offset + 1
            while end < size and values[end][0] == values[offset][0]:
                end += 1
            percentile = 50.0 if size == 1 else 100 * ((offset + end - 1) / 2) / (size - 1)
            for _, result in values[offset:end]:
                result["v4"]["percentiles"][spec.key] = percentile
            offset = end


def load_reform_results(
    results_dir: Path,
    model_by_id: dict[str, dict[str, Any]],
    model_order: list[str] | None = None,
    protocol_by_model: dict[str, dict[str, str]] | None = None,
    score_by_judge: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Return one row per configured model, including pending models."""

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
                benchmark=results_dir.name,
                judge_id=judge_id,
                candidate=model_id,
                expected=(score_by_judge or {}).get(judge_id),
            )

        all_judges_valid = all(
            judges[judge_id]["valid"] for judge_id in JUDGE_IDS
        )
        score_input_hash = None
        if all_judges_valid:
            input_hashes = {
                judges[judge_id]["input_hash"] for judge_id in JUDGE_IDS
            }
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

        source_scores_valid = detail_available and all_judges_valid
        derived_dimensions: dict[str, dict[str, Any]] = {}
        derived_overall = None
        if source_scores_valid:
            derived_dimensions = aggregate_dimension_scores(
                {
                    judge_id: judges[judge_id]["dimensions"]
                    for judge_id in JUDGE_IDS
                }
            )
            derived_overall = overall_score_from_medians(
                {
                    key: derived_dimensions[key]["median"]
                    for key in DIMENSION_KEYS
                }
            )

        aggregate_path = model_dir / "scores" / "aggregate.json"
        aggregate = _read_json(aggregate_path)
        expected_aggregate_judges = {
            judge_id: {"dimensions": judges[judge_id]["dimensions"]}
            for judge_id in JUDGE_IDS
        }
        aggregate_allows_ranking = (
            source_scores_valid
            and aggregate_path.is_file()
            and aggregate.get("schema") == AGGREGATE_SCHEMA_VERSION
            and aggregate.get("benchmark") == results_dir.name
            and aggregate.get("candidate") == model_id
            and aggregate.get("input_hash") == score_input_hash
            and aggregate.get("expected_judges") == list(JUDGE_IDS)
            and aggregate.get("completed_judges") == list(JUDGE_IDS)
            and aggregate.get("status") == "complete"
            and aggregate.get("eligible_for_ranking") is True
            and aggregate.get("judges") == expected_aggregate_judges
            and aggregate.get("dimensions") == derived_dimensions
            and aggregate.get("overall_score") == derived_overall
        )
        rankable = bool(aggregate_allows_ranking)
        aggregate_dimensions = derived_dimensions if rankable else {}
        overall_score = derived_overall if rankable else None

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
                "manuscript_completed_at": _manuscript_timestamp(manifest),
                "manuscript_date": _format_manuscript_date(manifest),
                "novel": novel,
                "novel_html": md_to_html(prose_only(novel)),
                "body_chars": count_chinese_chars(novel),
                "chapters": count_chapters(novel),
                "judges": judges,
                "judge_ids": JUDGE_IDS,
                "aggregate_dimensions": aggregate_dimensions,
                "overall_score": overall_score,
                "score_input_hash": current_input_hash,
                "detail_available": detail_available,
                "rankable": rankable,
                "archived": False,
                "archives": [],
            }
        )

    results.sort(
        key=lambda item: (
            not item["rankable"],
            -(item["overall_score"] or 0),
            item["config_order"],
        )
    )
    return results


def _archived_judge_expectation(raw: dict[str, Any]) -> dict[str, str] | None:
    keys = ("rubric_hash", "judge_config_sha256", "requested_model")
    if any(not isinstance(raw.get(key), str) or not raw[key] for key in keys):
        return None
    return {key: str(raw[key]) for key in keys}


def _archived_judge_ids(archived_dir: Path) -> tuple[str, ...]:
    """Recover the immutable judge cohort that belongs to an archived draft."""

    aggregate = _read_json(archived_dir / "scores" / "aggregate.json")
    raw_ids = aggregate.get("expected_judges")
    if isinstance(raw_ids, list):
        judge_ids = tuple(str(value) for value in raw_ids)
        if (
            judge_ids
            and len(judge_ids) == len(set(judge_ids))
            and all(SAFE_SLUG.fullmatch(value) for value in judge_ids)
        ):
            return judge_ids
    score_dir = archived_dir / "scores"
    if not score_dir.is_dir():
        return ()
    return tuple(
        path.stem
        for path in sorted(score_dir.glob("*.json"), key=lambda path: path.name)
        if path.stem != "aggregate" and SAFE_SLUG.fullmatch(path.stem)
    )


def _historical_median(values: list[float]) -> float:
    ordered = sorted(Decimal(str(value)) for value in values)
    middle = len(ordered) // 2
    value = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / Decimal("2")
    )
    return float(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _aggregate_historical_dimensions(
    judges: dict[str, dict[str, Any]], judge_ids: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    """Recompute an archived cohort without applying today's judge registry."""

    aggregate: dict[str, dict[str, Any]] = {}
    for spec in DIMENSION_SPECS:
        values = [float(judges[judge_id]["dimensions"][spec.key]["score"]) for judge_id in judge_ids]
        aggregate[spec.key] = {
            "label": spec.label,
            "weight": spec.weight,
            "higher_is_better": spec.higher_is_better,
            "median": _historical_median(values),
            "min": min(values),
            "max": max(values),
        }
    return aggregate


def load_archived_reform_results(
    results_dir: Path,
    model_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Load browsable historical manuscripts without making them rankable."""

    root = results_dir.parents[1]
    archived_results: list[dict[str, Any]] = []
    for model_id, config_model in model_by_id.items():
        archive_root = results_dir / model_id / ARCHIVE_DIR_NAME
        if not archive_root.is_dir():
            continue
        for archived_dir in sorted(archive_root.iterdir(), key=lambda path: path.name):
            if not archived_dir.is_dir() or not SAFE_SLUG.fullmatch(archived_dir.name):
                continue
            manifest = _read_json(archived_dir / "manifest.json")
            archive_meta = _read_json(archived_dir / "archive.json")
            run_id = str(manifest.get("run_id") or "")
            status = str(manifest.get("status") or "").strip().lower()
            detail_available = (
                bool(run_id)
                and status in {"complete", "completed"}
                and all((archived_dir / name).is_file() for name in REQUIRED_RESULT_ARTIFACTS)
                and _manifest_artifacts_match(archived_dir, manifest)
                and result_is_complete(archived_dir, run_id)
            )
            if not detail_available:
                print(f"[site] 忽略未通过完整性校验的归档稿：{archived_dir}", file=sys.stderr)
                continue

            book = _read_json(archived_dir / "book.json")
            macro_outline = _read_json(archived_dir / "macro_outline.json")
            opening_outline = _read_json(archived_dir / "opening_outline.json")
            try:
                novel = (archived_dir / "novel.md").read_text(encoding="utf-8")
                archived_candidate = str(manifest.get("model_id") or model_id)
                submission = load_submission_from_dir(
                    root,
                    results_dir.name,
                    archived_candidate,
                    archived_dir,
                    expected_run_id=run_id,
                )
            except (OSError, ScoreError, ValueError):
                print(f"[site] 忽略无法读取的归档稿：{archived_dir}", file=sys.stderr)
                continue

            judge_ids = _archived_judge_ids(archived_dir)
            judges: dict[str, dict[str, Any]] = {}
            for judge_id in judge_ids:
                raw = _read_json(archived_dir / "scores" / f"{judge_id}.json")
                judges[judge_id] = _normalise_judge(
                    raw,
                    benchmark=results_dir.name,
                    judge_id=judge_id,
                    candidate=archived_candidate,
                    expected=_archived_judge_expectation(raw),
                )
            all_judges_valid = bool(judge_ids) and all(
                judges[judge_id]["valid"] for judge_id in judge_ids
            )
            if all_judges_valid:
                score_hashes = {judges[judge_id]["input_hash"] for judge_id in judge_ids}
                all_judges_valid = score_hashes == {submission.input_hash}
            aggregate_dimensions = (
                _aggregate_historical_dimensions(judges, judge_ids)
                if all_judges_valid
                else {}
            )
            overall_score = (
                overall_score_from_medians(aggregate_dimensions)
                if all_judges_valid
                else None
            )

            archived_v4 = _blank_v4()
            try:
                v4_submission = load_v4_submission_from_dir(
                    root, results_dir.name, archived_candidate, archived_dir
                )
                raw_v4 = _read_json(archived_dir / "scores-v4" / "aggregate.json")
                archived_v4 = _normalise_v4_aggregate(
                    raw_v4,
                    benchmark=results_dir.name,
                    candidate=archived_candidate,
                    input_hash=v4_submission.input_hash,
                    provenance=(raw_v4.get("provenance") if isinstance(raw_v4, dict) else None),
                    chapters=v4_submission.chapters,
                    judge_ids=tuple(raw_v4.get("expected_judges", ())),
                )
            except Exception:
                archived_v4 = _blank_v4()

            title = _first(book, "title") or first_h1(novel) or model_id
            archived_results.append(
                {
                    "model_id": model_id,
                    "model_name": str(
                        manifest.get("requested_model")
                        or config_model.get("name", model_id)
                    ),
                    "title": title,
                    "blurb": _first(book, "blurb", "intro", "synopsis") or "暂无简介。",
                    "book": book,
                    "macro_outline": macro_outline,
                    "opening_outline": opening_outline,
                    "manifest": manifest,
                    "manuscript_completed_at": _manuscript_timestamp(manifest),
                    "manuscript_date": _format_manuscript_date(manifest),
                    "novel": novel,
                    "novel_html": md_to_html(prose_only(novel)),
                    "body_chars": count_chinese_chars(novel),
                    "chapters": count_chapters(novel),
                    "judges": judges,
                    "judge_ids": judge_ids,
                    "aggregate_dimensions": aggregate_dimensions,
                    "overall_score": overall_score,
                    "score_input_hash": submission.input_hash,
                    "detail_available": True,
                    "rankable": False,
                    "archived": True,
                    "archive_id": archived_dir.name,
                    "archive_meta": archive_meta,
                    "v4": archived_v4,
                    "archives": [],
                }
            )
    archived_results.sort(
        key=lambda item: (item["model_id"], item["manuscript_completed_at"]), reverse=True
    )
    return archived_results


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


PUBLIC_PROTOCOLS = ("v3", "v3-reagg", "v5", "v4")


def resolve_public_protocol(
    *,
    cli: str | None,
    output_dir_name: str,
    v5_default: bool = False,
) -> str:
    """Closed production set is {v3, v3-reagg, v5}. V4 never wins via attach_v4."""

    if cli:
        if cli not in PUBLIC_PROTOCOLS:
            raise ValueError(f"未知公开协议：{cli}")
        return cli
    if output_dir_name == "v4-preview" or output_dir_name.startswith(".v4-preview.build-"):
        return "v4"
    if output_dir_name == "v5-preview" or output_dir_name.startswith(".v5-preview.build-"):
        return "v5"
    if v5_default:
        return "v5"
    return "v3-reagg"


def page_head(
    title: str,
    root_prefix: str,
    body_class: str,
    *,
    leaderboard_script: bool = False,
    skip_href: str = "#main",
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
<a class="skip-link" href="{esc(skip_href)}">跳到正文</a>
<header class="site-header">
  <div class="header-inner">
    <a class="brand" href="{root_prefix}index.html" aria-label="{SITE_TITLE}首页">
      <span class="brand-mark" aria-hidden="true">文</span>
      <span>{SITE_TITLE}</span>
    </a>
    <nav class="site-nav" aria-label="主导航">
      <a href="{root_prefix}index.html">榜单</a>
      <a href="{root_prefix}history/index.html">V2.1 历史</a>
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
  <a href="{REPO_URL}" rel="noopener">源码</a>
</footer>
</body>
</html>
"""

DIMENSION_SHORT_LABELS = {
    "theme_fulfillment": "主题",
    "historical_grounding": "时代",
    "characters": "人物",
    "plot_causality": "因果",
    "longform_structure": "结构",
    "scene_execution": "场景",
    "style_control": "文风",
    "ai_flavor": "AI味↓",
    "naturalness": "自然度",
}

SCORING_NOTE = (
    "当前公开口径是 V3 重聚合，不是新评：名次用可靠性加权 T 分，"
    "雷达是评委票内残差（50 = 本书八维均值）。"
    f"历史 V3 口径仍取 {len(JUDGE_IDS)} 位活动评委票的中位数"
    f"（{'、'.join(JUDGE_LABELS[judge_id] for judge_id in JUDGE_IDS)}）；"
    "综合按固定权重加总，其中AI味使用100减原值。"
    "AI味越低越好，其余指标越高越好。T 分相对当前可排名队列，不可跨周当绝对分。"
)


def _judge_label(judge_id: str) -> str:
    return JUDGE_LABELS.get(
        judge_id, HISTORICAL_JUDGE_LABELS.get(judge_id, judge_id)
    )


def _dimension_label(spec: Any) -> str:
    return f"{spec.label}（越低越好）" if not getattr(spec, "higher_is_better", True) else spec.label


def _dimension_short(spec: Any) -> str:
    return DIMENSION_SHORT_LABELS[spec.key]


def _metric_key(spec: Any | None = None, *, overall: bool = False) -> str:
    if overall:
        return "overall"
    assert spec is not None
    return spec.key.replace("_", "-")


def _profile_spark(result: dict[str, Any]) -> str:
    reagg = result.get("reagg") or {}
    residual_p = reagg.get("residual_p") if reagg.get("status") == REAGG_COMPLETE else None
    if not residual_p:
        return ""
    bars = []
    for spec in DIMENSION_SPECS:
        offset = float(residual_p.get(spec.key, 50.0)) - 50.0
        bars.append(
            f'<span class="spark-bar" style="--spark:{offset:.1f}" '
            f'title="{esc(_dimension_short(spec))} {offset:+.1f}"></span>'
        )
    return f'<span class="profile-spark" aria-hidden="true">{"".join(bars)}</span>'


def _leaderboard_row(result: dict[str, Any], rank: int | None) -> str:
    if result["detail_available"]:
        history = len(result.get("archives") or [])
        history_text = f" · 历史稿 {history}" if history else ""
        entry = f"""<a class="entry-link" href="results/reform-era/{result['model_id']}.html">
      <span class="entry-model">{esc(result['model_name'])}</span>
      <span class="entry-title">《{esc(result['title'])}》</span>
      <span class="entry-date">成稿 {esc(result['manuscript_date'])}{history_text}</span>
      {_profile_spark(result)}
    </a>"""
    else:
        entry = f"""<span class="entry-link unavailable">
      <span class="entry-model">{esc(result['model_name'])}</span>
      <span class="entry-title">{esc(result['title'])}</span>
    </span>"""
    rank_text = f"{rank:02d}" if rank is not None else ""
    reagg = result.get("reagg") or {}
    tscore = reagg.get("tscore") if reagg.get("status") == REAGG_COMPLETE else None
    tie_next = bool(reagg.get("ties_with_next"))
    tie_mark = (
        '<span class="ci-overlap" data-tie-mark>=</span>' if tie_next else ""
    )
    dimensions = result["aggregate_dimensions"]
    data_dimensions = "\n".join(
        (
            f'    data-{spec.key.replace("_", "-")}="'
            f'{_data_number((dimensions.get(spec.key) or {}).get("median"))}"'
        )
        for spec in DIMENSION_SPECS
    )
    score_cells = "\n".join(
        (
            f'  <td class="metric-col" data-metric="{_metric_key(spec)}" '
            f'data-label="{esc(_dimension_label(spec))}" hidden>'
            f'{_format_score((dimensions.get(spec.key) or {}).get("median"))}</td>'
        )
        for spec in DIMENSION_SPECS
    )
    return f"""<tr data-model-id="{esc(result['model_id'])}" data-model="{esc(result['model_name'])}"
    data-config-order="{result['config_order']}"
    data-rankable="{'true' if result['rankable'] else 'false'}"
    data-tie-next="{'true' if tie_next else 'false'}"
    data-tscore="{_data_number(tscore)}"
    data-overall="{_data_number(result['overall_score'])}"
{data_dimensions}>
  <td class="rank-cell" data-rank>{rank_text}{tie_mark}</td>
  <th scope="row">
    {entry}
  </th>
  <td class="metric-col" data-metric="tscore" data-label="T分">{_format_score(tscore)}</td>
  <td class="metric-col" data-metric="overall" data-label="V3综合" hidden>{_format_score(result['overall_score'])}</td>
{score_cells}
</tr>"""


def _v4_heat_cell(value: float | None, spec: Any) -> str:
    if value is None:
        return '<td class="v4-heat" data-label="—">—</td>'
    label = f"{_dimension_short(spec)}：{value:.0f}百分位"
    return (
        f'<td class="v4-heat" data-label="{esc(label)}" style="--heat: {value:.1f}%">'
        f'<span>{value:.0f}</span></td>'
    )


def _v4_leaderboard_row(result: dict[str, Any]) -> str:
    v4 = result["v4"]
    detail = (
        f'<a class="entry-link" href="results/reform-era/{esc(result["model_id"])}.html">'
        f'<span class="entry-model">{esc(result["model_name"])}</span>'
        f'<span class="entry-title">《{esc(result["title"])}》</span></a>'
        if result["detail_available"]
        else f'<span class="entry-link unavailable"><span class="entry-model">{esc(result["model_name"])}</span></span>'
    )
    ci = v4.get("ci95")
    ci_text = f"{ci[0]:.1f}–{ci[1]:.1f}" if ci else "—"
    overlap = '<span class="ci-overlap">无法可靠区分</span>' if v4.get("ci_overlaps_next") else ""
    return f"""<tr data-model-id="{esc(result['model_id'])}" data-rankable="{'true' if v4['valid'] else 'false'}">
  <td class="rank-cell">{v4['rank']:02d}</td><th scope="row">{detail}</th>
  <td data-label="相对分">{_format_score(v4['relative_score'])}</td>
  <td data-label="胜率">{_format_probability(v4['win_probability'])}</td>
  <td data-label="95% CI">{ci_text}{overlap}</td><td data-label="绝对分">{_format_score(v4['overall_score'])}</td>
  {''.join(_v4_heat_cell(v4['percentiles'].get(spec.key), spec) for spec in V4_DIMENSION_SPECS)}
</tr>"""


def _v4_preview(results: list[dict[str, Any]], *, complete: bool) -> str:
    valid_results = [result for result in results if result.get("v4", {}).get("valid")]
    if complete:
        valid_results.sort(key=lambda result: result["v4"]["rank"])
    rows = "".join(_v4_leaderboard_row(result) for result in valid_results if complete)
    if not complete:
        rows = "".join(
            f"<tr><th scope=\"row\">{esc(result['model_name'])}</th><td>{_format_score(result['v4']['overall_score'])}</td>"
            + "".join(
                _v4_heat_cell(result["v4"]["percentiles"].get(spec.key), spec)
                for spec in V4_DIMENSION_SPECS
            )
            + "</tr>"
            for result in valid_results
        )
    headers = "".join(f"<th scope=\"col\">{esc(_dimension_short(spec))}</th>" for spec in V4_DIMENSION_SPECS)
    head = (
        "<th scope=\"col\">#</th><th scope=\"col\">作品</th><th scope=\"col\">相对分</th>"
        "<th scope=\"col\">胜率</th><th scope=\"col\">95% CI</th><th scope=\"col\">绝对分</th>"
        if complete
        else "<th scope=\"col\">作品</th><th scope=\"col\">绝对分</th>"
    )
    if not valid_results:
        rows = f'<tr><td colspan="{14 if complete else 10}">暂无可校验的 V4 聚合。</td></tr>'
    status = (
        "V4 已覆盖全部可发布候选，当前为默认排名口径。"
        if complete
        else "预览未满足发布条件：必须有全部可发布候选的最新 V4 聚合及完整 pairwise 排名；生产首页仍使用 V3。"
    )
    return f"""<section class="v4-panel {'v4-preview-mode' if not complete else ''}" aria-labelledby="v4-title">
  <header><h2 id="v4-title">V4 对比排名</h2><p>{status}</p></header>
  <div class="table-shell"><table class="leaderboard v4-leaderboard">
    <thead><tr>{head}{headers}</tr></thead><tbody>{rows}</tbody>
  </table></div>
  <p class="v4-legend">相对分、胜率与 95% CI 来自成对比较；绝对分来自 V4 评分聚合。热图为 8 个维度的队列百分位，50 表示队列中位。</p>
</section>"""


def render_v4_home(results: list[dict[str, Any]], legacy_count: int, *, preview: bool) -> str:
    return page_head("改革开放长篇模型榜 · V4", "", "page-leaderboard page-v4", leaderboard_script=False) + f"""
<header class="page-intro" aria-labelledby="page-title">
  <h1 id="page-title">改革开放长篇模型榜</h1>
  <p class="page-sub">V4 · 成对比较 + 绝对评分 · {len(V4_JUDGE_IDS)} 位活动评委</p>
  <p class="page-meta">全部 {len(results)} · Legacy {legacy_count}</p>
</header>
{_v4_preview(results, complete=not preview or all(result.get('v4', {}).get('rank') for result in results if result['detail_available']))}
""" + PAGE_FOOT


def render_home(
    results: list[dict[str, Any]],
    legacy_count: int,
    *,
    public_protocol: str = "v3-reagg",
    v4_default: bool = False,
    v4_preview: bool = False,
) -> str:
    if v4_default or public_protocol == "v4":
        return render_v4_home(results, legacy_count, preview=not v4_default and v4_preview)
    display = list(results)
    if public_protocol == "v3-reagg":
        display.sort(
            key=lambda item: (
                not item["rankable"],
                -(
                    (item.get("reagg") or {}).get("tscore")
                    if (item.get("reagg") or {}).get("status") == REAGG_COMPLETE
                    else -1e9
                ),
                item["config_order"],
            )
        )
    ranked_count = sum(1 for result in display if result["rankable"])
    rank = 0
    rendered_rows: list[str] = []
    for result in display:
        if result["rankable"]:
            rank += 1
            rendered_rows.append(_leaderboard_row(result, rank))
        else:
            rendered_rows.append(_leaderboard_row(result, None))
    rows = "\n".join(rendered_rows)
    model_count = len(results)
    if rows:
        dimension_headers = "".join(
            (
                f'<th class="metric-col" scope="col" data-metric="{_metric_key(spec)}" '
                f'title="{esc(_dimension_label(spec))}" hidden>'
                f'{esc(_dimension_short(spec))}</th>'
            )
            for spec in DIMENSION_SPECS
        )
        board = f"""
<div class="table-shell">
  <table class="leaderboard" aria-describedby="ranking-note">
    <thead><tr>
      <th scope="col">#</th>
      <th scope="col">作品</th>
      <th class="metric-col" scope="col" data-metric="tscore">T分</th>
      <th class="metric-col" scope="col" data-metric="overall" hidden>V3综合</th>
      {dimension_headers}
    </tr></thead>
    <tbody id="leaderboard-body">{rows}</tbody>
  </table>
</div>"""
    else:
        board = """<div class="empty-state" role="status">
  <strong>榜单还没有作品。</strong>
  <span>把结果放入 <code>results/reform-era/&lt;model&gt;/</code> 后重新构建站点。</span>
</div>"""

    use_tscore = public_protocol == "v3-reagg"
    metric_buttons = [
        '<button type="button" data-sort="tscore" data-direction="desc" '
        f'aria-pressed="{"true" if use_tscore else "false"}" title="T分">T分</button>',
        '<button type="button" data-sort="overall" data-direction="desc" '
        f'aria-pressed="{"false" if use_tscore else "true"}" title="V3 历史综合">V3综合</button>',
    ]
    for spec in DIMENSION_SPECS:
        metric_buttons.append(
            f'<button type="button" data-sort="{_metric_key(spec)}" '
            f'data-direction="{"desc" if spec.higher_is_better else "asc"}" '
            f'aria-pressed="false" title="{esc(_dimension_label(spec))}">'
            f"{esc(_dimension_short(spec))}</button>"
        )
    chip = (
        '<p class="protocol-chip">V3 重聚合 · 非新评 · 相对当前可排名队列</p>'
        if public_protocol == "v3-reagg"
        else ""
    )

    return page_head(
        "改革开放长篇模型榜", "", "page-leaderboard", leaderboard_script=True
    ) + f"""
<header class="page-intro" aria-labelledby="page-title">
  <h1 id="page-title">改革开放长篇模型榜</h1>
  <p class="protocol-chip">V2.1 历史赛道 · 已冻结 · 不是新开局文风榜</p>
  <p class="page-sub">同一方向 · 约 5 万字开篇 · {len(JUDGE_IDS)} 评委盲评</p>
  {chip}
  <p class="page-meta">已评分 {ranked_count} / 全部 {model_count} · 评委 {len(JUDGE_IDS)} · Legacy {legacy_count}</p>
</header>

<section class="ranking-panel" aria-labelledby="ranking-title">
  <div class="ranking-toolbar">
    <h2 id="ranking-title" class="visually-hidden">排名</h2>
    <div class="metric-switch" role="group" aria-label="排名指标">
      {''.join(metric_buttons)}
    </div>
    <div class="rank-ruler" data-rank-ruler>
      <label class="rank-limit-label" for="rank-limit">显示</label>
      <input id="rank-limit" type="range" min="1" max="{max(ranked_count, 1)}"
        value="{max(ranked_count, 1)}" {'disabled' if not ranked_count else ''}>
      <output for="rank-limit" id="rank-limit-output">{'前 ' + str(ranked_count) if ranked_count else '—'}</output>
    </div>
  </div>
  {board}
  <details class="info-drawer" id="ranking-note-drawer">
    <summary>评分怎么算</summary>
    <p id="ranking-note">{SCORING_NOTE}</p>
  </details>
</section>
{_v4_preview(results, complete=False) if v4_preview else ''}
""" + PAGE_FOOT


def _radar_point(cx: float, cy: float, radius: float, angle: float) -> str:
    return f"{cx + radius * math.cos(angle):.1f},{cy + radius * math.sin(angle):.1f}"


def _radar_label_lines(spec: Any) -> tuple[str, ...]:
    if not getattr(spec, "higher_is_better", True):
        return (spec.label, "（越低越好）")
    if "与" in spec.label:
        left, right = spec.label.split("与", 1)
        return (f"{left}与", right)
    return (spec.label,)


def _radar_short_label(spec: Any) -> str:
    return {
        "theme_fulfillment": "主题",
        "historical_grounding": "时代",
        "characters": "人物",
        "plot_causality": "因果",
        "longform_structure": "结构",
        "scene_execution": "场景",
        "style_control": "文风",
        "ai_flavor": "AI味↓",
        "naturalness": "自然度",
    }[spec.key]


def _radar_chart(
    chart_id: str,
    title: str,
    scores: dict[str, float | None],
    *,
    series_kind: str,
    ranges: dict[str, dict[str, float]] | None = None,
    already_oriented: bool = False,
    specs: tuple[Any, ...] = DIMENSION_SPECS,
    baseline: float | None = None,
) -> str:
    """Render an accessible dependency-free radar plus a visible score table."""

    safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", chart_id)
    cx, cy, radius, label_radius = 360.0, 310.0, 185.0, 255.0
    angles = [
        -math.pi / 2 + index * (2 * math.pi / len(specs))
        for index in range(len(specs))
    ]
    grid = "\n".join(
        (
            f'<polygon points="{" ".join(_radar_point(cx, cy, radius * level / 5, angle) for angle in angles)}" '
            f'class="radar-grid-line" />'
        )
        for level in range(1, 6)
    )
    axes = "\n".join(
        f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{_radar_point(cx, cy, radius, angle).split(",")[0]}" '
        f'y2="{_radar_point(cx, cy, radius, angle).split(",")[1]}" class="radar-axis" />'
        for angle in angles
    )

    plotted: list[float] = []
    raw_values: list[float | None] = []
    for spec in specs:
        raw = scores.get(spec.key)
        if raw is None or not math.isfinite(raw):
            raw_values.append(None)
            plotted.append(0.0)
            continue
        raw_values.append(raw)
        plotted.append(raw if already_oriented else dimension_radar_value(spec.key, raw))
    shape_points = " ".join(
        _radar_point(cx, cy, radius * value / 100, angle)
        for value, angle in zip(plotted, angles)
    )
    baseline_mark = ""
    if baseline is not None:
        baseline_points = " ".join(
            _radar_point(cx, cy, radius * baseline / 100, angle) for angle in angles
        )
        baseline_mark = (
            f'<polygon points="{baseline_points}" class="radar-baseline" />'
            f'<path class="radar-lune" fill-rule="evenodd" '
            f'd="M {shape_points} Z M {baseline_points} Z" />'
        )
    point_marks = "\n".join(
        (
            f'<circle cx="{_radar_point(cx, cy, radius * value / 100, angle).split(",")[0]}" '
            f'cy="{_radar_point(cx, cy, radius * value / 100, angle).split(",")[1]}" '
            f'r="4.5" class="radar-point" />'
        )
        for value, angle in zip(plotted, angles)
    )
    band = ""
    if ranges:
        minimums: list[float] = []
        maximums: list[float] = []
        for spec in specs:
            value_range = ranges.get(spec.key) or {}
            minimum = _number(value_range.get("min"))
            maximum = _number(value_range.get("max"))
            if minimum is None or maximum is None:
                minimums.append(0.0)
                maximums.append(0.0)
            else:
                low = minimum if already_oriented else dimension_radar_value(spec.key, minimum)
                high = maximum if already_oriented else dimension_radar_value(spec.key, maximum)
                minimums.append(min(low, high))
                maximums.append(max(low, high))
        outer = " ".join(
            _radar_point(cx, cy, radius * value / 100, angle)
            for value, angle in zip(maximums, angles)
        )
        inner = " ".join(
            _radar_point(cx, cy, radius * value / 100, angle)
            for value, angle in zip(minimums, angles)
        )
        band = f'<polygon points="{outer}" class="radar-band" /><polygon points="{inner}" class="radar-band-inner" />'
    labels: list[str] = []
    for spec, angle in zip(specs, angles):
        x, y = _radar_point(cx, cy, label_radius, angle).split(",")
        cosine = math.cos(angle)
        anchor = "start" if cosine > 0.25 else "end" if cosine < -0.25 else "middle"
        lines = _radar_label_lines(spec)
        line_height = 20
        first_y = float(y) - (len(lines) - 1) * line_height / 2
        tspans = "".join(
            f'<tspan x="{x}" y="{first_y + index * line_height:.1f}">{esc(line)}</tspan>'
            for index, line in enumerate(lines)
        )
        labels.append(
            f'<text text-anchor="{anchor}" '
            f'class="radar-axis-label radar-axis-label-full">{tspans}</text>'
            f'<text x="{x}" y="{y}" text-anchor="{anchor}" '
            f'class="radar-axis-label radar-axis-label-short">'
            f'{esc(_radar_short_label(spec))}</text>'
        )

    descriptions: list[str] = []
    rows: list[str] = []
    for spec, raw, plotted_value in zip(specs, raw_values, plotted):
        raw_text = _format_score(raw)
        value_range = (ranges or {}).get(spec.key) or {}
        range_text = (
            f"{_format_score(_number(value_range.get('min')))}–{_format_score(_number(value_range.get('max')))}"
            if ranges else ""
        )
        if raw is None:
            descriptions.append(f"{_dimension_label(spec)}暂无评分")
        elif getattr(spec, "higher_is_better", True):
            descriptions.append(f"{spec.label}{raw_text}分")
        else:
            descriptions.append(
                f"{spec.label}{raw_text}分，雷达按控制度{plotted_value:.1f}绘制"
            )
        rows.append(
            f"<tr><th scope=\"row\">{esc(_dimension_label(spec))}</th>"
            f"<td>{raw_text}</td>{f'<td>{range_text}</td>' if ranges else ''}</tr>"
        )
    desc = "；".join(descriptions) + "。雷达越靠外代表该项表现越好。"
    return f"""<figure class="radar-figure" data-radar-chart="{esc(safe_id)}" data-series-kind="{esc(series_kind)}">
  <svg class="radar-chart" viewBox="0 0 720 620" role="img"
       aria-labelledby="{esc(safe_id)}-title {esc(safe_id)}-desc">
    <title id="{esc(safe_id)}-title">{esc(title)}</title>
    <desc id="{esc(safe_id)}-desc">{esc(desc)}</desc>
    <defs>
      <pattern id="{esc(safe_id)}-hatch" width="10" height="10"
               patternUnits="userSpaceOnUse" patternTransform="rotate(35)">
        <line x1="0" y1="0" x2="0" y2="10" class="radar-hatch" />
      </pattern>
    </defs>
    <g aria-hidden="true">
      {grid}
      {axes}
      {band}
      {baseline_mark}
      <polygon points="{shape_points}" class="radar-shape"
               fill="url(#{esc(safe_id)}-hatch)" />
      {point_marks}
      {''.join(labels)}
    </g>
  </svg>
  <table class="radar-score-table">
    <caption class="visually-hidden">{esc(title)}的逐维分数</caption>
    <thead><tr><th scope="col">维度</th><th scope="col">分数</th>{'<th scope="col">分歧范围</th>' if ranges else ''}</tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <figcaption>{esc(title)}</figcaption>
</figure>"""


def _judge_evaluation(
    label: str,
    judge: dict[str, Any],
    chart_id: str,
) -> str:
    dimensions = judge["dimensions"]
    scores = {
        spec.key: (dimensions.get(spec.key) or {}).get("score")
        for spec in DIMENSION_SPECS
    }
    comments = []
    for spec in DIMENSION_SPECS:
        entry = dimensions.get(spec.key) or {}
        comment = entry.get("comment") or "尚未提交该维度评价。"
        comments.append(f"""<li class="dimension-comment">
  <header><h4 title="{esc(_dimension_label(spec))}">{esc(_dimension_short(spec))}</h4><strong>{_format_score(entry.get('score'))}</strong></header>
  <p>{esc(comment)}</p>
</li>""")
    return f"""<details class="judge-drawer">
  <summary>{esc(label)}</summary>
  <div class="judge-evaluation">
    <div class="judge-evaluation-grid">
      {_radar_chart(chart_id, f'{label}逐维评分', scores, series_kind="historical-median")}
      <ol class="dimension-comments" aria-label="{esc(label)}逐维评价">{''.join(comments)}</ol>
    </div>
  </div>
</details>"""


def _json_drawer(title: str, value: dict[str, Any]) -> str:
    if value:
        payload = esc(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        payload = "尚未生成。"
    return f"""<details class="outline-drawer">
  <summary>{esc(title)}</summary>
  <pre><code>{payload}</code></pre>
</details>"""


def _v4_subscore_table(v4: dict[str, Any]) -> str:
    rows: list[str] = []
    for spec in V4_DIMENSION_SPECS:
        dimension = v4["dimensions"][spec.key]
        for subkey, values in dimension["subscores"].items():
            rows.append(
                f"<tr><th scope=\"row\">{esc(_dimension_short(spec))} · {esc(subkey)}</th>"
                f"<td>{_format_score(values['median'])}</td>"
                f"<td>{_format_score(values['min'])}–{_format_score(values['max'])}</td></tr>"
            )
    return f"""<div class="table-shell v4-subscore-shell"><table class="v4-subscore-table">
  <caption>24 个 V4 子项：中位数与评委分歧范围</caption>
  <thead><tr><th scope="col">子项</th><th scope="col">中位数</th><th scope="col">分歧范围</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table></div>"""


def _v4_judge_evaluations(v4: dict[str, Any]) -> str:
    drawers: list[str] = []
    for judge_id, judge in v4["judges"].items():
        items: list[str] = []
        for spec in V4_DIMENSION_SPECS:
            dimension = judge["dimensions"][spec.key]
            subscore_text = " · ".join(
                f"{esc(key)} {_format_score(value)}" for key, value in dimension["subscores"].items()
            )
            items.append(f"""<li class="v4-evidence-item">
  <header><h4>{esc(_dimension_short(spec))}</h4><strong>{_format_score(dimension['score'])}</strong></header>
  <p><b>子项：</b>{subscore_text}</p>
  <p><b>证据：</b>{esc(json.dumps(dimension['evidence'], ensure_ascii=False))}</p>
  <p><b>主要缺陷：</b>{esc(json.dumps(dimension['major_defect'], ensure_ascii=False))}</p>
  <p><b>置信度：</b>{_format_score(dimension['confidence'])}</p>
</li>""")
        label = _judge_label(judge_id)
        drawers.append(f"""<details class="judge-drawer v4-judge-drawer">
  <summary>{esc(label)} · V4 证据记录</summary>
  <ol class="v4-evidence-list" aria-label="{esc(label)} V4 逐维证据">{''.join(items)}</ol>
</details>""")
    return '<div class="judge-evaluations" aria-label="V4 评委证据">' + "".join(drawers) + "</div>"


def _v4_result_section(result: dict[str, Any]) -> str:
    v4 = result.get("v4") or _blank_v4()
    if not v4["valid"]:
        return """<section id="score-v4" class="aggregate-section v4-result-section">
  <h2>V4 评分</h2><p class="empty-copy">该作品的 V4 聚合尚未通过完整性与时效校验，因此不会展示或参与排名。</p>
</section>"""
    absolute_scores = {spec.key: v4["dimensions"][spec.key]["median"] for spec in V4_DIMENSION_SPECS}
    absolute_ranges = {spec.key: v4["dimensions"][spec.key] for spec in V4_DIMENSION_SPECS}
    percentile_scores = {spec.key: v4["percentiles"].get(spec.key) for spec in V4_DIMENSION_SPECS}
    chart_prefix = f"v4-radar-{result['model_id']}"
    ranking = ""
    if v4.get("rank"):
        ci = v4.get("ci95")
        overlap = " · 无法可靠区分" if v4.get("ci_overlaps_next") else ""
        ranking = f"<p class=\"v4-result-meta\">第 {v4['rank']} 名 · 相对分 {_format_score(v4['relative_score'])} · 胜率 {_format_probability(v4['win_probability'])} · 95% CI {ci[0]:.1f}–{ci[1]:.1f}{overlap}</p>"
    audit = _json_drawer("大纲审计", {"outline_audit": v4["outline_audit"]}) if v4.get("outline_audit") is not None else ""
    return f"""<section id="score-v4" class="aggregate-section v4-result-section" aria-labelledby="v4-score-title">
  <h2 id="v4-score-title">V4 评分</h2>{ranking}
  {_radar_chart(f'{chart_prefix}-absolute', '绝对分与评委分歧范围', absolute_scores, series_kind="historical-median", ranges=absolute_ranges, specs=V4_DIMENSION_SPECS)}
  {_radar_chart(f'{chart_prefix}-percentile', '维度百分位（队列中位概念为 50）', percentile_scores, series_kind="percentile", already_oriented=True, specs=V4_DIMENSION_SPECS, baseline=50)}
  <p class="v4-legend">绝对分雷达的阴影外沿和内沿分别表示该维度的最高、最低有效评委分；百分位雷达已按方向统一，50 表示队列中位。</p>
  <h3>24 个子项</h3>{_v4_subscore_table(v4)}
  <h3>评委证据、主要缺陷与置信度</h3>{_v4_judge_evaluations(v4)}
  {audit}
</section>"""


def _archive_history_section(result: dict[str, Any]) -> str:
    archives = result.get("archives") or []
    if not archives:
        return ""
    items = "".join(
        f"""<li><a href="archive/{esc(result['model_id'])}/{esc(item['archive_id'])}.html">
  <span>《{esc(item['title'])}》</span>
  <small>成稿 {esc(item['manuscript_date'])} · {item['chapters']} 章 · {item['body_chars']:,} 字 · 已归档，不参与排名</small>
</a></li>"""
        for item in archives
    )
    return f"""<section class="archive-history" aria-labelledby="archive-history-title">
  <h2 id="archive-history-title">历史成稿</h2>
  <p>新稿发布后，旧稿和属于它的旧评审会整体归档；归档稿仅供查阅，不参与当前排名。</p>
  <ol>{items}</ol>
</section>"""


def _rank_strip(result: dict[str, Any], *, archived: bool) -> str:
    reagg = result.get("reagg") or {}
    if archived or reagg.get("status") != REAGG_COMPLETE:
        return """<nav class="rank-strip" aria-label="阅读入口">
  <a href="#novel-title">阅读正文</a>
  <a href="#scores">查看评分</a>
</nav>"""
    strong = DIMENSION_SHORT_LABELS.get(reagg["strongest"], reagg["strongest"])
    weak = DIMENSION_SHORT_LABELS.get(reagg["weakest"], reagg["weakest"])
    tie = " · =" if reagg.get("ties_with_next") else ""
    return f"""<nav class="rank-strip" aria-label="名次与剖面入口">
  <span class="protocol-chip">V3 重聚合 · 非新评</span>
  <span>第 {reagg['rank']} 名{tie} · 相对 {reagg['n']} 本</span>
  <span>相对强：{esc(strong)} · 相对弱：{esc(weak)}</span>
  <a href="#novel-title">阅读正文</a>
  <a href="#scores">查看评分</a>
</nav>"""


def _default_profile_radar(result: dict[str, Any], chart_prefix: str) -> str:
    reagg = result.get("reagg") or {}
    if reagg.get("status") != REAGG_COMPLETE:
        return (
            '<p class="empty-copy" role="status">暂无重聚合剖面。'
            "这本书不在当前可排名队列里，或不满足 N≥2。</p>"
        )
    residual_scores = {
        key: reagg["residual_p"].get(key) for key in DIMENSION_KEYS
    }
    percentile_scores = {
        key: reagg["percentiles"].get(key) for key in DIMENSION_KEYS
    }
    return (
        _radar_chart(
            f"{chart_prefix}-residual",
            "相对本书均值的残差（50 = 本书八维均值，不是满分）",
            residual_scores,
            series_kind="residual-p",
            already_oriented=True,
            baseline=50,
        )
        + _radar_chart(
            f"{chart_prefix}-percentile",
            "队列百分位，不是绝对分（50 = 队列中位）",
            percentile_scores,
            series_kind="percentile",
            already_oriented=True,
            baseline=50,
        )
    )


def render_result_detail(
    result: dict[str, Any],
    *,
    public_protocol: str = "v3-reagg",
) -> str:
    judges = result["judges"]
    judge_ids = tuple(result.get("judge_ids") or JUDGE_IDS)
    body_chars = result["body_chars"]
    novel_html = result["novel_html"] or '<p class="empty-copy">正文尚未归档。</p>'
    aggregate_dimensions = result["aggregate_dimensions"]
    aggregate_scores = {
        spec.key: (aggregate_dimensions.get(spec.key) or {}).get("median")
        for spec in DIMENSION_SPECS
    }
    chart_prefix = f"radar-{result['model_id']}"
    archived = bool(result.get("archived"))
    if archived:
        root_prefix = "../../../../"
        back_link = f'../../{esc(result["model_id"])}.html'
        back_text = "← 当前稿"
        archive_notice = """<p class="archive-notice" role="note">这是历史成稿及其原评审快照，已退出当前排名。</p>"""
        history = ""
        scoring_note = (
            f"该历史稿按归档时的 {len(judge_ids)} 位评委票取中位数"
            f"（{'、'.join(_judge_label(judge_id) for judge_id in judge_ids)}）；"
            "这些分数仅供查阅，不参与当前排名。"
        )
    else:
        root_prefix = "../../"
        back_link = "../../index.html"
        back_text = "← 榜单"
        archive_notice = ""
        history = _archive_history_section(result)
        scoring_note = SCORING_NOTE
    show_v4 = public_protocol == "v4"
    history_radar = ""
    if any(value is not None for value in aggregate_scores.values()):
        history_radar = (
            '<details class="history-radar"><summary>V3 历史中位数雷达</summary>'
            + _radar_chart(
                f"{chart_prefix}-median",
                "活动评委维度中位数",
                aggregate_scores,
                series_kind="historical-median",
            )
            + "</details>"
        )
    return page_head(
        f"{result['title']} · {result['model_name']}",
        root_prefix,
        "page-result page-archived-result" if archived else "page-result",
        skip_href="#novel-title",
    ) + f"""
<a class="back-link" href="{back_link}">{back_text}</a>
<article class="result-file">
  <header class="result-header">
    <h1>《{esc(result['title'])}》</h1>
    <p class="result-meta">{esc(result['model_name'])} · 成稿 {esc(result['manuscript_date'])} · {result['chapters']} 章 · {body_chars:,} 字</p>
    {archive_notice}
    <p class="result-blurb">{esc(result['blurb'])}</p>
    {_rank_strip(result, archived=archived)}
  </header>

  {history}

  <section class="reading-section" aria-labelledby="novel-title">
    <h2 id="novel-title">正文</h2>
    <div class="novel-body markdown">{novel_html}</div>
  </section>

  <section class="outline-section" aria-labelledby="outline-title">
    <h2 id="outline-title" class="visually-hidden">大纲</h2>
    {_json_drawer('全纲', result['macro_outline'])}
    {_json_drawer('细纲', result['opening_outline'])}
  </section>

  <section id="scores" class="aggregate-section" aria-labelledby="aggregate-title">
    <h2 id="aggregate-title">评分剖面</h2>
    {_default_profile_radar(result, chart_prefix)}
    <details class="info-drawer">
      <summary>评分怎么算</summary>
      <p>{scoring_note} 残差雷达里 AI 味已定向，50 是本书均值；头部书共同凹在自然度是 V3 数据里的真信号。</p>
    </details>
    {history_radar}
    <div class="judge-evaluations" aria-label="活动评委逐维记录">
      {''.join(
          _judge_evaluation(
              _judge_label(judge_id),
              judges[judge_id],
              f'{chart_prefix}-{judge_id}',
          )
          for judge_id in judge_ids
      )}
    </div>
    {_v4_result_section(result) if show_v4 else ''}
  </section>
</article>
""" + PAGE_FOOT


def render_history_index() -> str:
    return page_head("V2.1 历史", "../", "page-legacy") + """
<a class="back-link" href="../index.html">← 榜单</a>
<header class="page-intro">
  <h1>V2.1 已冻结</h1>
  <p class="page-sub">改革开放长篇仍可在榜单阅读，但不再当作文风评测的当前协议。</p>
</header>
<p>作者群指出：每个模型自己起世界、人物和大纲时，测到的不是文风。新协议先锁世界，再锁人物，再锁章纲，最后冻成同一份开局提示词写 5–10 章。现行题目只有一句：筑基修士翻过十万大山看见高楼。说明见仓库 <code>docs/opening-protocol.md</code>。</p>
<p>旧稿不删除。公开结果仍在 <code>results/reform-era/</code>，从<a href="../index.html">榜单</a>进入正文。</p>
""" + PAGE_FOOT


def render_legacy_index(stories: list[dict[str, Any]]) -> str:
    cards = []
    for story in stories:
        cards.append(f"""<a class="legacy-card" href="{story['slug']}/index.html">
  <h2>{esc(story['title'])}</h2>
  <p class="card-meta">{esc(story['genre'])} · {len(story['versions'])} 个版本</p>
  <p>{esc(story['intro'])}</p>
</a>""")
    cards_html = "\n".join(cards) or '<p class="empty-copy">没有可展示的旧题材。</p>'
    return page_head("Legacy", "../", "page-legacy") + f"""
<a class="back-link" href="../index.html">← 榜单</a>
<header class="page-intro">
  <h1>Legacy</h1>
</header>
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
  <span>{version['chars']:,} 字 · {version['chapters']} 章{suffix}</span>
</a>""")
    return page_head(story["title"], "../../", "page-legacy-story") + f"""
<a class="back-link" href="../index.html">← Legacy</a>
<header class="page-intro">
  <h1>{esc(story['title'])}</h1>
</header>
<details class="prompt-drawer"><summary>统一提示词</summary><div class="markdown">{story['prompt_html']}</div></details>
<section class="version-grid">{''.join(cards)}</section>
""" + PAGE_FOOT


def render_legacy_novel(story: dict[str, Any], version: dict[str, Any]) -> str:
    partial = '<p class="partial-notice">未完成十章，按原样归档。</p>' if version["partial"] else ""
    return page_head(
        f"{version['title']} · {version['model_name']}", "../../", "page-reading"
    ) + f"""
<a class="back-link" href="index.html">← {esc(story['title'])}</a>
<article class="legacy-novel">
  <header class="novel-header">
    <h1>{esc(version['title'])}</h1>
    <p class="result-meta">{esc(version['model_name'])} · {version['chars']:,} 字 · {version['chapters']} 章</p>{partial}
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
    public_protocol: str | None = None,
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
    archived_results = load_archived_reform_results(results_dir, model_by_id)
    archives_by_model: dict[str, list[dict[str, Any]]] = {}
    for archived in archived_results:
        archives_by_model.setdefault(archived["model_id"], []).append(archived)
    for result in results:
        result["archives"] = sorted(
            archives_by_model.get(result["model_id"], []),
            key=lambda item: item["manuscript_completed_at"],
            reverse=True,
        )
    attach_v4_results(results, results_dir)
    attach_reagg_v3(results, benchmark=results_dir.name, results_dir=results_dir)
    protocol = resolve_public_protocol(
        cli=public_protocol, output_dir_name=output_dir.name
    )
    if protocol == "v3-reagg":
        print(
            f"[reagg-v3] n={sum(1 for item in results if (item.get('reagg') or {}).get('status') == REAGG_COMPLETE)} "
            "protocol=v3-reagg",
            file=sys.stderr,
        )
    v4_preview = protocol == "v4"
    legacy_stories = load_legacy_stories(novels_dir, model_by_id, model_order)

    (output_dir / "index.html").write_text(
        render_home(
            results,
            len(legacy_stories),
            public_protocol=protocol,
            v4_preview=v4_preview,
        ),
        encoding="utf-8",
    )
    history_dir = output_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    (history_dir / "index.html").write_text(render_history_index(), encoding="utf-8")
    result_output = output_dir / "results" / "reform-era"
    result_output.mkdir(parents=True, exist_ok=True)
    for result in results:
        if not result["detail_available"]:
            continue
        (result_output / f"{result['model_id']}.html").write_text(
            render_result_detail(result, public_protocol=protocol), encoding="utf-8"
        )
    archive_output = result_output / ARCHIVE_DIR_NAME
    for archived in archived_results:
        version_output = archive_output / archived["model_id"]
        version_output.mkdir(parents=True, exist_ok=True)
        (version_output / f"{archived['archive_id']}.html").write_text(
            render_result_detail(archived, public_protocol=protocol), encoding="utf-8"
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
    parser.add_argument(
        "--public-protocol",
        choices=PUBLIC_PROTOCOLS,
        default=None,
        help="公开口径。默认 v3-reagg；v4 仅预览。",
    )
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
            public_protocol=args.public_protocol,
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
