#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V2.1 autonomous long-form benchmark generator.

One model receives one broad direction and keeps a single replayable chat
transcript while it creates a book pitch, a two-million-character macro
outline, a detailed opening outline, and roughly fifty thousand characters of
prose.  Legacy ten-chapter generation remains in ``generate_legacy.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:  # Script execution and package-style tests are both supported.
    from .llm_api import (
        ANTHROPIC_MESSAGES,
        OPENAI_CHAT_COMPLETIONS,
        PROVIDER_DEFAULTS_TRACKING_KEY,
        ChatClient,
        ChatResult,
        LLMAPIError,
        get_model_config,
        load_config,
        load_env_file,
        model_protocol,
        protocol_endpoint_path,
        protocol_required_parameters,
        split_inline_reasoning_text,
        with_provider_request_defaults,
    )
except ImportError:  # pragma: no cover - exercised by direct CLI usage
    from llm_api import (  # type: ignore
        ANTHROPIC_MESSAGES,
        OPENAI_CHAT_COMPLETIONS,
        PROVIDER_DEFAULTS_TRACKING_KEY,
        ChatClient,
        ChatResult,
        LLMAPIError,
        get_model_config,
        load_config,
        load_env_file,
        model_protocol,
        protocol_endpoint_path,
        protocol_required_parameters,
        split_inline_reasoning_text,
        with_provider_request_defaults,
    )


PROTOCOL_VERSION = "novel-benchmark.v2.1"
LEGACY_OPENAI_CODE_SHA256 = (
    "a71e090e70c9bf5eb6361ff2e552a0d143a5bee8aead8f79febe20615f3ea33d"
)
LEGACY_ANTHROPIC_CODE_SHA256 = (
    "61b40dba13fa56609dbb1666c525c8adc8a909381dbd8763fac68b3bb73d7ea2"
)
GENERATION_COMPATIBILITY_SOURCE_SHA256 = "af7534e4c01eb990d5603b784b322dc6cf2d4a0a2085bc29588a71dacd26f3fa"
DEFAULT_BENCHMARK = "reform-era"
PROMPT_FILES = (
    "system.md",
    "book.md",
    "macro_outline.md",
    "opening_outline.md",
    "chapter.md",
    "expand_chapter.md",
    "repair_json.md",
    "repair_chapter.md",
)
# A stage may lose attempts to transient 5xx/429 responses before it receives
# any usable text. Five total calls leave room for a real format repair while
# still bounding cost and preventing an endless retry loop.
MAX_STAGE_ATTEMPTS = 5
MAX_RETRY_DELAY_SECONDS = 30.0
CONTEXT_SAFETY_BASIS_POINTS = 8_500
CONTEXT_USAGE_MARGIN_TOKENS = 256
MIN_FINAL_CHARS = 48_000
MIN_OPENING_TARGET_CHARS = MIN_FINAL_CHARS
CHAPTER_EXPANSION_TRIGGER_CHARS = 3_000
MAX_CHAPTER_EXPANSION_CALLS = 1

PROTOCOL_POLICY = {
    "context_guard": {
        "strategy": "provider-usage-anchor-v1",
        "fallback": "cjk-conservative-v1",
        "safety_basis_points": CONTEXT_SAFETY_BASIS_POINTS,
        "usage_margin_tokens": CONTEXT_USAGE_MARGIN_TOKENS,
        "output_reserve": "none-server-defaults",
    },
    "generation": {
        "max_stage_attempts_per_execution": MAX_STAGE_ATTEMPTS,
        "repair_transcript": "isolated-latest-complete-candidate-v1",
        "canonical_session": "accepted-exchanges-only-v1",
        "api_optional_parameters": "omitted-server-defaults",
    },
    "length": {
        "opening_target_min_chars": MIN_OPENING_TARGET_CHARS,
        "final_min_chars": MIN_FINAL_CHARS,
        "hard_upper_bound": "none",
        "book_text_fields": "nonempty-no-char-range",
        "chapter_prompt_chars_approx": [3_000, 4_000],
        "chapter_validation": "format-only-no-char-range",
        "short_chapter_expansion": {
            "trigger_below_chars": CHAPTER_EXPANSION_TRIGGER_CHARS,
            "max_calls": MAX_CHAPTER_EXPANSION_CALLS,
            "failure_policy": "keep-valid-source-no-retry",
            "selection": "longer-valid-draft",
            "canonical_session": "final-draft-only",
        },
    },
}

EXPECTED_GENERATOR_IDS = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "mimo-v2.5",
    "mimo-v2.5-pro",
    "minimax-m3",
    "glm-5.2",
    "gpt-5.6-luna",
    "claude-haiku-4-5",
    "claude-sonnet-5",
    "gemini-3.1-pro",
    "gemini-3.5-flash",
    "kimi-k3",
    "grok-4.5",
    "claude-opus-4-8",
    "agnes-2.0-flash",
)
EXPECTED_JUDGES = {
    "sol": "gpt-5.6-sol",
    "grok": "grok-4.5",
    "kimi": "kimi-k3",
}
PRIVATE_REASONING_MARKER = re.compile(
    r"(?:\[/?思考过程\]|</?(?:think|thinking|analysis)>|reasoning_content)",
    re.IGNORECASE,
)


def api_error_is_retryable(error: LLMAPIError) -> bool:
    status = error.status_code
    return status is None or status in {408, 409, 425, 429} or status >= 500


def retry_delay_seconds(error: LLMAPIError, attempt: int) -> float:
    advised = getattr(error, "retry_after_seconds", None)
    if isinstance(advised, (int, float)) and math.isfinite(float(advised)):
        return min(MAX_RETRY_DELAY_SECONDS, max(0.0, float(advised)))
    return min(8.0, float(2 ** max(0, attempt - 1)))


class IncompleteCompletionError(LLMAPIError):
    """A non-empty response that the upstream marked as unfinished."""

    def __init__(self, finish_reason: str | None, content: str) -> None:
        label = finish_reason or "missing"
        super().__init__(f"finish_reason={label}，拒绝接受可能截断的输出")
        self.content = content


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    print(f"[v2.1] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[v2.1] [WARN] {message}", flush=True)


def fail(message: str) -> None:
    print(f"[v2.1] [ERR] {message}", file=sys.stderr, flush=True)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_newlines(value: str) -> str:
    """Canonicalize UTF-8 text across editors and operating systems."""

    return value.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def canonical_text(value: str) -> str:
    return normalize_newlines(value).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_normalized_text_file(path: Path) -> str:
    text = path.read_bytes().decode("utf-8-sig")
    return sha256_text(normalize_newlines(text))


def _current_source_code_hash() -> str:
    """Hash the current V2 runner sources for newly introduced protocols."""

    files = (Path(__file__).resolve(), Path(__file__).resolve().with_name("llm_api.py"))
    evidence = {
        path.name: sha256_normalized_text_file(path)
        for path in files
        if path.exists()
    }
    return sha256_text(canonical_json(evidence))


def _generation_compatibility_source_hash() -> str:
    """Fingerprint the exact judge-registry migration sources.

    The guard value itself is normalized so it can record this fingerprint
    without changing it.
    """

    files = (Path(__file__).resolve(), Path(__file__).resolve().with_name("llm_api.py"))
    evidence: dict[str, str] = {}
    for path in files:
        if not path.exists():
            continue
        text = normalize_newlines(path.read_bytes().decode("utf-8-sig"))
        if path == Path(__file__).resolve():
            text = re.sub(
                r'GENERATION_COMPATIBILITY_SOURCE_SHA256 = "[^"]+"',
                'GENERATION_COMPATIBILITY_SOURCE_SHA256 = "<guard>"',
                text,
                count=1,
            )
        evidence[path.name] = sha256_text(text)
    return sha256_text(canonical_json(evidence))


def calculate_code_hash(model_cfg: dict[str, Any] | None = None) -> str:
    """Return a protocol-scoped implementation identity.

    Replacing a scoring judge does not change either generation transport.
    Preserve the identities used by existing O-port and A-port books only for
    this exact source fingerprint; any later runner change fails closed to the
    current source hash.
    """

    current_hash = _current_source_code_hash()
    protocol = model_protocol(model_cfg or {})
    if (
        _generation_compatibility_source_hash()
        == GENERATION_COMPATIBILITY_SOURCE_SHA256
    ):
        if protocol == OPENAI_CHAT_COMPLETIONS:
            return LEGACY_OPENAI_CODE_SHA256
        if protocol == ANTHROPIC_MESSAGES:
            return LEGACY_ANTHROPIC_CODE_SHA256
    return current_hash


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} 不是 JSON object")
    return value


def _read_usage_journal(work_dir: Path) -> list[dict[str, Any]]:
    journal = work_dir / "usage.jsonl"
    if not journal.exists():
        return []
    try:
        text = journal.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("usage.jsonl UTF-8 不完整") from exc
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"usage.jsonl 第 {line_number} 行损坏") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"usage.jsonl 第 {line_number} 行不是对象")
        records.append(value)
    return records


def read_usage_records(work_dir: Path) -> list[dict[str, Any]]:
    """Read atomic usage events, safely reconciling a legacy JSONL migration."""

    events_dir = work_dir / "usage-events"
    event_paths = sorted(events_dir.glob("*.json")) if events_dir.is_dir() else []
    if event_paths:
        expected_names = [f"{index:06d}.json" for index in range(1, len(event_paths) + 1)]
        if [path.name for path in event_paths] != expected_names:
            raise RuntimeError("usage-events 序号不连续")
        event_records = [read_json(path) for path in event_paths]
        try:
            journal_records = _read_usage_journal(work_dir)
        except RuntimeError:
            # Atomic event files are the source of truth; a derived journal can
            # be rebuilt without losing an audited call.
            return event_records
        shared = min(len(event_records), len(journal_records))
        if event_records[:shared] != journal_records[:shared]:
            raise RuntimeError("usage-events 与 usage.jsonl 内容不一致")
        # During legacy migration the journal may still be longer; during a
        # new append the atomic events may be one record ahead of the journal.
        return (
            journal_records
            if len(journal_records) > len(event_records)
            else event_records
        )
    return _read_usage_journal(work_dir)


def count_content_chars(text: str) -> int:
    """Count visible CJK/alphanumeric content, excluding Markdown headings."""
    body_lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", "\n".join(body_lines)))


def estimate_tokens(messages: list[dict[str, str]]) -> int:
    """Conservative tokenizer-independent estimate for context guarding."""
    total = 0
    for message in messages:
        text = str(message.get("content", ""))
        cjk = len(re.findall(r"[\u3400-\u9fff]", text))
        other = max(0, len(text) - cjk)
        total += cjk + (other + 3) // 4 + 8
    return total


def estimate_prompt_tokens(
    messages: list[dict[str, str]],
    usage_records: list[dict[str, Any]],
) -> int:
    """Return only the numeric portion of ``estimate_prompt_tokens_with_audit``."""

    return estimate_prompt_tokens_with_audit(messages, usage_records)[0]


