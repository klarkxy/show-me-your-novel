#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V4 blinded pairwise ranking for completed V4 score aggregates.

The module deliberately keeps its public records compact: source text and raw
model responses never leave the local request.  A complete, content-addressed
pair record is a valid cache, so an all-cached invocation does not load
credentials, preflight models, or make a network request.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from . import score as _score
    from . import llm_api as _llm_api
    from .generate import WorkDirLock
    try:
        from . import score_v4 as _score_v4  # optional during parallel rollout
    except ImportError:
        _score_v4 = None
except ImportError:  # direct `python runner/compare_v4.py`
    import score as _score  # type: ignore
    import llm_api as _llm_api  # type: ignore
    from generate import WorkDirLock  # type: ignore
    try:
        import score_v4 as _score_v4  # type: ignore
    except ImportError:
        _score_v4 = None


PAIRWISE_SCHEMA_VERSION = "novel-pairwise.v4"
RANKING_SCHEMA_VERSION = "novel-ranking.v4"
DEFAULT_BENCHMARK = getattr(_score_v4, "DEFAULT_BENCHMARK", getattr(_score, "DEFAULT_BENCHMARK", "reform-era"))
JUDGE_IDS = ("sol", "grok", "opus", "k3", "ds-v4-pro")
EXPECTED_JUDGE_MODELS = {
    "sol": "gpt-5.6-sol",
    "grok": "grok-4.6",
    "opus": "claude-opus-5",
    "k3": "kimi-k3",
    "ds-v4-pro": "deepseek-v4-pro",
}
DIMENSION_SPECS = tuple(getattr(_score_v4, "DIMENSION_SPECS", ()))
DIMENSION_KEYS = tuple(spec.key for spec in DIMENSION_SPECS) or (
    "theme_fulfillment", "historical_grounding", "characters", "plot_causality",
    "longform_structure", "scene_execution", "style_control", "naturalness",
)
DIMENSION_WEIGHTS = {spec.key: float(spec.weight) for spec in DIMENSION_SPECS}
if not DIMENSION_WEIGHTS:
    DIMENSION_WEIGHTS = {key: 1 / len(DIMENSION_KEYS) for key in DIMENSION_KEYS}
# Enough room for all eight concise evidence entries.  The override contains no
# sampling control. A judge-specific config can add supported controls, but it
# cannot lower this evidence budget accidentally.
PAIRWISE_REQUEST_OVERRIDES = {"max_tokens": 16_384}
CONTEXT_SAFETY_FRACTION = 0.85
PAIRWISE_STAGE = "judge"
PILOT_CANDIDATES = ("gpt-5.6-sol", "grok-4.6", "gemini-3.1-pro", "minimax-m3")
ALL_CANDIDATES = (
    "deepseek-v4-flash", "deepseek-v4-pro", "gemini-3.1-pro",
    "gemini-3.5-flash", "gemini-3.6-flash", "gpt-5.6-luna",
    "gpt-5.6-sol", "gpt-5.6-terra", "grok-4.6", "kimi-k2.7-code",
    "kimi-k3", "mimo-v2.5", "mimo-v2.5-pro", "minimax-m3",
)
BOOTSTRAP_SAMPLES = 400


def _pairwise_json_schema() -> dict[str, Any]:
    """Anthropic structured-output contract for one blinded pairwise vote."""

    entry = {
        "type": "object",
        "properties": {
            "winner": {"type": "string", "enum": ["A", "B", "tie"]},
            "margin": {"type": "integer", "minimum": 0, "maximum": 3},
            "evidence": {"type": "string", "minLength": 1, "maxLength": 400},
        },
        "required": ["winner", "margin", "evidence"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "dimensions": {
                "type": "object",
                "properties": {key: entry for key in DIMENSION_KEYS},
                "required": list(DIMENSION_KEYS),
                "additionalProperties": False,
            }
        },
        "required": ["dimensions"],
        "additionalProperties": False,
    }


class CompareError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    name: str
    aggregate: Mapping[str, Any]
    aggregate_hash: str
    score: float
    direction: str
    direction_hash: str
    content: str
    content_hash: str


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise CompareError(f"缺少输入：{path}") from exc
    except json.JSONDecodeError as exc:
        raise CompareError(f"不是有效 JSON：{path}") from exc
    if not isinstance(value, Mapping):
        raise CompareError(f"JSON 顶层必须是对象：{path}")
    return value


def _load_json_if_present(path: Path) -> Mapping[str, Any] | None:
    """A stale or interrupted cache is a miss, never a reason to trust it."""
    try:
        return _read_json(path)
    except (CompareError, OSError):
        return None