def estimate_prompt_tokens_with_audit(
    messages: list[dict[str, str]],
    usage_records: list[dict[str, Any]],
) -> tuple[int, str, int | None]:
    """Estimate the next prompt and identify its replay-safe evidence source.

    The fallback intentionally overestimates CJK text. After a successful call,
    provider ``prompt_tokens`` is an exact prefix anchor. Only the assistant
    content actually saved in the transcript and the pending user prompt are
    added; hidden reasoning tokens are not replayed and therefore must not be
    charged again. Hash/count checks reject torn or mismatched checkpoints.
    """

    fallback = estimate_tokens(messages)
    if not messages or messages[-1].get("role") != "user":
        return fallback, "fallback", None

    # Validation failures are deliberately absent from the canonical session,
    # so the number of successful provider responses no longer equals the
    # number of saved assistant turns. Find the newest usage event whose exact
    # prompt is a prefix of this request and whose response is the next saved
    # assistant turn. This preserves an exact provider anchor when possible and
    # safely falls back after a repaired exchange was canonicalized.
    successful = [
        record for record in usage_records if record.get("status") != "api_error"
    ]
    for latest in reversed(successful):
        usage = latest.get("usage")
        context_audit = latest.get("context_audit")
        if not isinstance(usage, dict) or not isinstance(context_audit, dict):
            continue
        prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        prompt_count = context_audit.get("prompt_message_count")
        if (
            isinstance(prompt_tokens, bool)
            or not isinstance(prompt_tokens, int)
            or prompt_tokens <= 0
            or isinstance(prompt_count, bool)
            or not isinstance(prompt_count, int)
            or prompt_count < 1
            or prompt_count >= len(messages)
        ):
            continue
        prior_prompt = messages[:prompt_count]
        saved_assistant = messages[prompt_count]
        if (
            saved_assistant.get("role") != "assistant"
            or context_audit.get("prompt_sha256")
            != sha256_text(canonical_json(prior_prompt))
            or context_audit.get("assistant_content_sha256")
            != sha256_text(str(saved_assistant.get("content", "")))
        ):
            continue
        estimate = (
            prompt_tokens
            + estimate_tokens(messages[prompt_count:])
            + CONTEXT_USAGE_MARGIN_TOKENS
        )
        event_index = latest.get("event_index")
        if isinstance(event_index, bool) or not isinstance(event_index, int):
            event_index = None
        return estimate, "provider_usage_anchor", event_index
    return fallback, "fallback", None


def protocol_policy_sha256() -> str:
    return sha256_text(canonical_json(PROTOCOL_POLICY))


def generation_request_parameters(
    model_cfg: dict[str, Any], stage: str
) -> dict[str, Any]:
    """Resolve the exact optional parameters ChatClient would send upstream."""

    params: dict[str, Any] = {}
    stages = model_cfg.get("stages") or {}
    if not isinstance(stages, dict):
        raise ValueError("model.stages 必须是对象")
    layers = (
        ("provider.request_defaults", model_cfg.get(PROVIDER_DEFAULTS_TRACKING_KEY)),
        ("model.request", model_cfg.get("request")),
        (f"model.stages.{stage}", stages.get(stage)),
    )
    for label, layer in layers:
        if layer is None:
            continue
        if not isinstance(layer, dict):
            raise ValueError(f"{label} 必须是对象")
        params.update(layer)
    return params