def _read_text(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").strip()
    except FileNotFoundError as exc:
        raise CompareError(f"缺少输入：{path}") from exc
    if not text:
        raise CompareError(f"输入为空：{path}")
    return text


def _aggregate_path(candidate_dir: Path) -> Path | None:
    path = candidate_dir / "scores-v4" / "aggregate.json"
    return path if path.is_file() else None


def _aggregate_complete(value: Mapping[str, Any]) -> bool:
    expected_schema = getattr(_score_v4, "AGGREGATE_SCHEMA_VERSION", None)
    return (
        (expected_schema is None or value.get("schema") == expected_schema)
        and value.get("status") == "complete"
        and value.get("eligible_for_ranking") is True
    )


def _candidate_input(root: Path, benchmark: str, name: str) -> tuple[str, str]:
    # V4's public loader verifies the completed manifest and every chapter hash.
    # Its content is already anonymous and includes every full chapter.
    if _score_v4 is None or not hasattr(_score_v4, "load_submission"):
        raise CompareError("缺少 score_v4 的已验证章节加载器；拒绝使用非规范比较输入")
    try:
        submission = _score_v4.load_submission(root, benchmark, name)
        # Direction is shared once by build_messages; per-candidate payloads
        # contain only full manifest-verified chapters, never outlines/books.
        chapters = "\n\n".join(
            f'<chapter id="{chapter}">\n{text}\n</chapter>'
            for chapter, text in submission.chapters.items()
        )
        return str(submission.direction), chapters
    except Exception as exc:
        raise CompareError(f"V4 候选文本未通过完成产物校验：{name}: {exc}") from exc


def _aggregate_is_current(value: Mapping[str, Any], submission: Any, root: Path) -> bool:
    if not _aggregate_complete(value):
        return False
    if value.get("benchmark") != submission.benchmark or value.get("candidate") != submission.candidate or value.get("input_hash") != submission.input_hash:
        return False
    required_top = {"schema", "benchmark", "candidate", "input_hash", "provenance", "expected_judges", "completed_judges", "status", "eligible_for_ranking", "judges", "dimensions", "overall_score"}
    if set(value) != required_top or value.get("expected_judges") != list(JUDGE_IDS) or value.get("completed_judges") != list(JUDGE_IDS):
        return False
    try:
        expected_provenance = _score_v4.expected_aggregate_provenance(root, submission)
    except Exception:
        return False
    if value.get("provenance") != expected_provenance:
        return False
    judges = value.get("judges")
    if not isinstance(judges, Mapping) or set(judges) != set(JUDGE_IDS):
        return False
    try:
        judge_dimensions = {
            judge: judges[judge]["dimensions"]
            for judge in JUDGE_IDS
            if isinstance(judges[judge], Mapping)
        }
        if set(judge_dimensions) != set(JUDGE_IDS):
            return False
        # Re-validate every embedded public dimension against the current,
        # manifest-verified chapters before trusting its aggregate projection.
        for dimensions in judge_dimensions.values():
            response_dimensions = {
                key: {field: child for field, child in entry.items() if field != "score"}
                for key, entry in dimensions.items()
                if isinstance(entry, Mapping)
            }
            parsed = _score_v4.parse_score_response(
                json.dumps({"dimensions": response_dimensions}, ensure_ascii=False),
                submission.chapters,
            )
            if dimensions != parsed["dimensions"]:
                return False
        recomputed_dimensions = _score_v4.aggregate_dimension_scores(judge_dimensions)
        recomputed_overall = _score_v4.overall_score_from_medians(recomputed_dimensions)
    except Exception:
        return False
    if value.get("dimensions") != recomputed_dimensions or value.get("overall_score") != recomputed_overall:
        return False
    dimensions = value.get("dimensions")
    if not isinstance(dimensions, Mapping) or set(dimensions) != set(DIMENSION_KEYS):
        return False
    for spec in DIMENSION_SPECS:
        entry = dimensions.get(spec.key)
        if not isinstance(entry, Mapping) or set(entry) != {"label", "weight", "median", "min", "max", "subscores"}:
            return False
        if entry.get("label") != spec.label or entry.get("weight") != spec.weight:
            return False
        if any(isinstance(entry.get(name), bool) or not isinstance(entry.get(name), (int, float)) for name in ("median", "min", "max")):
            return False
        subscores = entry.get("subscores")
        if not isinstance(subscores, Mapping) or set(subscores) != set(spec.subscores):
            return False
        for subscore in subscores.values():
            if not isinstance(subscore, Mapping) or set(subscore) != {"median", "min", "max"}:
                return False
    return isinstance(value.get("overall_score"), (int, float)) and not isinstance(value.get("overall_score"), bool)


def load_completed_candidates(root: Path, benchmark: str = DEFAULT_BENCHMARK, *, allowed: Iterable[str] | None = None) -> list[Candidate]:
    results_dir = root / "results" / benchmark
    if not results_dir.is_dir():
        raise CompareError(f"结果目录不存在：{results_dir}")
    allowed_names = set(allowed) if allowed is not None else None
    candidates: list[Candidate] = []
    for candidate_dir in sorted(results_dir.iterdir(), key=lambda p: p.name):
        if not candidate_dir.is_dir() or candidate_dir.name.startswith("_"):
            continue
        if allowed_names is not None and candidate_dir.name not in allowed_names:
            continue
        aggregate_path = _aggregate_path(candidate_dir)
        if aggregate_path is None:
            continue
        aggregate = _read_json(aggregate_path)
        if _score_v4 is None or not hasattr(_score_v4, "load_submission"):
            raise CompareError("缺少 score_v4 的已验证章节加载器；拒绝加载 aggregate")
        try:
            submission = _score_v4.load_submission(root, benchmark, candidate_dir.name)
        except Exception as exc:
            raise CompareError(f"V4 候选文本未通过完成产物校验：{candidate_dir.name}: {exc}") from exc
        if not _aggregate_is_current(aggregate, submission, root):
            continue
        score = aggregate.get("overall_score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            continue
        direction = str(submission.direction)
        content = "\n\n".join(f'<chapter id="{chapter}">\n{text}\n</chapter>' for chapter, text in submission.chapters.items())
        candidates.append(Candidate(candidate_dir.name, aggregate, _sha256(_canonical_json(aggregate)), float(score), direction, _sha256(direction), content, _sha256(content)))
    return candidates


def select_edges(candidates: Iterable[Candidate]) -> list[tuple[str, str]]:
    """Adjacent and distance-two edges in score order, deduplicated deterministically."""
    ordered = sorted(candidates, key=lambda c: (-c.score, c.name))
    seen: set[tuple[str, str]] = set()
    edges: list[tuple[str, str]] = []
    for distance in (1, 2):
        for index in range(len(ordered) - distance):
            edge = tuple(sorted((ordered[index].name, ordered[index + distance].name)))
            if edge not in seen:
                seen.add(edge)
                edges.append(edge)
    return edges


def edge_id(left: str, right: str) -> str:
    return _sha256(f"{left}\0{right}")[:20]


def build_order_plan(edges: Iterable[tuple[str, str]]) -> dict[tuple[str, str], dict[str, tuple[str, str]]]:
    """Deterministically alternate labels over the *whole* edge set.

    Each judge has an A/B count difference of at most one.  Hash-sorted edges
    and judge-specific phase offsets avoid a fixed first-edge/fixed-judge bias.
    """
    ordered = sorted((tuple(sorted(edge)) for edge in edges), key=lambda edge: _sha256(f"{edge[0]}\0{edge[1]}\0global-order-v4"))
    plan = {edge: {} for edge in ordered}
    for judge in JUDGE_IDS:
        phase = int(_sha256(f"{judge}\0global-order-v4")[:2], 16) % 2
        for index, edge in enumerate(ordered):
            left_as_a = (index + phase) % 2 == 0
            plan[edge][judge] = edge if left_as_a else (edge[1], edge[0])
    return plan


def balanced_display_orders(left: str, right: str) -> dict[str, tuple[str, str]]:
    """Compatibility helper: the one-edge projection of the global plan."""
    edge = tuple(sorted((left, right)))
    return build_order_plan([edge])[edge]


def load_system_prompt(root: Path) -> str:
    template = _read_text(root / "runner" / "prompts" / "v4" / "pairwise_system.md")
    marker = "{{DIMENSION_SPECS}}"
    if template.count(marker) != 1:
        raise CompareError(f"提示词必须且只能包含一个 {marker}")
    lines = []
    for spec in DIMENSION_SPECS:
        direction = "越高越好" if getattr(spec, "higher_is_better", True) else "AI 味越低越好"
        lines.append(f"- `{spec.key}`：{spec.label}（{direction}，权重 {spec.weight:.0%}）")
    skeleton = {"dimensions": {key: {"winner": "A", "margin": 1, "evidence": "具体证据"} for key in DIMENSION_KEYS}}
    return template.replace(marker, "\n".join(lines) + "\n\nJSON 结构：\n" + _canonical_json(skeleton))


def build_messages(system_prompt: str, left: Candidate, right: Candidate, order: tuple[str, str]) -> list[dict[str, str]]:
    by_name = {left.name: left, right.name: right}
    a, b = (by_name[order[0]], by_name[order[1]])
    def labelled(label: str, candidate: Candidate) -> str:
        # Candidate content has no identifier; labels are introduced only here.
        return candidate.content.replace("<chapter ", f"<{label}_chapter ").replace("</chapter>", f"</{label}_chapter>")
    if left.direction != right.direction:
        raise CompareError("候选题材方向不一致；拒绝比较")
    content = "以下是两份匿名投稿。A 与 B 标签仅是本次显示顺序。\n\n" + f"<direction>\n{left.direction}\n</direction>\n\n<submission_A>\n{labelled('A', a)}\n</submission_A>\n\n<submission_B>\n{labelled('B', b)}\n</submission_B>"
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": content}]


def _estimate_tokens(text: str) -> int:
    """Conservative deterministic estimate for preflight, never a truncator."""
    cjk = sum(1 for char in text if "\u3400" <= char <= "\u9fff")
    other = len(text) - cjk
    return cjk + math.ceil(other / 4)


def validate_context_budget(
    messages: Iterable[Mapping[str, str]],
    model_cfg: Mapping[str, Any],
    request_overrides: Mapping[str, Any],
) -> None:
    """Fail closed if the complete request plus response cannot fit a judge."""
    context_window = model_cfg.get("context_window")
    if isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0:
        raise CompareError("评委缺少有效 context_window；未截断、未发送请求")
    required = _llm_api.protocol_required_parameters(model_cfg)
    output_budget = request_overrides.get("max_tokens", required.get("max_tokens"))
    if isinstance(output_budget, bool) or not isinstance(output_budget, int) or output_budget <= 0:
        raise CompareError("评委缺少有效 pairwise max_tokens；未截断、未发送请求")
    input_tokens = sum(_estimate_tokens(str(message.get("content", ""))) + 8 for message in messages)
    ceiling = math.floor(context_window * CONTEXT_SAFETY_FRACTION)
    required = input_tokens + output_budget
    if required > ceiling:
        raise CompareError(
            f"评委上下文超出 85% 安全上限（输入估算 {input_tokens} + 输出 {output_budget} > {ceiling}）；未截断、未发送请求"
        )


def context_budget_details(
    messages: Iterable[Mapping[str, str]],
    model_cfg: Mapping[str, Any],
    request_overrides: Mapping[str, Any],
) -> dict[str, int]:
    """Expose deterministic dry-run evidence for the exact complete payload."""
    rendered = list(messages)
    context_window = model_cfg.get("context_window")
    required = _llm_api.protocol_required_parameters(model_cfg)
    output_budget = request_overrides.get("max_tokens", required.get("max_tokens"))
    if isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0:
        raise CompareError("评委缺少有效 context_window；未截断、未发送请求")
    if isinstance(output_budget, bool) or not isinstance(output_budget, int) or output_budget <= 0:
        raise CompareError("评委缺少有效 pairwise max_tokens；未截断、未发送请求")
    return {
        "characters": sum(len(str(message.get("content", ""))) for message in rendered),
        "input_tokens": sum(_estimate_tokens(str(message.get("content", ""))) + 8 for message in rendered),
        "output_tokens": output_budget,
        "safety_ceiling": math.floor(context_window * CONTEXT_SAFETY_FRACTION),
    }


def validate_edge_context(
    prompt: str,
    left: Candidate,
    right: Candidate,
    judge_configs: Mapping[str, Mapping[str, Any]],
    orders: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, dict[str, int]]:
    """Validate each assigned judge order before any API client is constructed."""
    details: dict[str, dict[str, int]] = {}
    for judge, order in (orders or balanced_display_orders(left.name, right.name)).items():
        request_overrides = effective_request_overrides(
            judge, judge_configs[judge].get("request_overrides")
        )
        messages = build_messages(prompt, left, right, order)
        detail = context_budget_details(messages, judge_configs[judge]["model_cfg"], request_overrides)
        validate_context_budget(messages, judge_configs[judge]["model_cfg"], request_overrides)
        details[judge] = detail
    return details


def parse_pairwise_response(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S | re.I)
    if fenced:
        text = fenced.group(1)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CompareError(f"评委响应 JSON 解析失败：{exc}") from exc
    if not isinstance(parsed, Mapping) or set(parsed) != {"dimensions"}:
        raise CompareError("评委响应顶层必须且只能包含 dimensions")
    dimensions = parsed["dimensions"]
    if not isinstance(dimensions, Mapping) or set(dimensions) != set(DIMENSION_KEYS):
        raise CompareError("dimensions 必须完整且只能包含八个标准维度")
    normal: dict[str, Any] = {}
    for key in DIMENSION_KEYS:
        entry = dimensions[key]
        if not isinstance(entry, Mapping) or set(entry) != {"winner", "margin", "evidence"}:
            raise CompareError(f"dimensions.{key} 必须且只能包含 winner、margin、evidence")
        winner, margin, evidence = entry["winner"], entry["margin"], entry["evidence"]
        if winner not in {"A", "B", "tie"} or isinstance(margin, bool) or not isinstance(margin, int) or not 0 <= margin <= 3:
            raise CompareError(f"dimensions.{key} 的 winner 或 margin 无效")
        if (winner == "tie") != (margin == 0):
            raise CompareError(f"dimensions.{key} 的 tie 必须配 margin 0，非 tie 必须配 1–3")
        if not isinstance(evidence, str) or not (evidence := re.sub(r"\s+", " ", evidence).strip()) or len(evidence) > 400:
            raise CompareError(f"dimensions.{key}.evidence 无效")
        normal[key] = {"winner": winner, "margin": margin, "evidence": evidence}
    return {"dimensions": normal}


def _safe_model_config(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k): v for k, v in value.items() if str(k).lower() not in {"api_key", "api_key_env", "base_url", "token", "secret"}}


def effective_request_overrides(
    judge_id: str,
    configured: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the exact public request contract used by a pairwise vote."""
    result = dict(configured or {})
    # Pairwise JSON needs room for eight evidence strings. Anthropic Opus owns
    # this value as protocol_required.max_tokens, so it must not be duplicated.
    if judge_id != "opus":
        result["max_tokens"] = PAIRWISE_REQUEST_OVERRIDES["max_tokens"]
    else:
        tool_name = "submit_v4_pairwise_vote"
        result["tools"] = [
            {
                "name": tool_name,
                "description": "Submit the complete blinded pairwise vote.",
                "input_schema": _pairwise_json_schema(),
                "strict": True,
            }
        ]
        result["tool_choice"] = {"type": "tool", "name": tool_name}
    return result


def effective_stage_config(model_cfg: Mapping[str, Any]) -> dict[str, Any]:
    """The inherited transport controls used for a pairwise judge request."""
    stages = model_cfg.get("stages", {})
    if not isinstance(stages, Mapping):
        raise CompareError("评委 stages 配置无效")
    stage = stages.get(PAIRWISE_STAGE, {})
    if not isinstance(stage, Mapping):
        raise CompareError(f"评委 {PAIRWISE_STAGE} stage 配置无效")
    return dict(stage)


def resolve_judge_configs(cfg: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    entries: dict[str, Mapping[str, Any]] = {}
    raw = cfg.get("judges", [])
    if isinstance(raw, list):
        entries = {str(item.get("id")): item for item in raw if isinstance(item, Mapping) and item.get("id")}
    elif isinstance(raw, Mapping):
        entries = {str(k): v for k, v in raw.items() if isinstance(v, Mapping)}
    resolved: dict[str, dict[str, Any]] = {}
    for judge in JUDGE_IDS:
        entry = entries.get(judge)
        if entry is None:
            raise CompareError(f"config.yaml 缺少固定评委配置：{judge}")
        model_cfg = {k: v for k, v in entry.items() if k not in {"request_overrides"}}
        if str(model_cfg.get("model")) != EXPECTED_JUDGE_MODELS[judge]:
            raise CompareError(f"评委 {judge} 必须使用 {EXPECTED_JUDGE_MODELS[judge]}，不允许静默替换")
        try:
            model_cfg = _llm_api.with_provider_request_defaults(dict(cfg), model_cfg)
        except Exception as exc:
            raise CompareError(f"评委 {judge} 的 provider 配置无效：{exc}") from exc
        if "context_window" not in model_cfg:
            model_cfg = {
                **model_cfg,
                "context_window": getattr(_score_v4, "V4_DEFAULT_CONTEXT_WINDOW", 204_800),
            }
        resolved[judge] = {
            "model_cfg": model_cfg,
            "request_overrides": effective_request_overrides(
                judge, entry.get("request_overrides")
            ),
        }
    return resolved


def pair_cache_key(left: Candidate, right: Candidate, prompt: str, judge_configs: Mapping[str, Mapping[str, Any]], orders: Mapping[str, tuple[str, str]] | None = None) -> str:
    orders = orders or balanced_display_orders(left.name, right.name)
    return _sha256(_canonical_json({"schema": PAIRWISE_SCHEMA_VERSION, "left": {"name": left.name, "aggregate": left.aggregate_hash, "content": left.content_hash}, "right": {"name": right.name, "aggregate": right.aggregate_hash, "content": right.content_hash}, "direction_hash": left.direction_hash, "prompt": _sha256(prompt), "judges": {judge: {"model": _safe_model_config(judge_configs[judge]["model_cfg"]), "stage": PAIRWISE_STAGE, "effective_stage_config": effective_stage_config(judge_configs[judge]["model_cfg"]), "effective_request_overrides": effective_request_overrides(judge, judge_configs[judge].get("request_overrides"))} for judge in JUDGE_IDS}, "orders": orders}))


def _vote_value(dimensions: Mapping[str, Any], displayed_a: str, left: str) -> float:
    total = 0.0
    for key in DIMENSION_KEYS:
        entry = dimensions[key]
        sign = 0 if entry["winner"] == "tie" else (1 if (entry["winner"] == "A") == (displayed_a == left) else -1)
        total += DIMENSION_WEIGHTS[key] * sign * (entry["margin"] / 3)
    return total


def summarize_votes(votes: Mapping[str, Mapping[str, Any]], left: str) -> dict[str, Any]:
    judge_values = {judge: _vote_value(vote["dimensions"], str(vote["displayed_a"]), left) for judge, vote in votes.items()}
    wins = sum(value > 0 for value in judge_values.values())
    losses = sum(value < 0 for value in judge_values.values())
    winner = left if wins > losses else ("right" if losses > wins else "tie")
    return {"judge_values": judge_values, "left_judge_wins": wins, "right_judge_wins": losses, "has_majority": max(wins, losses) > len(judge_values) / 2, "weighted_margin": sum(judge_values.values()) / len(judge_values), "winner": winner}


def needs_reverse(summary: Mapping[str, Any]) -> bool:
    return not bool(summary["has_majority"]) or abs(float(summary["weighted_margin"])) < 0.10


def _public_vote(judge: str, displayed_a: str, parsed: Mapping[str, Any], result: Any) -> dict[str, Any]:
    return {"judge": judge, "displayed_a": displayed_a, "displayed_b": None, "requested_model": getattr(result, "requested_model", None), "response_model": getattr(result, "response_model", None), "finish_reason": getattr(result, "finish_reason", None), "dimensions": parsed["dimensions"]}


def _record_valid(record: Any, left: Candidate, right: Candidate, key: str, orders: Mapping[str, tuple[str, str]] | None = None) -> bool:
    if not isinstance(record, Mapping) or record.get("schema") != PAIRWISE_SCHEMA_VERSION or record.get("cache_key") != key:
        return False
    if record.get("left") != left.name or record.get("right") != right.name:
        return False
    initial = record.get("initial_votes")
    if not isinstance(initial, Mapping) or set(initial) != set(JUDGE_IDS):
        return False
    orders = orders or balanced_display_orders(left.name, right.name)
    for judge, vote in initial.items():
        try: parse_pairwise_response(_canonical_json({"dimensions": vote["dimensions"]}))
        except (CompareError, KeyError, TypeError): return False
        if not isinstance(vote, Mapping) or vote.get("judge") != judge or vote.get("displayed_a") != orders[judge][0] or vote.get("displayed_b") != orders[judge][1]:
            return False
        if vote.get("requested_model") != EXPECTED_JUDGE_MODELS[judge] or vote.get("response_model") != EXPECTED_JUDGE_MODELS[judge] or vote.get("finish_reason") != "stop":
            return False
    first = summarize_votes(initial, left.name)
    reverse = record.get("reverse_votes")
    if needs_reverse(first):
        if not isinstance(reverse, Mapping) or set(reverse) != set(JUDGE_IDS):
            return False
        for judge, vote in reverse.items():
            try: parse_pairwise_response(_canonical_json({"dimensions": vote["dimensions"]}))
            except (CompareError, KeyError, TypeError): return False
            if not isinstance(vote, Mapping) or vote.get("judge") != judge or vote.get("displayed_a") != orders[judge][1] or vote.get("displayed_b") != orders[judge][0]:
                return False
            if vote.get("requested_model") != EXPECTED_JUDGE_MODELS[judge] or vote.get("response_model") != EXPECTED_JUDGE_MODELS[judge] or vote.get("finish_reason") != "stop":
                return False
    elif reverse is not None:
        return False
    all_votes = dict(initial)
    if isinstance(reverse, Mapping):
        all_votes.update({f"reverse:{judge}": vote for judge, vote in reverse.items()})
    final = summarize_votes(all_votes, left.name)
    winner = (
        left.name
        if final["weighted_margin"] > 0
        else right.name if final["weighted_margin"] < 0 else "tie"
    )
    expected_decision = {
        **final,
        "winner": winner,
        "reversed": isinstance(reverse, Mapping),
    }
    return record.get("decision") == expected_decision


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _run_vote(client: Any, judge: str, cfg: Mapping[str, Any], prompt: str, left: Candidate, right: Candidate, order: tuple[str, str]) -> dict[str, Any]:
    messages = build_messages(prompt, left, right, order)
    request_overrides = effective_request_overrides(judge, cfg.get("request_overrides"))
    validate_context_budget(messages, cfg["model_cfg"], request_overrides)
    result = client.complete(cfg["model_cfg"], messages, stage=PAIRWISE_STAGE, request_overrides=request_overrides)
    expected_model = EXPECTED_JUDGE_MODELS[judge]
    if getattr(result, "requested_model", None) != expected_model or getattr(result, "response_model", None) != expected_model:
        raise CompareError(f"评委 {judge} 模型身份不匹配；拒绝响应")
    if getattr(result, "finish_reason", None) != "stop":
        raise CompareError(f"评委 {judge} 未以 stop 完成；拒绝响应")
    parsed = parse_pairwise_response(result.content)
    record = _public_vote(judge, order[0], parsed, result)
    record["displayed_b"] = order[1]
    return record


def compare_edge(root: Path, benchmark: str, left: Candidate, right: Candidate, prompt: str, judge_configs: Mapping[str, Mapping[str, Any]], client: Any | None, *, orders: Mapping[str, tuple[str, str]] | None = None, dry_run: bool = False) -> tuple[str, Mapping[str, Any] | None]:
    orders = orders or balanced_display_orders(left.name, right.name)
    key = pair_cache_key(left, right, prompt, judge_configs, orders)
    path = root / "results" / benchmark / "_pairwise-v4" / "pairs" / f"{edge_id(left.name, right.name)}.json"
    validate_edge_context(prompt, left, right, judge_configs, orders)
    cached = _load_json_if_present(path)
    if _record_valid(cached, left, right, key, orders):
        return "cached", cached
    if dry_run:
        return "would-compare", None
    if client is None:
        raise CompareError("缺少评委客户端")
    lock_path = root / "work" / "v4" / benchmark / "pairwise" / edge_id(left.name, right.name) / ".run.lock"
    try:
        with WorkDirLock(lock_path):
            # A second runner may have completed the paid work while this one
            # waited for the lock.  Revalidate its content-addressed record.
            cached = _load_json_if_present(path)
            if _record_valid(cached, left, right, key, orders):
                return "cached", cached
            initial = {judge: _run_vote(client, judge, judge_configs[judge], prompt, left, right, orders[judge]) for judge in JUDGE_IDS}
            first = summarize_votes(initial, left.name)
            reverse: dict[str, Any] | None = None
            if needs_reverse(first):
                reverse = {judge: _run_vote(client, judge, judge_configs[judge], prompt, left, right, (orders[judge][1], orders[judge][0])) for judge in JUDGE_IDS}
            all_votes = dict(initial)
            if reverse:
                all_votes.update({f"reverse:{judge}": vote for judge, vote in reverse.items()})
            final = summarize_votes(all_votes, left.name)
            winner = left.name if final["weighted_margin"] > 0 else (right.name if final["weighted_margin"] < 0 else "tie")
            fresh_by_name = {candidate.name: candidate for candidate in load_completed_candidates(root, benchmark, allowed=(left.name, right.name))}
            for original in (left, right):
                fresh = fresh_by_name.get(original.name)
                if fresh is None or (fresh.aggregate_hash, fresh.direction_hash, fresh.content_hash) != (original.aggregate_hash, original.direction_hash, original.content_hash):
                    raise CompareError("候选或 completed aggregate 在 pairwise 请求期间变化；已丢弃响应，未写入结果")
            record = {"schema": PAIRWISE_SCHEMA_VERSION, "benchmark": benchmark, "edge_id": edge_id(left.name, right.name), "left": left.name, "right": right.name, "cache_key": key, "input": {"left_aggregate_hash": left.aggregate_hash, "right_aggregate_hash": right.aggregate_hash, "left_content_hash": left.content_hash, "right_content_hash": right.content_hash, "prompt_hash": _sha256(prompt)}, "initial_votes": initial, "reverse_votes": reverse, "decision": {**final, "winner": winner, "reversed": reverse is not None}}
            _atomic_write_json(path, record)
            return "compared", record
    except RuntimeError as exc:
        if isinstance(exc, CompareError):
            raise
        raise CompareError(f"pairwise 边已被另一进程占用：{left.name} vs {right.name}") from exc


def _edge_observations(records: Iterable[Mapping[str, Any]]) -> list[tuple[str, str, float, float]]:
    observations = []
    for record in records:
        if record.get("schema") != PAIRWISE_SCHEMA_VERSION: continue
        decision = record.get("decision")
        if not isinstance(decision, Mapping): continue
        margin = float(decision.get("weighted_margin", 0.0))
        if not math.isfinite(margin): continue
        observations.append((str(record["left"]), str(record["right"]), min(1.0, max(0.0, (1 + margin) / 2)), max(0.05, abs(margin))))
    return observations


def fit_bradley_terry(candidates: Iterable[str], observations: Iterable[tuple[str, str, float, float]]) -> dict[str, float]:
    names = sorted(set(candidates))
    ratings = {name: 0.0 for name in names}
    rows = list(observations)
    if not rows: return ratings
    for iteration in range(1200):
        grad = {name: 0.0 for name in names}
        for left, right, score, weight in rows:
            p = 1 / (1 + math.exp(max(-40, min(40, ratings[right] - ratings[left]))))
            delta = weight * (score - p)
            grad[left] += delta; grad[right] -= delta
        step = 0.25 / math.sqrt(1 + iteration / 30)
        for name in names: ratings[name] += step * grad[name]
        mean = sum(ratings.values()) / len(names)
        for name in names: ratings[name] -= mean
    return ratings


def _bootstrap(names: list[str], observations: list[tuple[str, str, float, float]], seed_text: str) -> dict[str, tuple[float, float]]:
    if not observations: return {name: (0.0, 0.0) for name in names}
    rng = random.Random(int(_sha256(seed_text)[:16], 16)); values = {name: [] for name in names}
    for _ in range(BOOTSTRAP_SAMPLES):
        sample = [observations[rng.randrange(len(observations))] for _ in observations]
        ratings = fit_bradley_terry(names, sample)
        for name in names: values[name].append(ratings[name])
    return {name: (sorted(value)[int(.025 * (len(value)-1))], sorted(value)[int(.975 * (len(value)-1))]) for name, value in values.items()}


def graph_is_connected(candidates: Iterable[str], observations: Iterable[tuple[str, str, float, float]]) -> bool:
    names = set(candidates)
    if not names:
        return False
    neighbours = {name: set() for name in names}
    for left, right, _score, _weight in observations:
        if left in names and right in names:
            neighbours[left].add(right); neighbours[right].add(left)
    seen = {next(iter(names))}; pending = list(seen)
    while pending:
        current = pending.pop()
        for neighbour in neighbours[current] - seen:
            seen.add(neighbour); pending.append(neighbour)
    return seen == names


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def saturation_gate(candidates: Iterable[Candidate]) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    for key in DIMENSION_KEYS:
        try:
            values = [float(candidate.aggregate["dimensions"][key]["median"]) for candidate in candidates]
        except (KeyError, TypeError, ValueError):
            detail[key] = {"distinct_medians": 0, "iqr": 0.0, "passed": False}
            continue
        distinct = len(set(values)); iqr = _quantile(values, .75) - _quantile(values, .25)
        detail[key] = {"distinct_medians": distinct, "iqr": iqr, "passed": distinct >= 3 and iqr > 0}
    return {"passed": bool(detail) and all(item["passed"] for item in detail.values()), "dimensions": detail}


def ranking_input_binding(
    candidates: Iterable[Candidate],
    records: Iterable[Mapping[str, Any]],
    pairwise_rubric_hash: str | None,
) -> dict[str, Any]:
    candidate_list = list(candidates)
    record_list = list(records)
    payload = {
        "pairwise_rubric_hash": pairwise_rubric_hash,
        "aggregate_hashes": {
            candidate.name: candidate.aggregate_hash
            for candidate in sorted(candidate_list, key=lambda item: item.name)
        },
        "pair_record_hashes": {
            edge_id(str(record.get("left")), str(record.get("right"))): _sha256(
                _canonical_json(record)
            )
            for record in record_list
        },
    }
    return {**payload, "binding_hash": _sha256(_canonical_json(payload))}


def build_ranking(benchmark: str, candidates: Iterable[Candidate], records: Iterable[Mapping[str, Any]], *, scope: str = "all", pilot_passed: bool = False, expected_edge_count: int | None = None, pairwise_rubric_hash: str | None = None) -> dict[str, Any]:
    candidates = list(candidates); names = [item.name for item in candidates]; records = list(records)
    observations = _edge_observations(records); ratings = fit_bradley_terry(names, observations)
    ci = _bootstrap(names, observations, _canonical_json(records))
    ordered = sorted(names, key=lambda name: (-ratings[name], name))
    ranking = []
    for index, name in enumerate(ordered, 1):
        probabilities = [1 / (1 + math.exp(ratings[other] - ratings[name])) for other in names if other != name]
        lower, upper = ci[name]
        overlaps = index < len(ordered) and not (upper < ci[ordered[index]][0] or ci[ordered[index]][1] < lower)
        ranking.append({"rank": index, "candidate": name, "rating": ratings[name], "win_probability": sum(probabilities) / len(probabilities) if probabilities else 0.5, "rating_ci95": [lower, upper], "ci_overlaps_next": overlaps})
    connected = graph_is_connected(names, observations)
    ci_present = len(ranking) == len(names) and all(isinstance(row.get("rating_ci95"), list) and len(row["rating_ci95"]) == 2 for row in ranking)
    saturation = saturation_gate(candidates)
    expected = len(observations) if expected_edge_count is None else expected_edge_count
    binding = ranking_input_binding(candidates, records, pairwise_rubric_hash)
    unique_pairs = len(binding["pair_record_hashes"]) == len(records)
    status = "complete" if len(observations) == expected and unique_pairs and connected and ci_present else "incomplete"
    eligible = (
        status == "complete"
        and scope == "all"
        and saturation["passed"]
        and pilot_passed
        and isinstance(pairwise_rubric_hash, str)
        and bool(pairwise_rubric_hash)
    )
    return {"schema": RANKING_SCHEMA_VERSION, "benchmark": benchmark, "scope": scope, "status": status, "expected_edges": expected, "input_binding": binding, "method": {"fit": "weighted-bradley-terry", "bootstrap_samples": BOOTSTRAP_SAMPLES, "seed": _sha256(_canonical_json(records))}, "completed_edges": len(observations), "connected_graph": connected, "every_candidate_ci_present": ci_present, "saturation_gate": saturation, "pilot_acceptance_passed": pilot_passed, "eligible_for_default": eligible, "ranking": ranking}


def _score_vote_is_usable(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("schema") != getattr(_score_v4, "SCHEMA_VERSION", "novel-eval.v4"):
        return False
    dimensions = value.get("dimensions"); repair = value.get("repair")
    if not isinstance(dimensions, Mapping) or set(dimensions) != set(DIMENSION_KEYS):
        return False
    if not isinstance(repair, Mapping) or set(repair) != {"attempted", "validation_error"} or type(repair.get("attempted")) is not bool:
        return False
    if repair["attempted"] and not isinstance(repair.get("validation_error"), str):
        return False
    for key in DIMENSION_KEYS:
        entry = dimensions[key]
        if not isinstance(entry, Mapping) or isinstance(entry.get("score"), bool) or not isinstance(entry.get("score"), (int, float)):
            return False
    return True


def _weighted_absolute_vote(vote: Mapping[str, Any]) -> float:
    return sum(DIMENSION_WEIGHTS[key] * float(vote["dimensions"][key]["score"]) for key in DIMENSION_KEYS)


def _pilot_binding(root: Path, benchmark: str, candidates: Mapping[str, Candidate], vote_paths: Mapping[str, Mapping[str, Path]], pair_paths: Mapping[str, Path], ranking_path: Path) -> dict[str, Any]:
    try:
        absolute_rubric = _score_v4.load_system_prompt(root)
    except Exception as exc:
        raise CompareError(f"无法读取 V4 绝对评分 rubric：{exc}") from exc
    ranking = _load_json_if_present(ranking_path)
    if ranking is None:
        raise CompareError("缺少 pilot ranking；不能放行 --all")
    return {"schemas": {"score": getattr(_score_v4, "SCHEMA_VERSION", None), "aggregate": getattr(_score_v4, "AGGREGATE_SCHEMA_VERSION", None), "pairwise": PAIRWISE_SCHEMA_VERSION, "ranking": RANKING_SCHEMA_VERSION}, "rubric_hashes": {"absolute": _sha256(absolute_rubric), "pairwise": _sha256(load_system_prompt(root))}, "aggregate_hashes": {name: candidate.aggregate_hash for name, candidate in sorted(candidates.items())}, "vote_hashes": {name: {judge: _sha256(_canonical_json(_read_json(path))) for judge, path in sorted(paths.items())} for name, paths in sorted(vote_paths.items())}, "pair_hashes": {name: _sha256(_canonical_json(_read_json(path))) for name, path in sorted(pair_paths.items())}, "ranking_hash": _sha256(_canonical_json(ranking))}


def build_pilot_acceptance(root: Path, benchmark: str) -> dict[str, Any]:
    candidates = {candidate.name: candidate for candidate in load_completed_candidates(root, benchmark, allowed=PILOT_CANDIDATES)}
    missing = [name for name in PILOT_CANDIDATES if name not in candidates]
    votes: dict[str, dict[str, Mapping[str, Any]]] = {}; vote_paths: dict[str, dict[str, Path]] = {}
    try:
        cfg = _llm_api.load_config(root / "config.yaml")
        absolute_prompt = _score_v4.load_system_prompt(root)
        absolute_judges = _score_v4.resolve_judge_configs(cfg)
        score_validation_ready = True
    except Exception:
        cfg = {}; absolute_prompt = ""; absolute_judges = {}; score_validation_ready = False
    for name in PILOT_CANDIDATES:
        votes[name] = {}; vote_paths[name] = {}
        for judge in JUDGE_IDS:
            path = root / "results" / benchmark / name / "scores-v4" / f"{judge}.json"
            value = _load_json_if_present(path)
            if value is None or not score_validation_ready:
                continue
            try:
                submission = _score_v4.load_submission(root, benchmark, name)
                key = _score_v4.score_cache_key(submission, absolute_prompt, judge, absolute_judges[judge], _score_v4.V4_REQUEST_OVERRIDES)
                identity = _score_v4.public_score_identity(submission, judge, absolute_judges[judge], absolute_prompt, _score_v4.V4_REQUEST_OVERRIDES)
                valid = _score_v4._valid_public_score(value, key, identity, submission.chapters)
            except Exception:
                valid = False
            if valid:
                votes[name][judge] = value; vote_paths[name][judge] = path
    pilot_candidates = [candidates[name] for name in PILOT_CANDIDATES if name in candidates]
    edge_names = select_edges(pilot_candidates) if len(pilot_candidates) == len(PILOT_CANDIDATES) else []
    pair_paths = {f"{left}|{right}": root / "results" / benchmark / "_pairwise-v4" / "pairs" / f"{edge_id(left, right)}.json" for left, right in edge_names}
    valid_pairs: list[Mapping[str, Any]] = []
    try:
        pair_prompt = load_system_prompt(root)
        pair_judges = resolve_judge_configs(cfg)
        order_plan = build_order_plan(edge_names)
        for left_name, right_name in edge_names:
            record = _load_json_if_present(pair_paths[f"{left_name}|{right_name}"])
            if _record_valid(record, candidates[left_name], candidates[right_name], pair_cache_key(candidates[left_name], candidates[right_name], pair_prompt, pair_judges, order_plan[(left_name, right_name)]), order_plan[(left_name, right_name)]):
                valid_pairs.append(record)  # type: ignore[arg-type]
    except Exception:
        valid_pairs = []
    conditions: dict[str, Any] = {"twelve_valid_votes": sum(len(item) for item in votes.values()) == 12, "five_pairwise_edges": len(valid_pairs) == 5}
    anchor_names = ("minimax-m3", "gpt-5.6-sol", "grok-4.6")
    anchors_ready = all(name in candidates for name in anchor_names)
    if anchors_ready:
        mini, sol, grok = (candidates[name] for name in anchor_names)
        conditions["minimax_overall_gap"] = {"passed": mini.score <= sol.score - 15 and mini.score <= grok.score - 15, "minimax": mini.score, "sol": sol.score, "grok": grok.score}
        try:
            lower_dimensions = sum(float(mini.aggregate["dimensions"][key]["median"]) < float(sol.aggregate["dimensions"][key]["median"]) and float(mini.aggregate["dimensions"][key]["median"]) < float(grok.aggregate["dimensions"][key]["median"]) for key in DIMENSION_KEYS)
        except (KeyError, TypeError, ValueError):
            lower_dimensions = 0
        conditions["minimax_lower_dimensions"] = {"passed": lower_dimensions >= 5, "count": lower_dimensions}
        per_judge = {judge: {name: _weighted_absolute_vote(votes[name][judge]) for name in anchor_names} for judge in JUDGE_IDS if all(judge in votes[name] for name in anchor_names)}
        conditions["minimax_below_anchors_per_judge"] = {"passed": len(per_judge) == len(JUDGE_IDS) and all(values["minimax-m3"] < values["gpt-5.6-sol"] and values["minimax-m3"] < values["grok-4.6"] for values in per_judge.values()), "totals": per_judge}
    else:
        conditions["minimax_overall_gap"] = {"passed": False}; conditions["minimax_lower_dimensions"] = {"passed": False}; conditions["minimax_below_anchors_per_judge"] = {"passed": False}
    identical = 0
    subitems_available = len(pilot_candidates) == 4
    if subitems_available:
        try:
            for spec in DIMENSION_SPECS:
                for subscore in spec.subscores:
                    values = [candidate.aggregate["dimensions"][spec.key]["subscores"][subscore]["median"] for candidate in pilot_candidates]
                    identical += len(set(values)) == 1
        except (KeyError, TypeError):
            subitems_available = False
    conditions["aggregate_subitem_collapse"] = {"passed": subitems_available and identical <= 6, "identical_of_24": identical}
    agreed = total = 0
    for record in valid_pairs:
        all_votes = dict(record.get("initial_votes", {})); all_votes.update({f"reverse:{key}": value for key, value in (record.get("reverse_votes") or {}).items()})
        summary = summarize_votes(all_votes, str(record["left"])) if all_votes else None
        if summary:
            wins, losses = int(summary["left_judge_wins"]), int(summary["right_judge_wins"])
            agreed += max(wins, losses); total += wins + losses
    conditions["non_tie_majority_agreement"] = {"passed": total > 0 and agreed / total >= .70, "agreement": agreed / total if total else 0.0, "non_tie_votes": total}
    repairs = {name: {judge: vote.get("repair") for judge, vote in values.items()} for name, values in votes.items()}
    ranking_path = root / "results" / benchmark / "_pairwise-v4" / "pilot-ranking.json"
    try:
        binding = _pilot_binding(root, benchmark, candidates, vote_paths, pair_paths, ranking_path)
        binding_hash = _sha256(_canonical_json(binding))
    except CompareError as exc:
        binding = {"error": str(exc)}; binding_hash = None
    passed = all(value if isinstance(value, bool) else bool(value.get("passed")) for value in conditions.values()) and binding_hash is not None
    return {"schema": "novel-pairwise-pilot-acceptance.v4", "benchmark": benchmark, "scope": "pilot", "binding": binding, "binding_hash": binding_hash, "conditions": conditions, "repair_transparency": repairs, "passed": passed}


def current_pilot_gate(root: Path, benchmark: str) -> tuple[bool, str]:
    stored = _load_json_if_present(root / "results" / benchmark / "_pairwise-v4" / "pilot-acceptance.json")
    if not isinstance(stored, Mapping):
        return False, "缺少 pilot-acceptance.json"
    fresh = build_pilot_acceptance(root, benchmark)
    if not fresh.get("passed"):
        return False, "当前 pilot 证据未通过 acceptance 条件"
    if stored.get("binding_hash") != fresh.get("binding_hash"):
        return False, "pilot acceptance 与当前 vote/ranking/aggregate/rubric 哈希不一致"
    if stored.get("passed") is not True:
        return False, "已保存的 pilot acceptance 未通过"
    return True, "passed"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V4 匿名成对小说排名")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--pilot", action="store_true", help="只比较四个指定试点候选的五条相邻/隔一位边")
    choice.add_argument("--all", action="store_true", help="比较全部相邻和距离二的边")
    parser.add_argument("--dry-run", action="store_true", help="只显示缓存与待比较项，不访问网络")
    parser.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    return parser


def run(argv: Iterable[str] | None = None, *, root: Path | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    root = (root or Path(__file__).resolve().parent.parent).resolve()
    pilot_passed = False
    if args.all:
        passed, reason = current_pilot_gate(root, args.benchmark)
        if not passed:
            print(f"[compare-v4] all: BLOCKED: {reason}", file=sys.stderr)
            return 1
        pilot_passed = True
    candidates = load_completed_candidates(root, args.benchmark, allowed=PILOT_CANDIDATES if args.pilot else None)
    if args.pilot:
        present = {candidate.name for candidate in candidates}
        missing = [name for name in PILOT_CANDIDATES if name not in present]
        if missing:
            raise CompareError(
                "pilot 必须先具备四份当前 completed scores-v4 aggregate；缺少："
                + "、".join(missing)
            )
    elif not args.all and len(candidates) < 2:
        raise CompareError("至少需要两份 completed scores-v4 aggregate")
    by_name = {candidate.name: candidate for candidate in candidates}; edges = select_edges(candidates)
    if args.all:
        missing = [name for name in ALL_CANDIDATES if name not in by_name]
        extra = sorted(set(by_name) - set(ALL_CANDIDATES))
        if missing or extra or len(edges) != 25:
            detail = []
            if missing:
                detail.append("缺少：" + "、".join(missing))
            if extra:
                detail.append("额外：" + "、".join(extra))
            detail.append(f"计划边={len(edges)}，要求=25")
            raise CompareError("全量 pairwise 必须是固定 14 本完整队列；" + "；".join(detail))
    prompt = load_system_prompt(root); cfg = _llm_api.load_config(root / "config.yaml"); judge_configs = resolve_judge_configs(cfg)
    order_plan = build_order_plan(edges)
    planned = [(by_name[left], by_name[right], order_plan[(left, right)]) for left, right in edges]
    # Fail closed before credentials/model discovery.  The same complete
    # content is sent for every label order; this is validation, never slicing.
    context_details = {f"{left.name}|{right.name}": validate_edge_context(prompt, left, right, judge_configs, orders) for left, right, orders in planned}
    statuses = []
    for left, right, orders in planned:
        key = pair_cache_key(left, right, prompt, judge_configs, orders); path = root / "results" / args.benchmark / "_pairwise-v4" / "pairs" / f"{edge_id(left.name, right.name)}.json"
        cached = _load_json_if_present(path)
        statuses.append(_record_valid(cached, left, right, key, orders))
    if args.dry_run:
        base = len(planned) * len(JUDGE_IDS)
        print(f"[compare-v4] requests: base={base}; max-reversal-additions={base}; maximum={base * 2}")
        for (left, right, _orders), cached in zip(planned, statuses):
            detail = context_details[f"{left.name}|{right.name}"]
            judges = ", ".join(f"{judge}: chars={item['characters']} input_tokens={item['input_tokens']} output={item['output_tokens']} ceiling={item['safety_ceiling']} fits" for judge, item in detail.items())
            print(f"[compare-v4] {left.name} vs {right.name}: {'cached' if cached else 'would-compare'}; {judges}")
        return 0
    client = None
    if not all(statuses):
        env = _llm_api.load_env_file(root / ".env")
        client = _llm_api.ChatClient.from_config(cfg, env)
        available = set(client.list_models()); missing = [EXPECTED_JUDGE_MODELS[judge] for judge in JUDGE_IDS if EXPECTED_JUDGE_MODELS[judge] not in available]
        if missing: raise CompareError("/v1/models 缺少配置模型：" + ", ".join(missing))
    records = []
    for left, right, orders in planned:
        status, record = compare_edge(root, args.benchmark, left, right, prompt, judge_configs, client, orders=orders)
        print(f"[compare-v4] {left.name} vs {right.name}: {status}")
        if record: records.append(record)
    observations = _edge_observations(records)
    if not graph_is_connected([candidate.name for candidate in candidates], observations):
        raise CompareError("当前计划边图不连通；拒绝生成排名")
    ranking = build_ranking(
        args.benchmark,
        candidates,
        records,
        scope="pilot" if args.pilot else "all",
        pilot_passed=pilot_passed,
        expected_edge_count=len(planned),
        pairwise_rubric_hash=_sha256(prompt),
    )
    _atomic_write_json(root / "results" / args.benchmark / "_pairwise-v4" / "ranking.json", ranking)
    if args.pilot:
        _atomic_write_json(root / "results" / args.benchmark / "_pairwise-v4" / "pilot-ranking.json", ranking)
        acceptance = build_pilot_acceptance(root, args.benchmark)
        _atomic_write_json(root / "results" / args.benchmark / "_pairwise-v4" / "pilot-acceptance.json", acceptance)
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    try: return run(argv)
    except CompareError as exc:
        print(f"[compare-v4] ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