def build_run_input(
    benchmark: str,
    direction: str,
    prompts: dict[str, str],
    model_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Build the complete, reproducible identity of a generation run."""

    return {
        "protocol": PROTOCOL_VERSION,
        "policy": PROTOCOL_POLICY,
        "benchmark": benchmark,
        "direction": canonical_text(direction),
        "prompts": {name: canonical_text(value) for name, value in prompts.items()},
        "model": model_cfg,
        "runner_code_sha256": calculate_code_hash(model_cfg),
    }


def load_prompts(path: Path) -> dict[str, str]:
    prompts: dict[str, str] = {}
    for name in PROMPT_FILES:
        prompt_path = path / name
        if not prompt_path.exists() and path.name == "v2.1":
            prompt_path = path.parent / "v2" / name
        if not prompt_path.exists():
            raise FileNotFoundError(f"缺少 V2.1 prompt：{prompt_path}")
        prompts[name] = canonical_text(prompt_path.read_bytes().decode("utf-8-sig"))
    return prompts


def parse_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip().lstrip("\ufeff")
    fenced = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
    initial_error: json.JSONDecodeError | None = None
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        initial_error = exc
        # A few Chat Completions adapters advertise JSON mode but omit the
        # final ASCII quote of the last string while still returning the final
        # object brace. Repair only that unambiguous delimiter; no prose bytes
        # are changed, and the untouched upstream response remains in raw/.
        if exc.msg.startswith("Unterminated string") and candidate.rstrip().endswith("}"):
            closing_brace = candidate.rfind("}")
            repaired = candidate[:closing_brace] + '"' + candidate[closing_brace:]
            try:
                value = json.loads(repaired)
            except json.JSONDecodeError:
                pass
            else:
                if not isinstance(value, dict):
                    raise ValueError("响应 JSON 不是 object")
                return value
        start = candidate.find("{")
        if start < 0:
            raise ValueError("响应中没有 JSON object")
        depth = 0
        in_string = False
        escaped = False
        end = -1
        for index, char in enumerate(candidate[start:], start=start):
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end < 0:
            assert initial_error is not None
            raise ValueError(
                "响应中的 JSON object 不完整："
                f"{initial_error.msg}（line {initial_error.lineno}, column {initial_error.colno}）"
            )
        value = json.loads(candidate[start:end])
    if not isinstance(value, dict):
        raise ValueError("响应 JSON 不是 object")
    return value


def _nonempty(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not None


def _require_exact_fields(
    value: dict[str, Any], expected: tuple[str, ...], label: str
) -> None:
    missing = [field for field in expected if field not in value]
    extra = sorted(set(value) - set(expected))
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append("缺少 " + ", ".join(missing))
        if extra:
            details.append("多出 " + ", ".join(extra))
        raise ValueError(f"{label} 字段不符合协议：" + "；".join(details))


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串")
    return value.strip()


def _require_text_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} 必须是非空字符串数组")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label} 必须是非空字符串数组")
    return value


def _reject_private_reasoning_markers(value: Any, label: str) -> None:
    if isinstance(value, str):
        if PRIVATE_REASONING_MARKER.search(value):
            raise ValueError(f"{label} 包含私有 reasoning 标记")
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            _reject_private_reasoning_markers(nested, f"{label}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_private_reasoning_markers(nested, f"{label}[{index}]")


def validate_book(data: dict[str, Any]) -> dict[str, Any]:
    _reject_private_reasoning_markers(data, "book")
    required = ("title", "blurb", "protagonist", "setting", "core_theme", "ending_direction")
    _require_exact_fields(data, required, "book")
    for field in required:
        data[field] = _require_text(data[field], f"book.{field}")
    return data


def validate_macro_outline(data: dict[str, Any]) -> dict[str, Any]:
    _reject_private_reasoning_markers(data, "macro_outline")
    _require_exact_fields(
        data,
        ("target_total_chars", "volumes", "character_arcs", "foreshadowing", "ending"),
        "macro_outline",
    )
    volumes = data.get("volumes")
    if not isinstance(volumes, list) or not 10 <= len(volumes) <= 20:
        raise ValueError("macro_outline.volumes 必须有 10–20 卷")
    total = 0
    for expected, volume in enumerate(volumes, start=1):
        if not isinstance(volume, dict) or volume.get("number") != expected:
            raise ValueError(f"第 {expected} 卷 number 不连续")
        _require_exact_fields(
            volume,
            ("number", "title", "target_chars", "period", "start_state", "end_state", "main_conflict", "arcs"),
            f"第 {expected} 卷",
        )
        for field in ("title", "period", "start_state", "end_state", "main_conflict"):
            _require_text(volume[field], f"第 {expected} 卷.{field}")
        target = volume.get("target_chars")
        if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
            raise ValueError(f"第 {expected} 卷 target_chars 无效")
        arcs = volume.get("arcs")
        if not isinstance(arcs, list) or not 3 <= len(arcs) <= 6:
            raise ValueError(f"第 {expected} 卷必须有 3–6 个剧情弧")
        for arc_index, arc in enumerate(arcs, start=1):
            if not isinstance(arc, dict):
                raise ValueError(f"第 {expected} 卷第 {arc_index} 个剧情弧必须是对象")
            _require_exact_fields(arc, ("title", "summary"), f"第 {expected} 卷第 {arc_index} 个剧情弧")
            _require_text(arc["title"], f"第 {expected} 卷第 {arc_index} 个剧情弧.title")
            _require_text(arc["summary"], f"第 {expected} 卷第 {arc_index} 个剧情弧.summary")
        total += target
    declared = data.get("target_total_chars")
    if isinstance(declared, bool) or not isinstance(declared, int):
        raise ValueError("macro_outline.target_total_chars 必须为整数")
    if not 1_800_000 <= total <= 2_200_000:
        raise ValueError(f"各卷目标合计应约 200 万字，当前 {total}")
    if abs(declared - total) > 10_000:
        raise ValueError("target_total_chars 与各卷合计不一致")
    _require_text_list(data["character_arcs"], "macro_outline.character_arcs")
    _require_text_list(data["foreshadowing"], "macro_outline.foreshadowing")
    _require_text(data["ending"], "macro_outline.ending")
    return data


def validate_opening_outline(data: dict[str, Any]) -> dict[str, Any]:
    _reject_private_reasoning_markers(data, "opening_outline")
    _require_exact_fields(
        data,
        ("target_total_chars", "macro_scope", "chapters"),
        "opening_outline",
    )
    chapters = data.get("chapters")
    if not isinstance(chapters, list) or not 16 <= len(chapters) <= 18:
        raise ValueError("opening_outline.chapters 必须有 16–18 章")
    total = 0
    for expected, chapter in enumerate(chapters, start=1):
        if not isinstance(chapter, dict) or chapter.get("number") != expected:
            raise ValueError(f"第 {expected} 章 number 不连续")
        _require_exact_fields(
            chapter,
            (
                "number", "title", "target_chars", "summary", "beats",
                "continuity_in", "continuity_out", "foreshadowing",
            ),
            f"第 {expected} 章细纲",
        )
        _require_text(chapter["title"], f"第 {expected} 章.title")
        _require_text(chapter["summary"], f"第 {expected} 章.summary")
        for field in ("beats", "continuity_in", "continuity_out", "foreshadowing"):
            _require_text_list(chapter[field], f"第 {expected} 章.{field}")
        target = chapter.get("target_chars")
        if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
            raise ValueError(f"第 {expected} 章 target_chars 必须为正整数")
        total += target
    declared = data.get("target_total_chars")
    if isinstance(declared, bool) or not isinstance(declared, int):
        raise ValueError("opening_outline.target_total_chars 必须为整数")
    if declared < MIN_OPENING_TARGET_CHARS:
        raise ValueError(
            "opening_outline.target_total_chars "
            f"应不少于 {MIN_OPENING_TARGET_CHARS} 字"
        )
    if total < MIN_OPENING_TARGET_CHARS:
        raise ValueError(
            f"前段细纲目标合计应不少于 {MIN_OPENING_TARGET_CHARS} 字，当前 {total}"
        )
    if abs(declared - total) > 500:
        raise ValueError("target_total_chars 与各章合计不一致")
    _require_text(data["macro_scope"], "opening_outline.macro_scope")
    return data


def normalize_chapter(text: str) -> str:
    return canonical_text(text)


def validate_chapter(text: str, chapter: dict[str, Any]) -> str:
    normalized = normalize_chapter(text)
    _reject_private_reasoning_markers(normalized, f"第 {chapter['number']} 章")
    if "```" in normalized:
        raise ValueError("正文包含代码围栏")
    number = chapter["number"]
    title = str(chapter["title"]).strip()
    lines = normalized.splitlines()
    if not lines:
        raise ValueError("章节为空")
    heading = re.match(r"^##\s*第\s*(\d+)\s*章\s*(.*)$", lines[0].strip())
    if not heading or int(heading.group(1)) != number:
        raise ValueError(f"必须以 ## 第{number}章 {title} 开头")
    if heading.group(2).strip() != title:
        raise ValueError(f"章节标题必须与细纲一致：{title}")
    opening = "\n".join(lines[:5])
    if re.search(r"(?:下面|以下)(?:是|为).{0,8}(?:正文|章节)|创作说明|写作说明|作为.{0,8}(?:模型|AI)", opening):
        raise ValueError("章节开头包含元评论")
    if count_content_chars(normalized) == 0:
        raise ValueError("章节正文为空")
    return normalized


def _chat_result_dict(result: ChatResult) -> dict[str, Any]:
    if is_dataclass(result):
        value = asdict(result)
    elif hasattr(result, "__dict__"):
        value = dict(result.__dict__)
    else:
        value = {}
    value.pop("raw_response", None)
    return value


_PRIVATE_MANIFEST_FIELDS = {
    "api_key", "apikey", "authorization", "headers", "proxy_authorization",
    "token", "reasoning", "reasoning_content", "raw_response",
}


def _public_manifest_value(value: Any) -> Any:
    """Copy config metadata while excluding credentials and private reasoning."""
    if isinstance(value, dict):
        return {
            str(key): _public_manifest_value(nested)
            for key, nested in value.items()
            if str(key).strip().lower().replace("-", "_") not in _PRIVATE_MANIFEST_FIELDS
        }
    if isinstance(value, list):
        return [_public_manifest_value(item) for item in value]
    return value


def work_checkpoint_is_resumable(
    work_dir: Path,
    run_id: str,
    *,
    run_input_sha256: str | None = None,
    policy_sha256: str | None = None,
    code_sha256: str | None = None,
) -> bool:
    """Deep-check accepted artifacts and transcript/state checkpoint invariants."""

    try:
        state = read_json(work_dir / "state.json")
        session = read_json(work_dir / "session.json")
        if (
            state.get("schema") != PROTOCOL_VERSION
            or session.get("schema") != PROTOCOL_VERSION
            or state.get("run_id") != run_id
            or session.get("run_id") != run_id
            or state.get("run_input_sha256") != session.get("run_input_sha256")
            or state.get("protocol_policy_sha256")
            != session.get("protocol_policy_sha256")
            or state.get("code_sha256_at_start")
            != session.get("code_sha256_at_start")
            or not re.fullmatch(r"[0-9a-f]{64}", str(state.get("run_input_sha256") or ""))
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(state.get("protocol_policy_sha256") or "")
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(state.get("code_sha256_at_start") or "")
            )
            or (run_input_sha256 is not None and state.get("run_input_sha256") != run_input_sha256)
            or (policy_sha256 is not None and state.get("protocol_policy_sha256") != policy_sha256)
            or (code_sha256 is not None and state.get("code_sha256_at_start") != code_sha256)
        ):
            return False
        stage = state.get("stage")
        stage_order = {
            "book": 0,
            "macro_outline": 1,
            "opening_outline": 2,
            "chapters": 3,
            "publish": 4,
            "completed": 5,
        }
        if stage not in stage_order:
            return False
        messages = session.get("messages")
        if (
            not isinstance(messages, list)
            or not messages
            or (len(messages) - 1) % 2 != 0
            or messages[0].get("role") != "system"
            or not isinstance(messages[0].get("content"), str)
            or not messages[0]["content"].strip()
            or any(
                not isinstance(message, dict)
                or message.get("role") != ("user" if index % 2 else "assistant")
                or not isinstance(message.get("content"), str)
                for index, message in enumerate(messages[1:], start=1)
            )
        ):
            return False

        accepted = work_dir / "accepted"
        book = macro = opening = None
        book_path = accepted / "book.json"
        macro_path = accepted / "macro_outline.json"
        opening_path = accepted / "opening_outline.json"
        if (
            (stage_order[stage] < 1 and macro_path.exists())
            or (stage_order[stage] < 2 and opening_path.exists())
        ):
            return False
        if book_path.exists():
            book = validate_book(read_json(book_path))
        elif stage_order[stage] >= 1:
            return False
        if macro_path.exists():
            macro = validate_macro_outline(read_json(macro_path))
        elif stage_order[stage] >= 2:
            return False
        if opening_path.exists():
            opening = validate_opening_outline(
                read_json(opening_path)
            )
        elif stage_order[stage] >= 3:
            return False

        completed = state.get("completed_chapters")
        next_chapter = state.get("next_chapter")
        completed_count = 0
        actual_numbers: set[int] = set()
        if opening is not None:
            chapter_dir = accepted / "chapters"
            actual_numbers = {
                int(path.stem)
                for path in chapter_dir.glob("*.md")
                if re.fullmatch(r"\d{2}", path.stem)
            } if chapter_dir.is_dir() else set()
            if stage_order[stage] < 3 and actual_numbers:
                return False
        if stage_order[stage] >= 3:
            if (
                not isinstance(completed, list)
                or any(isinstance(number, bool) or not isinstance(number, int) for number in completed)
                or isinstance(next_chapter, bool)
                or not isinstance(next_chapter, int)
                or not 1 <= next_chapter <= len(opening["chapters"]) + 1
                or completed != list(range(1, next_chapter))
            ):
                return False
            completed_count = len(completed)
            chapter_dir = accepted / "chapters"
            required_numbers = set(completed)
            # One extra file is the supported crash window between committing
            # a validated chapter and advancing state.json.
            allowed_numbers = set(required_numbers)
            if next_chapter <= len(opening["chapters"]):
                allowed_numbers.add(next_chapter)
            if not required_numbers.issubset(actual_numbers) or not actual_numbers.issubset(
                allowed_numbers
            ):
                return False
            for number in sorted(actual_numbers):
                validate_chapter(
                    (chapter_dir / f"{number:02d}.md").read_text(encoding="utf-8-sig"),
                    opening["chapters"][number - 1],
                )
            if stage in {"publish", "completed"} and actual_numbers != set(
                range(1, len(opening["chapters"]) + 1)
            ):
                return False

        accepted_artifacts: list[tuple[str, Any]] = []
        if book is not None:
            accepted_artifacts.append(("book", book))
        if macro is not None:
            accepted_artifacts.append(("macro_outline", macro))
        if opening is not None:
            accepted_artifacts.append(("opening_outline", opening))
            chapter_dir = accepted / "chapters"
            for number in sorted(actual_numbers):
                accepted_artifacts.append(
                    (
                        "chapter",
                        (
                            opening["chapters"][number - 1],
                            (chapter_dir / f"{number:02d}.md").read_text(
                                encoding="utf-8-sig"
                            ),
                        ),
                    )
                )

        state_artifact_count = min(stage_order[stage], 3) + completed_count
        actual_artifact_count = len(accepted_artifacts)
        if actual_artifact_count not in {
            state_artifact_count,
            state_artifact_count + 1,
        }:
            return False
        committed_exchange_count = (len(messages) - 1) // 2
        allowed_exchange_counts = {actual_artifact_count}
        if actual_artifact_count == state_artifact_count + 1:
            # The last accepted file may have landed just before its canonical
            # exchange; stage reconciliation will append that single turn.
            allowed_exchange_counts.add(actual_artifact_count - 1)
        if committed_exchange_count not in allowed_exchange_counts:
            return False

        for index, (artifact_stage, artifact_value) in enumerate(
            accepted_artifacts[:committed_exchange_count]
        ):
            assistant_text = str(messages[2 * index + 2]["content"])
            if artifact_stage == "book":
                if validate_book(parse_json_object(assistant_text)) != artifact_value:
                    return False
            elif artifact_stage == "macro_outline":
                if validate_macro_outline(parse_json_object(assistant_text)) != artifact_value:
                    return False
            elif artifact_stage == "opening_outline":
                if validate_opening_outline(parse_json_object(assistant_text)) != artifact_value:
                    return False
            else:
                chapter, chapter_text = artifact_value
                if (
                    validate_chapter(assistant_text, chapter)
                    != normalize_chapter(chapter_text)
                ):
                    return False
        usage_records = read_usage_records(work_dir)
        expansion_counts: dict[int, int] = {}
        for record in usage_records:
            if record.get("stage") != "chapter_expansion":
                continue
            number = record.get("chapter")
            if isinstance(number, bool) or not isinstance(number, int):
                return False
            expansion_counts[number] = expansion_counts.get(number, 0) + 1
        if any(
            count > MAX_CHAPTER_EXPANSION_CALLS
            for count in expansion_counts.values()
        ):
            return False
        return True
    except Exception:
        return False


def resumable_other_run_ids(model_work_root: Path, current_run_id: str) -> list[str]:
    if not model_work_root.is_dir():
        return []
    run_ids: list[str] = []
    for path in sorted(model_work_root.iterdir(), key=lambda item: item.name):
        if not path.is_dir() or path.name == current_run_id:
            continue
        if work_checkpoint_is_resumable(path, path.name):
            run_ids.append(path.name)
    return run_ids


def cleanup_completed_publish_debris(result_dir: Path, expected_run_id: str) -> None:
    """Remove recoverable staging/backup leftovers only after a valid commit exists."""

    if not result_is_complete(result_dir, expected_run_id):
        return
    patterns = (
        f".{result_dir.name}.backup-*",
        f".{result_dir.name}.publish-*",
    )
    for pattern in patterns:
        for path in result_dir.parent.glob(pattern):
            if path.is_dir() and path.parent.resolve() == result_dir.parent.resolve():
                shutil.rmtree(path)


_WORK_LOCK_REGISTRY_GUARD = threading.Lock()
_HELD_WORK_LOCKS: set[str] = set()


class WorkDirLock:
    """Cross-platform non-blocking lock for all runs of one model."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None
        self.registry_key: str | None = None

    def __enter__(self) -> "WorkDirLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_key = str(self.path.resolve()).casefold()
        with _WORK_LOCK_REGISTRY_GUARD:
            if self.registry_key in _HELD_WORK_LOCKS:
                raise RuntimeError(
                    f"当前 run 已被另一个生成进程占用：{self.path.parent}"
                )
            _HELD_WORK_LOCKS.add(self.registry_key)
        try:
            self.handle = self.path.open("a+b")
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(b"\0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - exercised by Linux CI
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if self.handle is not None:
                self.handle.close()
            self.handle = None
            with _WORK_LOCK_REGISTRY_GUARD:
                _HELD_WORK_LOCKS.discard(self.registry_key)
            self.registry_key = None
            raise RuntimeError(f"当前 run 已被另一个生成进程占用：{self.path.parent}") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised by Linux CI
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None
            with _WORK_LOCK_REGISTRY_GUARD:
                if self.registry_key is not None:
                    _HELD_WORK_LOCKS.discard(self.registry_key)
            self.registry_key = None


class GenerationRun:
    def __init__(
        self,
        *,
        root: Path,
        benchmark: str,
        direction: str,
        prompts: dict[str, str],
        model_cfg: dict[str, Any],
        client: ChatClient,
        new_run: bool,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = root
        self.benchmark = benchmark
        self.direction = canonical_text(direction)
        self.prompts = {name: canonical_text(value) for name, value in prompts.items()}
        self.model_cfg = model_cfg
        self.model_id = str(model_cfg["id"])
        self.client = client
        self.sleep_fn = sleep_fn
        self.result_dir = root / "results" / benchmark / self.model_id
        self.model_work_root = root / "work" / "v2.1" / benchmark / self.model_id
        self.run_input = build_run_input(
            benchmark, self.direction, self.prompts, model_cfg
        )
        self.run_input_sha256 = sha256_text(canonical_json(self.run_input))
        self.policy_sha256 = protocol_policy_sha256()
        self.code_sha256_at_start = calculate_code_hash(model_cfg)
        self.run_id = self.run_input_sha256[:12]
        self.work_dir = self.model_work_root / self.run_id
        self.accepted_dir = self.work_dir / "accepted"
        self.state_path = self.work_dir / "state.json"
        self.session_path = self.work_dir / "session.json"
        self.usage_path = self.work_dir / "usage.jsonl"
        self.usage_events_dir = self.work_dir / "usage-events"
        self.raw_dir = self.work_dir / "raw"
        self.failures_dir = self.work_dir / "failures"
        self.new_run = new_run
        self.state: dict[str, Any] = {}
        self.session: dict[str, Any] = {}

    def _initialize_work(self) -> None:
        """Initialize or repair the hashed work directory while model lock is held."""

        # --new-run authorizes a new hash to supersede stale public output.  A
        # matching, valid checkpoint is precious and must remain resumable if
        # the first attempt was interrupted.  Only an unusable directory is
        # cleared when the user explicitly supplied the flag.
        if (
            self.new_run
            and self.work_dir.exists()
            and not work_checkpoint_is_resumable(
                self.work_dir,
                self.run_id,
                run_input_sha256=self.run_input_sha256,
                policy_sha256=self.policy_sha256,
                code_sha256=self.code_sha256_at_start,
            )
        ):
            shutil.rmtree(self.work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.accepted_dir.mkdir(exist_ok=True)
        self.raw_dir.mkdir(exist_ok=True)
        self.failures_dir.mkdir(exist_ok=True)
        self.usage_events_dir.mkdir(exist_ok=True)
        self.state = self._load_or_create_state()
        self.session = self._load_or_create_session()

    def _load_or_create_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            state = read_json(self.state_path)
            if (
                state.get("schema") != PROTOCOL_VERSION
                or state.get("run_id") != self.run_id
                or state.get("run_input_sha256") != self.run_input_sha256
                or state.get("protocol_policy_sha256") != self.policy_sha256
                or state.get("code_sha256_at_start") != self.code_sha256_at_start
            ):
                raise RuntimeError("state 与当前 V2.1 run 身份不匹配")
            return state
        state = {
            "schema": PROTOCOL_VERSION,
            "run_id": self.run_id,
            "run_input_sha256": self.run_input_sha256,
            "protocol_policy_sha256": self.policy_sha256,
            "code_sha256_at_start": self.code_sha256_at_start,
            "run_origin": "fresh",
            "benchmark": self.benchmark,
            "model_id": self.model_id,
            "stage": "book",
            "next_chapter": 1,
            "completed_chapters": [],
            "attempts": {},
            "chapter_expansions": {},
            "last_error": None,
            "started_at": utc_now(),
            "updated_at": utc_now(),
        }
        atomic_write_json(self.state_path, state)
        return state

    def _load_or_create_session(self) -> dict[str, Any]:
        if self.session_path.exists():
            session = read_json(self.session_path)
            if (
                session.get("schema") != PROTOCOL_VERSION
                or session.get("run_id") != self.run_id
                or session.get("run_input_sha256") != self.run_input_sha256
                or session.get("protocol_policy_sha256") != self.policy_sha256
                or session.get("code_sha256_at_start") != self.code_sha256_at_start
                or not isinstance(session.get("messages"), list)
            ):
                raise RuntimeError("session 与当前 V2.1 run 身份不匹配")
            return session
        session = {
            "schema": PROTOCOL_VERSION,
            "run_id": self.run_id,
            "run_input_sha256": self.run_input_sha256,
            "protocol_policy_sha256": self.policy_sha256,
            "code_sha256_at_start": self.code_sha256_at_start,
            "run_origin": "fresh",
            "benchmark": self.benchmark,
            "model_id": self.model_id,
            "messages": [{"role": "system", "content": self.prompts["system.md"]}],
        }
        atomic_write_json(self.session_path, session)
        return session

    def _save_state(self) -> None:
        self.state["updated_at"] = utc_now()
        atomic_write_json(self.state_path, self.state)

    def _save_session(self) -> None:
        atomic_write_json(self.session_path, self.session)

    def _write_usage_journal(self, records: list[dict[str, Any]]) -> None:
        text = "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        )
        atomic_write_text(self.usage_path, text)

    def _usage_records(self) -> list[dict[str, Any]]:
        records = read_usage_records(self.work_dir)
        if records:
            for index, record in enumerate(records, start=1):
                event_path = self.usage_events_dir / f"{index:06d}.json"
                if event_path.exists():
                    if read_json(event_path) != record:
                        raise RuntimeError(f"usage event {index} 与 journal 不一致")
                    continue
                atomic_write_json(
                    event_path, record
                )
        self._write_usage_journal(records)
        return records

    def _matching_usage_record(
        self,
        *,
        stage: str,
        chapter: int | None,
        attempt: int,
        messages: list[dict[str, str]],
        records: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Find one already-journaled response for this exact pending request."""

        prompt_sha256 = sha256_text(canonical_json(messages))
        matches = [
            record
            for record in (self._usage_records() if records is None else records)
            if record.get("stage") == stage
            and record.get("chapter") == chapter
            and record.get("attempt") == attempt
            and isinstance(record.get("context_audit"), dict)
            and record["context_audit"].get("prompt_message_count") == len(messages)
            and record["context_audit"].get("prompt_sha256") == prompt_sha256
        ]
        if len(matches) > 1:
            raise RuntimeError(
                f"{stage} 已存在多条相同请求的 usage 记录，拒绝重复或猜测恢复"
            )
        return matches[0] if matches else None

    def _append_usage_record(self, record: dict[str, Any]) -> None:
        records = self._usage_records()
        index = len(records) + 1
        enriched = {
            **record,
            "event_index": index,
            "schema": PROTOCOL_VERSION,
            "run_id": self.run_id,
            "run_input_sha256": self.run_input_sha256,
            "protocol_policy_sha256": self.policy_sha256,
        }
        atomic_write_json(self.usage_events_dir / f"{index:06d}.json", enriched)
        records.append(enriched)
        self._write_usage_journal(records)

    def _append_usage(
        self,
        stage: str,
        result: ChatResult,
        attempt: int,
        chapter: int | None,
        context_audit: dict[str, Any],
    ) -> None:
        context_audit = {
            **context_audit,
            "assistant_content_sha256": sha256_text(result.content),
        }
        record = {
            "ts": utc_now(),
            "stage": stage,
            "chapter": chapter,
            "attempt": attempt,
            "context_audit": context_audit,
            **_chat_result_dict(result),
        }
        self._append_usage_record(record)

    def _append_failed_usage(
        self,
        stage: str,
        error: LLMAPIError,
        attempt: int,
        chapter: int | None,
        context_audit: dict[str, Any],
    ) -> None:
        raw = error.raw_response or {}
        choices = raw.get("choices") if isinstance(raw.get("choices"), list) else []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        native_stop_reason = raw.get("stop_reason")
        if native_stop_reason is not None:
            usage = dict(usage)

            def token_count(name: str) -> int:
                value = usage.get(name)
                if isinstance(value, bool) or not isinstance(value, int):
                    return 0
                return max(0, value)

            prompt_tokens = sum(
                token_count(name)
                for name in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            )
            completion_tokens = token_count("output_tokens")
            usage.update(
                {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                }
            )
        finish_reason = first.get("finish_reason")
        if finish_reason is None and native_stop_reason is not None:
            finish_reason = {
                "end_turn": "stop",
                "stop_sequence": "stop",
                "max_tokens": "length",
            }.get(str(native_stop_reason), str(native_stop_reason))
        record = {
            "ts": utc_now(),
            "stage": stage,
            "chapter": chapter,
            "attempt": attempt,
            "status": "api_error",
            "error": str(error),
            "status_code": error.status_code,
            "retry_after_seconds": error.retry_after_seconds,
            "context_audit": context_audit,
            "usage": usage,
            "requested_model": self.model_cfg.get("model"),
            "response_model": raw.get("model"),
            "response_id": raw.get("id"),
            "finish_reason": finish_reason,
            "native_finish_reason": native_stop_reason,
        }
        self._append_usage_record(record)

    def _call(
        self,
        user_prompt: str,
        *,
        stage: str,
        attempt: int,
        chapter: int | None = None,
        history: list[dict[str, str]] | None = None,
        persist: bool = True,
    ) -> str:
        base_messages = self.session["messages"] if history is None else history
        messages = [*base_messages, {"role": "user", "content": user_prompt}]
        context_window = int(self.model_cfg.get("context_window", 131_072))
        api_optional_parameters = generation_request_parameters(self.model_cfg, stage)
        wire_protocol = model_protocol(self.model_cfg)
        required_parameters = protocol_required_parameters(self.model_cfg)
        if api_optional_parameters:
            raise RuntimeError(
                "生成请求必须使用服务端默认参数，禁止显式 API 控制字段："
                + ", ".join(sorted(api_optional_parameters))
            )
        replay = self._matching_usage_record(
            stage=stage,
            chapter=chapter,
            attempt=attempt,
            messages=messages,
        )
        if replay is not None:
            if replay.get("status") == "api_error":
                status_code = replay.get("status_code")
                retry_after = replay.get("retry_after_seconds")
                raise LLMAPIError(
                    str(replay.get("error") or "已落盘的 API 请求失败"),
                    status_code=(
                        int(status_code)
                        if isinstance(status_code, int) and not isinstance(status_code, bool)
                        else None
                    ),
                    retry_after_seconds=(
                        float(retry_after)
                        if isinstance(retry_after, (int, float))
                        and not isinstance(retry_after, bool)
                        else None
                    ),
                )
            replay_content = replay.get("content")
            if not isinstance(replay_content, str):
                raise RuntimeError(f"{stage} 的已落盘响应缺少正文，无法安全恢复")
            if persist:
                self._ensure_committed_exchange(user_prompt, replay_content)
            finish_reason = str(replay.get("finish_reason") or "").strip().lower()
            if finish_reason != "stop":
                raise IncompleteCompletionError(
                    replay.get("finish_reason"), replay_content
                )
            log(
                f"{self.model_id} {stage}"
                + (f" 第 {chapter} 章" if chapter is not None else "")
                + " 从原子 usage 记录恢复，未重复请求 API"
            )
            return replay_content
        prompt_estimate, estimate_source, anchor_event_index = (
            estimate_prompt_tokens_with_audit(messages, self._usage_records())
        )
        estimate = prompt_estimate
        safe_limit = context_window * CONTEXT_SAFETY_BASIS_POINTS // 10_000
        context_audit = {
            "prompt_message_count": len(messages),
            "prompt_sha256": sha256_text(canonical_json(messages)),
            "estimate_source": estimate_source,
            "anchor_event_index": anchor_event_index,
            "context_window": context_window,
            "safety_basis_points": CONTEXT_SAFETY_BASIS_POINTS,
            "safe_limit_tokens": safe_limit,
            "prompt_estimate_tokens": prompt_estimate,
            "usage_margin_tokens": CONTEXT_USAGE_MARGIN_TOKENS,
            "api_optional_parameters": [],
            "wire_protocol": wire_protocol,
            "endpoint_path": protocol_endpoint_path(wire_protocol),
            "protocol_required_parameters": sorted(required_parameters),
            "configured_max_tokens": required_parameters.get("max_tokens"),
            "max_tokens_sent": "max_tokens" in required_parameters,
            "output_reserve_tokens": 0,
            "reserved_total_tokens": estimate,
            "headroom_tokens": safe_limit - estimate,
        }
        if estimate > safe_limit:
            raise RuntimeError(
                f"上下文预算超限：估算 {estimate} > {safe_limit}（85% 安全线）"
            )
        try:
            result = self.client.complete(self.model_cfg, messages, stage=stage)
        except LLMAPIError as exc:
            raw_index = len(list(self.raw_dir.glob("*.json"))) + 1
            if exc.raw_response is not None:
                atomic_write_json(
                    self.raw_dir / f"{raw_index:04d}_{stage}_error.json",
                    exc.raw_response,
                )
            self._append_failed_usage(stage, exc, attempt, chapter, context_audit)
            raise
        raw_index = len(list(self.raw_dir.glob("*.json"))) + 1
        raw_payload = getattr(result, "raw_response", None)
        if raw_payload is not None:
            atomic_write_json(self.raw_dir / f"{raw_index:04d}_{stage}.json", raw_payload)
        self._append_usage(stage, result, attempt, chapter, context_audit)
        if persist:
            self._commit_exchange(user_prompt, result.content)
        finish_reason = (
            str(getattr(result, "finish_reason", "") or "").strip().lower()
        )
        if finish_reason != "stop":
            raise IncompleteCompletionError(
                getattr(result, "finish_reason", None), result.content
            )
        return result.content

    def _commit_exchange(self, user_prompt: str, assistant_content: str) -> None:
        """Append one accepted exchange to the canonical replay transcript."""

        self.session["messages"].append({"role": "user", "content": user_prompt})
        self.session["messages"].append(
            {"role": "assistant", "content": assistant_content}
        )
        self._save_session()

    def _ensure_committed_exchange(
        self, user_prompt: str, assistant_content: str
    ) -> None:
        """Reconcile an accepted artifact committed just before session/state."""

        messages = self.session["messages"]
        if (
            len(messages) >= 2
            and messages[-2].get("role") == "user"
            and messages[-2].get("content") == user_prompt
            and messages[-1].get("role") == "assistant"
        ):
            return
        self._commit_exchange(user_prompt, assistant_content)

    def _attempt_count(self, key: str) -> int:
        return int((self.state.get("attempts") or {}).get(key, 0))

    def _mark_attempt(self, key: str, error: str | None) -> int:
        attempts = self.state.setdefault("attempts", {})
        attempts[key] = int(attempts.get(key, 0)) + 1
        self.state["last_error"] = error
        self._save_state()
        return attempts[key]

    def _mark_api_attempt(self, key: str) -> int:
        """Count a failed API call without changing the pending repair prompt."""

        attempts = self.state.setdefault("attempts", {})
        attempts[key] = int(attempts.get(key, 0)) + 1
        self._save_state()
        return attempts[key]

    def _record_validation_failure(
        self,
        *,
        stage: str,
        attempt: int,
        error: Exception,
        response: str,
        chapter: int | None = None,
    ) -> None:
        """Persist every rejected response in ignored local audit data."""
        label = self._failure_label(stage, chapter)
        path = self.failures_dir / f"{label}_attempt_{attempt:02d}.json"
        atomic_write_json(path, {
            "schema": PROTOCOL_VERSION,
            "ts": utc_now(),
            "stage": stage,
            "chapter": chapter,
            "attempt": attempt,
            "error_type": type(error).__name__,
            "error": str(error),
            "response_sha256": sha256_text(response),
            "response": response,
        })

    def _failure_records(
        self, *, stage: str, chapter: int | None = None
    ) -> list[dict[str, Any]]:
        """Load private rejected responses that can seed an isolated repair."""

        label = self._failure_label(stage, chapter)
        records: list[dict[str, Any]] = []
        for path in sorted(self.failures_dir.glob(f"{label}_attempt_*.json")):
            try:
                record = read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            response = record.get("response")
            if (
                record.get("schema") == PROTOCOL_VERSION
                and record.get("stage") == stage
                and isinstance(response, str)
                and response.strip()
            ):
                records.append(record)
        return records

    @staticmethod
    def _failure_label(stage: str, chapter: int | None) -> str:
        if chapter is None:
            return stage
        if stage == "chapter":
            return f"chapter_{chapter:02d}"
        return f"{stage}_{chapter:02d}"

    def _latest_json_repair_candidate(
        self, stage: str
    ) -> dict[str, Any] | None:
        records = self._failure_records(stage=stage)
        return records[-1] if records else None

    def _latest_chapter_repair_candidate(
        self, chapter: int
    ) -> dict[str, Any] | None:
        candidates = [
            record
            for record in self._failure_records(stage="chapter", chapter=chapter)
            if record.get("error_type") != "IncompleteCompletionError"
        ]
        return candidates[-1] if candidates else None

    def _expand_short_chapter(
        self,
        *,
        chapter: dict[str, Any],
        original_prompt: str,
        source_response: str,
        source_clean: str,
        base_history: list[dict[str, str]],
    ) -> tuple[str, dict[str, Any]]:
        """Make one best-effort expansion call and return the longer valid draft."""

        number = int(chapter["number"])
        source_chars = count_content_chars(source_clean)
        audit: dict[str, Any] = {
            "requested": False,
            "threshold_chars": CHAPTER_EXPANSION_TRIGGER_CHARS,
            "initial_chars": source_chars,
            "result_chars": None,
            "adopted": False,
            "outcome": "not_needed",
        }
        if source_chars >= CHAPTER_EXPANSION_TRIGGER_CHARS:
            return source_clean, audit

        audit["requested"] = True
        expansion_prompt = self.prompts["expand_chapter.md"].format(
            chapter_number=number,
            chapter_title=chapter["title"],
            current_chars=source_chars,
        )
        expansion_history = [
            *base_history,
            {"role": "user", "content": original_prompt},
            {"role": "assistant", "content": source_response},
        ]
        expansion_messages = [
            *expansion_history,
            {"role": "user", "content": expansion_prompt},
        ]
        usage_records = self._usage_records()
        prior_expansion_records = [
            record
            for record in usage_records
            if record.get("stage") == "chapter_expansion"
            and record.get("chapter") == number
        ]
        if len(prior_expansion_records) > MAX_CHAPTER_EXPANSION_CALLS:
            raise RuntimeError(
                f"第 {number} 章已有 {len(prior_expansion_records)} 条扩写 usage，"
                "超过每章一次的协议上限"
            )
        matching_expansion_record = self._matching_usage_record(
            stage="chapter_expansion",
            chapter=number,
            attempt=1,
            messages=expansion_messages,
            records=usage_records,
        )
        if prior_expansion_records and matching_expansion_record is None:
            raise RuntimeError(
                f"第 {number} 章已有无法与当前首稿匹配的扩写 usage；"
                "为避免第二次扩写，停止恢复"
            )
        try:
            expanded_response = self._call(
                expansion_prompt,
                stage="chapter_expansion",
                attempt=1,
                chapter=number,
                history=expansion_history,
                persist=False,
            )
        except IncompleteCompletionError as exc:
            self._record_validation_failure(
                stage="chapter_expansion",
                chapter=number,
                attempt=1,
                error=exc,
                response=exc.content,
            )
            audit["outcome"] = "kept_source_incomplete"
            warn(
                f"{self.model_id} 第 {number} 章扩写未完成，保留原稿：{exc}"
            )
            return source_clean, audit
        except LLMAPIError as exc:
            self._record_validation_failure(
                stage="chapter_expansion",
                chapter=number,
                attempt=1,
                error=exc,
                response="",
            )
            audit["outcome"] = "kept_source_api_error"
            warn(
                f"{self.model_id} 第 {number} 章扩写 API 响应不可用，保留原稿：{exc}"
            )
            return source_clean, audit

        try:
            expanded_clean = validate_chapter(expanded_response, chapter)
        except Exception as exc:
            self._record_validation_failure(
                stage="chapter_expansion",
                chapter=number,
                attempt=1,
                error=exc,
                response=expanded_response,
            )
            audit["result_chars"] = count_content_chars(expanded_response)
            audit["outcome"] = "kept_source_invalid"
            warn(
                f"{self.model_id} 第 {number} 章扩写未通过结构校验，保留原稿：{exc}"
            )
            return source_clean, audit

        expanded_chars = count_content_chars(expanded_clean)
        audit["result_chars"] = expanded_chars
        if expanded_chars > source_chars:
            audit["adopted"] = True
            audit["outcome"] = "adopted"
            return expanded_clean, audit
        audit["outcome"] = "kept_source_not_longer"
        return source_clean, audit

    def _reconcile_chapter_expansion_audit(
        self, number: int, final_text: str
    ) -> dict[str, Any]:
        """Rebuild expansion metadata if an artifact landed before state did."""

        records = self._usage_records()
        expansion_records = [
            record
            for record in records
            if record.get("stage") == "chapter_expansion"
            and record.get("chapter") == number
        ]
        if not expansion_records:
            chars = count_content_chars(final_text)
            return {
                "requested": False,
                "threshold_chars": CHAPTER_EXPANSION_TRIGGER_CHARS,
                "initial_chars": chars,
                "result_chars": None,
                "adopted": False,
                "outcome": "not_needed",
            }
        source_records = [
            record
            for record in records
            if record.get("stage") == "chapter"
            and record.get("chapter") == number
            and isinstance(record.get("content"), str)
        ]
        source_chars = (
            count_content_chars(str(source_records[-1]["content"]))
            if source_records
            else count_content_chars(final_text)
        )
        result_content = expansion_records[-1].get("content")
        result_chars = (
            count_content_chars(str(result_content))
            if isinstance(result_content, str)
            else None
        )
        adopted = (
            isinstance(result_content, str)
            and sha256_text(canonical_text(result_content))
            == sha256_text(canonical_text(final_text))
        )
        return {
            "requested": True,
            "threshold_chars": CHAPTER_EXPANSION_TRIGGER_CHARS,
            "initial_chars": source_chars,
            "result_chars": result_chars,
            "adopted": adopted,
            "outcome": "adopted" if adopted else "reconciled_keep_source",
        }

    def run_json_stage(
        self,
        *,
        stage: str,
        prompt_name: str,
        artifact_name: str,
        validator: Callable[[dict[str, Any]], dict[str, Any]],
        next_stage: str,
    ) -> dict[str, Any]:
        artifact = self.accepted_dir / artifact_name
        original_prompt = self.prompts[prompt_name].format(direction=self.direction)
        # Reconcile the narrow crash window in which the validated artifact was
        # atomically committed but state.json had not yet advanced.
        if self.state.get("stage") == stage and artifact.exists():
            value = validator(read_json(artifact))
            self._ensure_committed_exchange(original_prompt, canonical_json(value))
            self.state["stage"] = next_stage
            self.state["last_error"] = None
            self._save_state()
            return value
        if self.state.get("stage") != stage:
            if not artifact.exists():
                raise RuntimeError(f"状态已越过 {stage}，但缺少 {artifact}")
            return validator(read_json(artifact))
        key = stage
        base_history = list(self.session["messages"])
        repair_candidate = self._latest_json_repair_candidate(stage)
        attempts_this_execution = 0
        while attempts_this_execution < MAX_STAGE_ATTEMPTS:
            if repair_candidate is not None:
                prompt = self.prompts["repair_json.md"].format(
                    stage=stage,
                    error=str(repair_candidate.get("error") or "响应未通过校验"),
                )
                history = [
                    *base_history,
                    {"role": "user", "content": original_prompt},
                    {
                        "role": "assistant",
                        "content": str(repair_candidate["response"]),
                    },
                ]
            else:
                prompt = original_prompt
                history = base_history
            attempt = self._attempt_count(key) + 1
            attempts_this_execution += 1
            try:
                text = self._call(
                    prompt,
                    stage=stage,
                    attempt=attempt,
                    history=history,
                    persist=False,
                )
            except IncompleteCompletionError as exc:
                self._record_validation_failure(
                    stage=stage,
                    attempt=attempt,
                    error=exc,
                    response=exc.content,
                )
                self._mark_attempt(key, str(exc))
                repair_candidate = self._latest_json_repair_candidate(stage)
                warn(f"{self.model_id} {stage} 第 {attempt} 次未完成：{exc}")
                continue
            except LLMAPIError as exc:
                self._record_validation_failure(
                    stage=stage,
                    attempt=attempt,
                    error=exc,
                    response="",
                )
                self._mark_api_attempt(key)
                if not api_error_is_retryable(exc):
                    raise
                delay = retry_delay_seconds(exc, attempts_this_execution)
                if attempts_this_execution < MAX_STAGE_ATTEMPTS:
                    warn(
                        f"{self.model_id} {stage} 第 {attempt} 次 API 响应不可用：{exc}；"
                        f"{delay:g} 秒后重试"
                    )
                    self.sleep_fn(delay)
                else:
                    warn(
                        f"{self.model_id} {stage} 第 {attempt} 次 API 响应不可用：{exc}；"
                        "本次运行重试额度已用尽"
                    )
                continue
            try:
                value = validator(parse_json_object(text))
            except Exception as exc:
                self._record_validation_failure(
                    stage=stage,
                    attempt=attempt,
                    error=exc,
                    response=text,
                )
                self._mark_attempt(key, str(exc))
                repair_candidate = self._latest_json_repair_candidate(stage)
                warn(f"{self.model_id} {stage} 第 {attempt} 次未通过：{exc}")
                continue
            atomic_write_json(artifact, value)
            self._commit_exchange(original_prompt, text)
            self._mark_attempt(key, None)
            self.state["stage"] = next_stage
            self.state["last_error"] = None
            self._save_state()
            log(f"{self.model_id} {stage} 完成")
            return value
        raise RuntimeError(f"{stage} 本次运行连续 {MAX_STAGE_ATTEMPTS} 次未通过")

    def run_chapters(
        self,
        opening: dict[str, Any],
        *,
        stop_after_chapter: int | None = None,
    ) -> tuple[list[str], bool]:
        chapter_dir = self.accepted_dir / "chapters"
        chapter_dir.mkdir(parents=True, exist_ok=True)
        accepted: list[str] = []
        for chapter in opening["chapters"]:
            number = int(chapter["number"])
            path = chapter_dir / f"{number:02d}.md"
            target_chars = int(chapter["target_chars"])
            if number < int(self.state.get("next_chapter", 1)):
                if not path.exists():
                    raise RuntimeError(f"状态显示第 {number} 章已完成，但文件缺失")
                accepted.append(
                    validate_chapter(path.read_text(encoding="utf-8"), chapter)
                )
                if stop_after_chapter == number:
                    return accepted, True
                continue
            key = f"chapter_{number:02d}"
            original_prompt = self.prompts["chapter.md"].format(
                chapter_number=number,
                chapter_title=chapter["title"],
                chapter_summary=chapter["summary"],
                chapter_beats=json.dumps(chapter["beats"], ensure_ascii=False),
                continuity_in=json.dumps(chapter["continuity_in"], ensure_ascii=False),
                continuity_out=json.dumps(chapter["continuity_out"], ensure_ascii=False),
                foreshadowing=json.dumps(chapter["foreshadowing"], ensure_ascii=False),
                target_chars=target_chars,
            )
            # Reconcile an accepted chapter file committed just before a crash
            # that prevented the corresponding state update.
            if path.exists():
                clean = validate_chapter(path.read_text(encoding="utf-8"), chapter)
                self._ensure_committed_exchange(original_prompt, clean)
                accepted.append(clean)
                expansion_audits = self.state.setdefault("chapter_expansions", {})
                expansion_key = f"{number:02d}"
                if expansion_key not in expansion_audits:
                    expansion_audits[expansion_key] = (
                        self._reconcile_chapter_expansion_audit(number, clean)
                    )
                completed = self.state.setdefault("completed_chapters", [])
                if number not in completed:
                    completed.append(number)
                self.state["next_chapter"] = number + 1
                self.state["stage"] = "chapters"
                self.state["last_error"] = None
                self.state.pop("last_response_chars", None)
                self._save_state()
                if stop_after_chapter == number:
                    return accepted, True
                continue
            base_history = list(self.session["messages"])
            repair_candidate = self._latest_chapter_repair_candidate(number)
            attempts_this_execution = 0
            while attempts_this_execution < MAX_STAGE_ATTEMPTS:
                if repair_candidate is not None:
                    candidate_text = str(repair_candidate["response"])
                    prior_error = str(
                        repair_candidate.get("error") or "章节未通过校验"
                    )
                    prompt = self.prompts["repair_chapter.md"].format(
                        chapter_number=number,
                        error=prior_error,
                    )
                    history = [
                        *base_history,
                        {"role": "user", "content": original_prompt},
                        {"role": "assistant", "content": candidate_text},
                    ]
                else:
                    prompt = original_prompt
                    history = base_history
                attempt = self._attempt_count(key) + 1
                attempts_this_execution += 1
                try:
                    text = self._call(
                        prompt,
                        stage="chapter",
                        attempt=attempt,
                        chapter=number,
                        history=history,
                        persist=False,
                    )
                except IncompleteCompletionError as exc:
                    self._record_validation_failure(
                        stage="chapter",
                        chapter=number,
                        attempt=attempt,
                        error=exc,
                        response=exc.content,
                    )
                    self.state["last_response_chars"] = count_content_chars(exc.content)
                    self._mark_attempt(key, str(exc))
                    warn(
                        f"{self.model_id} 第 {number} 章第 {attempt} 次未完成：{exc}"
                    )
                    continue
                except LLMAPIError as exc:
                    self._record_validation_failure(
                        stage="chapter",
                        chapter=number,
                        attempt=attempt,
                        error=exc,
                        response="",
                    )
                    self._mark_api_attempt(key)
                    if not api_error_is_retryable(exc):
                        raise
                    delay = retry_delay_seconds(exc, attempts_this_execution)
                    if attempts_this_execution < MAX_STAGE_ATTEMPTS:
                        warn(
                            f"{self.model_id} 第 {number} 章第 {attempt} 次 API 响应不可用：{exc}；"
                            f"{delay:g} 秒后重试"
                        )
                        self.sleep_fn(delay)
                    else:
                        warn(
                            f"{self.model_id} 第 {number} 章第 {attempt} 次 API 响应不可用：{exc}；"
                            "本次运行重试额度已用尽"
                        )
                    continue
                try:
                    clean = validate_chapter(text, chapter)
                except Exception as exc:
                    self._record_validation_failure(
                        stage="chapter",
                        chapter=number,
                        attempt=attempt,
                        error=exc,
                        response=text,
                    )
                    self.state["last_response_chars"] = count_content_chars(text)
                    self._mark_attempt(key, str(exc))
                    repair_candidate = self._latest_chapter_repair_candidate(number)
                    warn(f"{self.model_id} 第 {number} 章第 {attempt} 次未通过：{exc}")
                    continue
                final_clean, expansion_audit = self._expand_short_chapter(
                    chapter=chapter,
                    original_prompt=original_prompt,
                    source_response=text,
                    source_clean=clean,
                    base_history=base_history,
                )
                atomic_write_text(path, final_clean + "\n")
                self._commit_exchange(original_prompt, final_clean)
                self.state.setdefault("chapter_expansions", {})[
                    f"{number:02d}"
                ] = expansion_audit
                self._mark_attempt(key, None)
                accepted.append(final_clean)
                completed = self.state.setdefault("completed_chapters", [])
                if number not in completed:
                    completed.append(number)
                self.state["next_chapter"] = number + 1
                self.state["stage"] = "chapters"
                self.state["last_error"] = None
                self.state.pop("last_response_chars", None)
                self._save_state()
                expansion_note = (
                    f"，扩写={expansion_audit['outcome']}"
                    if expansion_audit["requested"]
                    else ""
                )
                log(
                    f"{self.model_id} 第 {number} 章完成"
                    f"（{count_content_chars(final_clean)} 字{expansion_note}）"
                )
                if stop_after_chapter == number:
                    return accepted, True
                break
            else:
                raise RuntimeError(
                    f"第 {number} 章本次运行连续 {MAX_STAGE_ATTEMPTS} 次未通过"
                )
        self.state["stage"] = "publish"
        self._save_state()
        return accepted, False

    def publish(
        self,
        book: dict[str, Any],
        macro: dict[str, Any],
        opening: dict[str, Any],
        chapters: list[str],
    ) -> None:
        novel = f"# {book['title']}\n\n" + "\n\n".join(chapters).rstrip() + "\n"
        chars = sum(count_content_chars(chapter) for chapter in chapters)
        if chars < MIN_FINAL_CHARS:
            raise RuntimeError(f"最终正文 {chars} 字，少于最低完成线 {MIN_FINAL_CHARS}")
        if len(chapters) != len(opening["chapters"]):
            raise RuntimeError("正文章数与前段细纲不一致")
        staging_dir = self.result_dir.with_name(
            f".{self.result_dir.name}.publish-{self.run_id}"
        )
        backup_dir = self.result_dir.with_name(
            f".{self.result_dir.name}.backup-{self.run_id}"
        )
        if backup_dir.exists():
            if not self.result_dir.exists():
                os.replace(backup_dir, self.result_dir)
            else:
                shutil.rmtree(backup_dir)
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True)
        usage_records = self._usage_records()
        prompt_tokens = completion_tokens = total_tokens = 0
        response_models: set[str] = set()
        for record in usage_records:
            usage = record.get("usage") or {}
            prompt_tokens += int(usage.get("prompt_tokens") or 0)
            completion_tokens += int(usage.get("completion_tokens") or 0)
            total_tokens += int(usage.get("total_tokens") or 0)
            if record.get("response_model"):
                response_models.add(str(record["response_model"]))
        # Atomic usage-events are committed immediately after every API call;
        # usage.jsonl is a rebuilt audit view. This survives a process death
        # between an accepted artifact commit and the state update.
        usage_attempts: dict[str, int] = {}
        for record in usage_records:
            chapter_number = record.get("chapter")
            stage = str(record.get("stage") or "")
            if chapter_number is None:
                key = stage
            elif stage == "chapter":
                key = f"chapter_{int(chapter_number):02d}"
            else:
                key = f"{stage}_{int(chapter_number):02d}"
            if key:
                usage_attempts[key] = usage_attempts.get(key, 0) + 1
        attempts = {
            str(key): max(int(value), usage_attempts.get(str(key), 0))
            for key, value in (self.state.get("attempts") or {}).items()
        }
        for key, count in usage_attempts.items():
            attempts[key] = max(attempts.get(key, 0), count)
        retry_count = sum(max(0, count - 1) for count in attempts.values())
        context_audits = [
            record["context_audit"]
            for record in usage_records
            if isinstance(record.get("context_audit"), dict)
        ]
        context_sources = {
            source: sum(1 for audit in context_audits if audit.get("estimate_source") == source)
            for source in ("fallback", "provider_usage_anchor")
        }
        context_summary = {
            "calls": len(context_audits),
            "estimate_sources": context_sources,
            "max_prompt_estimate_tokens": max(
                (int(audit.get("prompt_estimate_tokens") or 0) for audit in context_audits),
                default=0,
            ),
            "max_reserved_total_tokens": max(
                (int(audit.get("reserved_total_tokens") or 0) for audit in context_audits),
                default=0,
            ),
            "min_headroom_tokens": min(
                (int(audit.get("headroom_tokens") or 0) for audit in context_audits),
                default=0,
            ),
        }

        # Public files are written only after the complete manuscript validates.
        atomic_write_json(staging_dir / "book.json", book)
        atomic_write_json(staging_dir / "macro_outline.json", macro)
        atomic_write_json(staging_dir / "opening_outline.json", opening)
        public_chapters = staging_dir / "chapters"
        public_chapters.mkdir(parents=True, exist_ok=True)
        for chapter, text in zip(opening["chapters"], chapters):
            atomic_write_text(public_chapters / f"{int(chapter['number']):02d}.md", text + "\n")
        atomic_write_text(staging_dir / "novel.md", novel)

        artifact_paths = [
            "book.json",
            "macro_outline.json",
            "opening_outline.json",
            "novel.md",
            *[
                f"chapters/{int(chapter['number']):02d}.md"
                for chapter in opening["chapters"]
            ],
        ]
        artifact_sha256 = {
            name: sha256_normalized_text_file(staging_dir / Path(name))
            for name in artifact_paths
        }
        completed_at = utc_now()
        chapter_manifest: list[dict[str, Any]] = []
        expansion_audits = self.state.get("chapter_expansions") or {}
        for chapter, text in zip(opening["chapters"], chapters):
            number = int(chapter["number"])
            expansion = expansion_audits.get(f"{number:02d}")
            if not isinstance(expansion, dict):
                expansion = self._reconcile_chapter_expansion_audit(number, text)
            chapter_attempt_key = f"chapter_{number:02d}"
            expansion_attempt_key = f"chapter_expansion_{number:02d}"
            chapter_manifest.append({
                "number": chapter["number"],
                "title": chapter["title"],
                "chars": count_content_chars(text),
                "attempt_count": attempts.get(chapter_attempt_key, 0),
                "retry_count": max(0, attempts.get(chapter_attempt_key, 0) - 1),
                "initial_chars": int(expansion.get("initial_chars") or 0),
                "expansion_requested": bool(expansion.get("requested")),
                "expansion_attempt_count": attempts.get(expansion_attempt_key, 0),
                "expansion_result_chars": expansion.get("result_chars"),
                "expansion_adopted": bool(expansion.get("adopted")),
                "expansion_outcome": str(expansion.get("outcome") or "unknown"),
            })
        manifest = {
            "schema": PROTOCOL_VERSION,
            "benchmark": self.benchmark,
            "run_id": self.run_id,
            "run_input_sha256": self.run_input_sha256,
            "protocol_policy": PROTOCOL_POLICY,
            "protocol_policy_sha256": self.policy_sha256,
            "run_origin": "fresh",
            "model_id": self.model_id,
            "requested_model": self.model_cfg.get("model"),
            "response_models": sorted(response_models),
            "direction_sha256": sha256_text(self.direction),
            "prompts_sha256": sha256_text(canonical_json(self.prompts)),
            "model_config_sha256": sha256_text(canonical_json(self.model_cfg)),
            "code_sha256": self.code_sha256_at_start,
            "artifact_sha256": artifact_sha256,
            "parameters": _public_manifest_value({
                "request": self.model_cfg.get("request") or {},
                "stages": self.model_cfg.get("stages") or {},
                "provider_request_defaults": self.model_cfg.get(
                    PROVIDER_DEFAULTS_TRACKING_KEY
                )
                or {},
                "protocol": model_protocol(self.model_cfg),
                "protocol_required": protocol_required_parameters(self.model_cfg),
                "context_window": self.model_cfg.get("context_window"),
            }),
            "attempts": attempts,
            "retry_count": retry_count,
            "chapters": chapter_manifest,
            "body_chars": chars,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "calls": len(usage_records),
            },
            "context_audit": context_summary,
            "started_at": self.state.get("started_at"),
            "completed_at": completed_at,
            "status": "completed",
        }
        # The manifest is the commit marker and is intentionally written last.
        atomic_write_json(staging_dir / "manifest.json", manifest)
        if not result_is_complete(staging_dir, self.run_id):
            raise RuntimeError("发布 staging 深校验失败，未替换公开结果")

        had_previous = self.result_dir.exists()
        if had_previous:
            os.replace(self.result_dir, backup_dir)
        try:
            os.replace(staging_dir, self.result_dir)
        except Exception:
            if had_previous and backup_dir.exists() and not self.result_dir.exists():
                os.replace(backup_dir, self.result_dir)
            raise
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        self.state["stage"] = "completed"
        self.state["completed_at"] = manifest["completed_at"]
        self._save_state()
        log(f"{self.model_id} 发布完成：{chars} 字，{len(chapters)} 章")

    def execute(self, stop_after: str | None = None) -> bool:
        """Run or resume generation; return True only when publication completed."""
        with WorkDirLock(self.model_work_root / ".run.lock"):
            self._initialize_work()
            return self._execute_unlocked(stop_after)

    def _execute_unlocked(self, stop_after: str | None = None) -> bool:
        stop_kind, stop_chapter = parse_stop_after(stop_after)
        book = self.run_json_stage(
            stage="book", prompt_name="book.md", artifact_name="book.json",
            validator=validate_book, next_stage="macro_outline",
        )
        if stop_kind == "book":
            log(f"{self.model_id} 已按 --stop-after book 停在已提交检查点")
            return False
        macro = self.run_json_stage(
            stage="macro_outline", prompt_name="macro_outline.md", artifact_name="macro_outline.json",
            validator=validate_macro_outline, next_stage="opening_outline",
        )
        if stop_kind == "macro-outline":
            log(f"{self.model_id} 已按 --stop-after macro-outline 停在已提交检查点")
            return False
        opening = self.run_json_stage(
            stage="opening_outline", prompt_name="opening_outline.md", artifact_name="opening_outline.json",
            validator=validate_opening_outline, next_stage="chapters",
        )
        if stop_kind == "opening-outline":
            log(f"{self.model_id} 已按 --stop-after opening-outline 停在已提交检查点")
            return False
        if stop_chapter is not None and stop_chapter > len(opening["chapters"]):
            raise ValueError(
                f"--stop-after chapter:{stop_chapter} 超过细纲章数 {len(opening['chapters'])}"
            )
        chapters, stopped = self.run_chapters(
            opening,
            stop_after_chapter=stop_chapter,
        )
        if stopped:
            log(f"{self.model_id} 已按 --stop-after chapter:{stop_chapter} 停在已提交检查点")
            return False
        self.publish(book, macro, opening, chapters)
        return True


def parse_stop_after(value: str | None) -> tuple[str | None, int | None]:
    if value is None:
        return None, None
    normalized = value.strip().lower()
    if normalized in {"book", "macro-outline", "opening-outline"}:
        return normalized, None
    match = re.fullmatch(r"chapter:([1-9][0-9]*)", normalized)
    if match:
        number = int(match.group(1))
        if number > 18:
            raise ValueError("--stop-after chapter:N 的 N 必须在 1–18")
        return "chapter", number
    raise ValueError(
        "--stop-after 仅支持 book、macro-outline、opening-outline 或 chapter:N"
    )


def result_is_complete(result_dir: Path, expected_run_id: str) -> bool:
    required = (
        "book.json", "macro_outline.json", "opening_outline.json",
        "novel.md", "manifest.json",
    )
    if any(not (result_dir / name).exists() for name in required):
        return False
    try:
        manifest = read_json(result_dir / "manifest.json")
        if (
            manifest.get("schema") != PROTOCOL_VERSION
            or manifest.get("run_id") != expected_run_id
            or not str(manifest.get("run_input_sha256") or "").startswith(expected_run_id)
            or manifest.get("protocol_policy") != PROTOCOL_POLICY
            or manifest.get("protocol_policy_sha256") != protocol_policy_sha256()
            or manifest.get("run_origin") != "fresh"
            or manifest.get("status") != "completed"
        ):
            return False
        code_hash = manifest.get("code_sha256")
        manifest_parameters = manifest.get("parameters")
        manifest_protocol = (
            manifest_parameters.get("protocol", OPENAI_CHAT_COMPLETIONS)
            if isinstance(manifest_parameters, dict)
            else OPENAI_CHAT_COMPLETIONS
        )
        expected_code_hash = calculate_code_hash(
            {"protocol": manifest_protocol}
        )
        if (
            not isinstance(code_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", code_hash)
            or code_hash != expected_code_hash
        ):
            return False
        run_input_sha256 = manifest.get("run_input_sha256")
        if (
            not isinstance(run_input_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", run_input_sha256)
            or run_input_sha256[:12] != expected_run_id
        ):
            return False
        context_audit = manifest.get("context_audit")
        if (
            not isinstance(context_audit, dict)
            or context_audit.get("calls") != (manifest.get("usage") or {}).get("calls")
            or not isinstance(context_audit.get("estimate_sources"), dict)
        ):
            return False

        book = validate_book(read_json(result_dir / "book.json"))
        validate_macro_outline(read_json(result_dir / "macro_outline.json"))
        opening = validate_opening_outline(read_json(result_dir / "opening_outline.json"))
        expected_chapter_names = {
            f"{int(chapter['number']):02d}.md" for chapter in opening["chapters"]
        }
        chapter_dir = result_dir / "chapters"
        if not chapter_dir.is_dir():
            return False
        actual_chapter_names = {path.name for path in chapter_dir.glob("*.md")}
        if actual_chapter_names != expected_chapter_names:
            return False

        chapter_texts: list[str] = []
        chapter_chars: list[int] = []
        for chapter in opening["chapters"]:
            path = chapter_dir / f"{int(chapter['number']):02d}.md"
            clean = validate_chapter(path.read_text(encoding="utf-8"), chapter)
            chapter_texts.append(clean)
            chapter_chars.append(count_content_chars(clean))
        body_chars = sum(chapter_chars)
        if body_chars < MIN_FINAL_CHARS:
            return False
        if manifest.get("body_chars") != body_chars:
            return False

        chapter_manifest = manifest.get("chapters")
        if not isinstance(chapter_manifest, list) or len(chapter_manifest) != len(chapter_chars):
            return False
        for expected, actual_chars in zip(chapter_manifest, chapter_chars):
            if not isinstance(expected, dict) or expected.get("chars") != actual_chars:
                return False
            initial_chars = expected.get("initial_chars")
            expansion_requested = expected.get("expansion_requested")
            expansion_attempt_count = expected.get("expansion_attempt_count")
            expansion_result_chars = expected.get("expansion_result_chars")
            expansion_adopted = expected.get("expansion_adopted")
            expansion_outcome = expected.get("expansion_outcome")
            if (
                isinstance(initial_chars, bool)
                or not isinstance(initial_chars, int)
                or initial_chars <= 0
                or not isinstance(expansion_requested, bool)
                or expansion_attempt_count not in (0, 1)
                or not isinstance(expansion_adopted, bool)
                or not isinstance(expansion_outcome, str)
            ):
                return False
            if expansion_requested:
                if (
                    initial_chars >= CHAPTER_EXPANSION_TRIGGER_CHARS
                    or expansion_attempt_count != 1
                    or actual_chars < initial_chars
                ):
                    return False
                if expansion_result_chars is not None and (
                    isinstance(expansion_result_chars, bool)
                    or not isinstance(expansion_result_chars, int)
                    or expansion_result_chars < 0
                ):
                    return False
                if expansion_adopted and (
                    expansion_result_chars != actual_chars
                    or actual_chars <= initial_chars
                    or expansion_outcome != "adopted"
                ):
                    return False
                if not expansion_adopted and actual_chars != initial_chars:
                    return False
            elif (
                initial_chars != actual_chars
                or expansion_attempt_count != 0
                or expansion_result_chars is not None
                or expansion_adopted
                or expansion_outcome != "not_needed"
            ):
                return False

        expected_novel = f"# {book['title']}\n\n" + "\n\n".join(chapter_texts).rstrip() + "\n"
        actual_novel = normalize_newlines(
            (result_dir / "novel.md").read_bytes().decode("utf-8-sig")
        )
        if actual_novel != expected_novel:
            return False

        artifact_hashes = manifest.get("artifact_sha256")
        if not isinstance(artifact_hashes, dict):
            return False
        artifact_names = {
            "book.json", "macro_outline.json", "opening_outline.json", "novel.md",
            *{f"chapters/{name}" for name in expected_chapter_names},
        }
        if set(artifact_hashes) != artifact_names:
            return False
        for name in artifact_names:
            digest = artifact_hashes.get(name)
            if (
                not isinstance(digest, str)
                or sha256_normalized_text_file(result_dir / Path(name)) != digest
            ):
                return False

        attempts = manifest.get("attempts")
        if not isinstance(attempts, dict):
            return False
        for chapter_entry in chapter_manifest:
            number = int(chapter_entry["number"])
            if attempts.get(f"chapter_expansion_{number:02d}", 0) != chapter_entry.get(
                "expansion_attempt_count"
            ):
                return False
        expected_retries = sum(max(0, int(count) - 1) for count in attempts.values())
        if manifest.get("retry_count") != expected_retries:
            return False
        return True
    except Exception:
        return False


def calculate_run_id(benchmark: str, direction: str, prompts: dict[str, str], model_cfg: dict[str, Any]) -> str:
    return sha256_text(
        canonical_json(build_run_input(benchmark, direction, prompts, model_cfg))
    )[:12]


def validate_fixed_registries(
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Require the exact V2.1 field: 15 generators and three fixed judges."""
    models = cfg.get("models")
    judges = cfg.get("judges")
    if not isinstance(models, list) or not all(isinstance(item, dict) for item in models):
        raise ValueError("config.yaml 的 models 必须是对象数组")
    if not isinstance(judges, list) or not all(isinstance(item, dict) for item in judges):
        raise ValueError("config.yaml 的 judges 必须是对象数组")
    model_ids = tuple(str(item.get("id") or "") for item in models)
    if model_ids != EXPECTED_GENERATOR_IDS:
        raise ValueError(
            "V2 生成模型必须严格按固定 15 模型配置，当前：" + ", ".join(model_ids)
        )
    for item in models:
        if item.get("model") != item.get("id"):
            raise ValueError(f"生成模型 {item.get('id')} 的 wire model 不允许静默替换")
    judge_ids = tuple(str(item.get("id") or "") for item in judges)
    if judge_ids != tuple(EXPECTED_JUDGES):
        raise ValueError("V2 评委必须严格为 sol、grok、kimi")
    for item in judges:
        expected_model = EXPECTED_JUDGES[str(item["id"])]
        if item.get("model") != expected_model:
            raise ValueError(
                f"评委 {item['id']} 必须使用 {expected_model}，不得静默替换"
            )
    return list(models), list(judges)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="自主长篇评测 V2.1 生成器")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--model", action="append", help="生成指定模型，可重复")
    selection.add_argument("--all", action="store_true", help="显式生成全部 15 个模型")
    parser.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env", dest="env_file", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="只做配置与模型 preflight")
    parser.add_argument(
        "--new-run",
        action="store_true",
        help="授权新 run-id 接替旧成品；匹配当前 run-id 的有效断点仍会保留",
    )
    parser.add_argument(
        "--stop-after",
        metavar="CHECKPOINT",
        help="在 book、macro-outline、opening-outline 或 chapter:N 检查点停止且不发布",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = repo_root()
    config_path = args.config or root / "config.yaml"
    env_path = args.env_file or root / ".env"
    direction_path = root / "benchmark" / args.benchmark / "direction.md"
    prompt_dir = root / "runner" / "prompts" / "v2.1"
    if not direction_path.exists():
        fail(f"缺少题材方向：{direction_path}")
        return 1
    cfg = load_config(config_path)
    prompts = load_prompts(prompt_dir)
    direction = canonical_text(direction_path.read_bytes().decode("utf-8-sig"))
    try:
        parse_stop_after(args.stop_after)
        raw_model_cfgs, all_judge_cfgs = validate_fixed_registries(cfg)
        all_model_cfgs = [
            with_provider_request_defaults(cfg, model_cfg)
            for model_cfg in raw_model_cfgs
        ]
        for model_cfg in all_model_cfgs:
            for stage in (
                "book",
                "macro_outline",
                "opening_outline",
                "chapter",
                "chapter_expansion",
            ):
                params = generation_request_parameters(model_cfg, stage)
                if params:
                    raise ValueError(
                        f"生成模型 {model_cfg['id']} 的 {stage} 必须使用服务端默认参数；"
                        "禁止显式字段：" + ", ".join(sorted(params))
                    )
    except Exception as exc:
        fail(str(exc))
        return 1
    configured = [str(item["id"]) for item in all_model_cfgs]
    model_ids = configured if args.all else list(args.model or [])
    try:
        configured_by_id = {str(item["id"]): item for item in all_model_cfgs}
        unknown = [model_id for model_id in model_ids if model_id not in configured_by_id]
        if unknown:
            raise ValueError("未知模型 id（不允许模糊匹配或别名）：" + ", ".join(unknown))
        model_cfgs = [configured_by_id[model_id] for model_id in model_ids]
    except Exception as exc:
        fail(str(exc))
        return 1

    stale: list[str] = []
    pending: list[dict[str, Any]] = []
    for model_cfg in model_cfgs:
        run_id = calculate_run_id(args.benchmark, direction, prompts, model_cfg)
        result_dir = root / "results" / args.benchmark / str(model_cfg["id"])
        current_work_dir = (
            root
            / "work"
            / "v2.1"
            / args.benchmark
            / str(model_cfg["id"])
            / run_id
        )
        if result_is_complete(result_dir, run_id) and not args.new_run:
            try:
                with WorkDirLock(current_work_dir.parent / ".run.lock"):
                    cleanup_completed_publish_debris(result_dir, run_id)
            except RuntimeError:
                log(
                    f"{model_cfg['id']} 已完成；另一个进程正在处理该模型，"
                    "本次跳过残留目录清理"
                )
            log(f"{model_cfg['id']} 已完成且哈希匹配，离线跳过")
            continue
        manifest_path = result_dir / "manifest.json"
        if manifest_path.exists() and not args.new_run:
            try:
                manifest_run_id = read_json(manifest_path).get("run_id")
            except Exception:
                manifest_run_id = None
            if manifest_run_id != run_id:
                work_dir = (
                    current_work_dir
                )
                if not work_checkpoint_is_resumable(work_dir, run_id):
                    stale.append(str(model_cfg["id"]))
                    continue
        if not args.new_run:
            if current_work_dir.exists() and not work_checkpoint_is_resumable(
                current_work_dir, run_id
            ):
                stale.append(str(model_cfg["id"]))
                continue
            other_runs = resumable_other_run_ids(
                current_work_dir.parent, run_id
            )
            if not current_work_dir.exists() and other_runs:
                stale.append(str(model_cfg["id"]))
                continue
        pending.append(model_cfg)
    if stale:
        fail(
            "以下模型存在不同 run-id 的旧成品/断点或损坏断点，"
            "请使用 --new-run：" + ", ".join(stale)
        )
        return 1
    if not pending and not args.dry_run:
        log("没有需要生成的模型")
        return 0

    env = load_env_file(env_path)
    env.update(os.environ)
    try:
        client = ChatClient.from_config(cfg, env, provider_id="new-api")
        available = client.list_models()
    except Exception as exc:
        fail(f"New API preflight 失败：{exc}")
        return 1
    wire_models = {str(model_cfg["model"]) for model_cfg in all_model_cfgs}
    judge_models = {str(judge["model"]) for judge in all_judge_cfgs}
    missing = sorted((wire_models | judge_models) - set(available))
    if missing:
        fail("/v1/models 缺少配置模型：" + ", ".join(missing))
        return 1
    log(f"preflight 通过：全部 {len(all_model_cfgs)} 个生成模型、{len(all_judge_cfgs)} 个评委均精确存在")
    for model_cfg in model_cfgs:
        context_window = int(model_cfg.get("context_window", 131_072))
        safe_context = int(context_window * 0.85)
        wire_protocol = model_protocol(model_cfg)
        required_parameters = protocol_required_parameters(model_cfg)
        required_label = (
            ", ".join(
                f"{key}={value}" for key, value in sorted(required_parameters.items())
            )
            or "none"
        )
        log(
            f"{model_cfg['id']}: wire={model_cfg['model']}, protocol={wire_protocol}, "
            f"path={protocol_endpoint_path(wire_protocol)}, context={context_window}, "
            f"85%安全线={safe_context}, api_optional_params=none（服务端默认），"
            f"protocol_required={required_label}，"
            f"基础调用=19–21；若每章均不足3000字，最多追加16–18次扩写"
            f"（总计35–39，格式修复另计），"
            f"run={calculate_run_id(args.benchmark, direction, prompts, model_cfg)}"
        )
    if args.dry_run:
        log(
            f"dry-run 完成；选择 {len(model_cfgs)} 个模型，其中待生成 {len(pending)} 个；"
            "未发 completion 请求"
        )
        return 0

    failures: list[str] = []
    for model_cfg in pending:
        try:
            run = GenerationRun(
                root=root,
                benchmark=args.benchmark,
                direction=direction,
                prompts=prompts,
                model_cfg=model_cfg,
                client=client,
                new_run=args.new_run,
            )
            run.execute(args.stop_after)
        except Exception as exc:
            failures.append(str(model_cfg["id"]))
            fail(f"{model_cfg['id']} 失败：{exc}")
    if failures:
        fail("失败模型：" + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
