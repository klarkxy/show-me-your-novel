#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V3 opening protocol: lock world, then characters, then outline, then prose."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

try:
    from . import generate as g
    from .llm_api import (
        ChatClient,
        get_model_config,
        load_config,
        load_env_file,
        with_provider_request_defaults,
    )
except ImportError:  # pragma: no cover
    import generate as g  # type: ignore
    from llm_api import (  # type: ignore
        ChatClient,
        get_model_config,
        load_config,
        load_env_file,
        with_provider_request_defaults,
    )


PROTOCOL_VERSION = "novel-benchmark.v3"
DEFAULT_BENCHMARK = "foundation-city"
PROMPT_FILES = (
    "system.md",
    "world.md",
    "characters.md",
    "outline.md",
    "beat.md",
    "expand_beat.md",
)
DESIGN_STAGES = ("world", "characters", "outline")
SKIP_FROM_ALL = ("gpt-5.6-luna", "agnes-2.5-flash")
MIN_BEAT_CHARS = 500
# Prompt still asks for 500–1000. The accept gate is looser so a deterministic
# model that overshoots cannot deadlock the isolated repair loop.
MAX_BEAT_CHARS = 2_500
MIN_CHAPTER_CHARS = 2_000
MAX_CHAPTER_CHARS = 3_600
MIN_CHAPTERS = 5
MAX_CHAPTERS = 10
PREVIOUS_TAIL_CHARS = 400
PROSE_SCHEMA = "novel-benchmark.v3.prose"
FROZEN_STYLE = (
    "叙述经过视角人物的经验、偏见和注意力。"
    "人物心里可以叫错、看不懂。"
    "旁白要把公路、汽车、灯牌、证件、手机写成当代物件，让山里人与现代城的反差落在纸面上。"
    "动作、对白、物件和反应彼此接力。"
    "关键处展开，重复流程压缩。"
    "不要念设定。"
)


class OpeningError(RuntimeError):
    """Validation or protocol failure."""


def load_v3_prompts(path: Path) -> dict[str, str]:
    prompts: dict[str, str] = {}
    for name in PROMPT_FILES:
        prompt_path = path / name
        if not prompt_path.is_file():
            raise FileNotFoundError(f"缺少 V3 prompt：{prompt_path}")
        prompts[name] = g.canonical_text(prompt_path.read_bytes().decode("utf-8-sig"))
    return prompts


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpeningError(f"{label} 必须是非空字符串")
    return value.strip()


def _require_text_list(value: Any, label: str, *, minimum: int = 1) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum:
        raise OpeningError(f"{label} 至少 {minimum} 条")
    return [_require_text(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _exact(data: dict[str, Any], fields: tuple[str, ...], label: str) -> None:
    try:
        g._require_exact_fields(data, fields, label)
    except ValueError as exc:
        raise OpeningError(str(exc)) from exc


def validate_world(data: dict[str, Any]) -> dict[str, Any]:
    _exact(
        data,
        ("name", "premise", "rules", "institutions", "opening_constraints", "taboos", "unresolved"),
        "world",
    )
    institutions = data["institutions"]
    if not isinstance(institutions, list) or len(institutions) < 1:
        raise OpeningError("world.institutions 至少 1 个")
    cleaned = []
    for index, item in enumerate(institutions):
        if not isinstance(item, dict):
            raise OpeningError(f"world.institutions[{index}] 必须是对象")
        _exact(item, ("name", "wants", "can", "cannot"), f"institutions[{index}]")
        cleaned.append({key: _require_text(item[key], f"institutions[{index}].{key}") for key in item})
    return {
        "name": _require_text(data["name"], "world.name"),
        "premise": _require_text(data["premise"], "world.premise"),
        "rules": _require_text_list(data["rules"], "world.rules", minimum=1),
        "institutions": cleaned,
        "opening_constraints": _require_text_list(
            data["opening_constraints"], "world.opening_constraints", minimum=1
        ),
        "taboos": _require_text_list(data["taboos"], "world.taboos"),
        "unresolved": _require_text_list(data["unresolved"], "world.unresolved"),
    }


def validate_characters(data: dict[str, Any]) -> dict[str, Any]:
    _exact(data, ("viewpoint", "cast"), "characters")
    viewpoint = _require_text(data["viewpoint"], "characters.viewpoint")
    cast = data["cast"]
    if not isinstance(cast, list) or not 3 <= len(cast) <= 5:
        raise OpeningError("characters.cast 必须是 3–5 人")
    names: list[str] = []
    cleaned = []
    fields = (
        "name",
        "role_in_incident",
        "desire",
        "cannot_accept",
        "knows",
        "can_decide",
        "how_refuses",
        "attention",
        "entry_state",
    )
    for index, item in enumerate(cast):
        if not isinstance(item, dict):
            raise OpeningError(f"cast[{index}] 必须是对象")
        _exact(item, fields, f"cast[{index}]")
        person = {key: _require_text(item[key], f"cast[{index}].{key}") for key in fields}
        names.append(person["name"])
        cleaned.append(person)
    if viewpoint not in names:
        raise OpeningError("viewpoint 必须是 cast 中的一个人")
    if len(set(names)) != len(names):
        raise OpeningError("人物名不得重复")
    return {"viewpoint": viewpoint, "cast": cleaned}


def validate_outline(data: dict[str, Any]) -> dict[str, Any]:
    _exact(
        data, ("incident_one_liner", "first_irreversible", "not_resolved", "chapters"), "outline"
    )
    chapters = data["chapters"]
    if not isinstance(chapters, list) or not MIN_CHAPTERS <= len(chapters) <= MAX_CHAPTERS:
        raise OpeningError(f"outline.chapters 必须是 {MIN_CHAPTERS}–{MAX_CHAPTERS} 章")
    cleaned_chapters = []
    expected = 1
    chapter_fields = (
        "number",
        "title",
        "function",
        "spine",
        "pressures",
        "must_keep",
        "must_not_lock",
        "prose_free",
        "beats",
    )
    for item in chapters:
        if not isinstance(item, dict):
            raise OpeningError("chapter 必须是对象")
        _exact(item, chapter_fields, f"chapter {item.get('number')}")
        number = item["number"]
        if not isinstance(number, int) or number != expected:
            raise OpeningError(f"章节号必须从 1 连续，读到 {number}")
        beats = _require_text_list(item["beats"], f"chapter {number}.beats", minimum=3)
        if len(beats) > 4:
            raise OpeningError(f"第{number}章 beats 最多 4 条")
        cleaned_chapters.append(
            {
                "number": number,
                "title": _require_text(item["title"], f"chapter {number}.title"),
                "function": _require_text(item["function"], f"chapter {number}.function"),
                "spine": _require_text(item["spine"], f"chapter {number}.spine"),
                "pressures": _require_text_list(item["pressures"], f"chapter {number}.pressures"),
                "must_keep": _require_text_list(item["must_keep"], f"chapter {number}.must_keep"),
                "must_not_lock": _require_text_list(
                    item["must_not_lock"], f"chapter {number}.must_not_lock"
                ),
                "prose_free": _require_text(item["prose_free"], f"chapter {number}.prose_free"),
                "beats": beats,
            }
        )
        expected += 1
    return {
        "incident_one_liner": _require_text(data["incident_one_liner"], "incident_one_liner"),
        "first_irreversible": _require_text(data["first_irreversible"], "first_irreversible"),
        "not_resolved": _require_text_list(data["not_resolved"], "not_resolved"),
        "chapters": cleaned_chapters,
    }


def packs_compatible(world: dict[str, Any], characters: dict[str, Any], outline: dict[str, Any]) -> list[str]:
    """Return human-readable incompatibilities. Empty means they may be frozen together."""

    problems: list[str] = []
    names = {person["name"] for person in characters["cast"]}
    if characters["viewpoint"] not in names:
        problems.append("视角人物不在人物表")
    joined = json.dumps(outline, ensure_ascii=False)
    if characters["viewpoint"] not in joined:
        problems.append("章纲从未点名视角人物，现场可能没这个人")
    if not any(token in joined for token in ("高楼", "大厦")):
        problems.append("章纲没接上题目里的高楼")
    if not any(token in joined for token in ("山", "筑基", "修士")):
        problems.append("章纲没接上题目里的山或修士")
    return problems


def render_frozen_markdown(
    world: dict[str, Any],
    characters: dict[str, Any],
    outline: dict[str, Any],
) -> str:
    lines = [
        "# 冻结开局提示词",
        "",
        "## 文风义务",
        FROZEN_STYLE,
        "",
        "## 世界",
        json.dumps(world, ensure_ascii=False, indent=2),
        "",
        "## 人物",
        json.dumps(characters, ensure_ascii=False, indent=2),
        "",
        "## 章纲",
        json.dumps(outline, ensure_ascii=False, indent=2),
    ]
    return "\n".join(lines) + "\n"


def _dump_json(data: dict[str, Any] | None) -> str:
    if data is None:
        return "（尚未锁定）"
    return json.dumps(data, ensure_ascii=False, indent=2)


def design_user_prompt(
    prompts: dict[str, str],
    stage: str,
    direction: str,
    *,
    world: dict[str, Any] | None = None,
    characters: dict[str, Any] | None = None,
) -> str:
    text = prompts[f"{stage}.md"].format(
        direction=direction,
        world="__WORLD__",
        characters="__CHARACTERS__",
    )
    return text.replace("__WORLD__", _dump_json(world)).replace(
        "__CHARACTERS__", _dump_json(characters)
    )


def assert_design_prompt(
    user_prompt: str,
    direction: str,
    stage: str,
    *,
    world: dict[str, Any] | None = None,
    characters: dict[str, Any] | None = None,
) -> None:
    if direction.strip() not in user_prompt:
        raise OpeningError("设计提示必须包含题目")
    if stage in {"characters", "outline"}:
        if world is None:
            raise OpeningError("人物/章纲必须先有已定世界")
        marker = world.get("name") or world.get("premise") or ""
        if marker and marker not in user_prompt:
            raise OpeningError("人物/章纲提示必须带上已定世界")
    if stage == "outline":
        if characters is None:
            raise OpeningError("章纲必须先有已定人物")
        viewpoint = characters.get("viewpoint") or ""
        if viewpoint and viewpoint not in user_prompt:
            raise OpeningError("章纲提示必须带上已定人物")
    lowered = user_prompt.lower()
    for token in ("macro_outline", "opening_outline", "book.json"):
        if token in lowered:
            raise OpeningError(f"设计提示泄漏了无关材料：{token}")


def assert_design_isolated(user_prompt: str, direction: str) -> None:
    assert_design_prompt(user_prompt, direction, "world")


def load_design_dir(model_dir: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    mapping = {
        "world": ("world.json", validate_world),
        "characters": ("characters.json", validate_characters),
        "outline": ("outline.json", validate_outline),
    }
    for key, (name, validator) in mapping.items():
        path = model_dir / name
        if not path.is_file():
            continue
        payload[key] = validator(json.loads(path.read_text(encoding="utf-8")))
    return payload


def assemble_frozen(
    results_root: Path,
    frozen_dir: Path,
    *,
    world_model: str,
    characters_model: str,
    outline_model: str,
    allow_incompatible: bool = False,
) -> dict[str, Any]:
    world = load_design_dir(results_root / world_model)["world"]
    characters = load_design_dir(results_root / characters_model)["characters"]
    outline = load_design_dir(results_root / outline_model)["outline"]
    problems = packs_compatible(world, characters, outline)
    if problems and not allow_incompatible:
        raise OpeningError("冻结包不相容：" + "；".join(problems))
    frozen_dir.mkdir(parents=True, exist_ok=True)
    pack = {
        "schema": PROTOCOL_VERSION,
        "benchmark": DEFAULT_BENCHMARK,
        "sources": {
            "world": world_model,
            "characters": characters_model,
            "outline": outline_model,
        },
        "compatibility_problems": problems,
        "style": FROZEN_STYLE,
        "world": world,
        "characters": characters,
        "outline": outline,
    }
    g.atomic_write_json(frozen_dir / "pack.json", pack)
    g.atomic_write_text(frozen_dir / "prompt.md", render_frozen_markdown(world, characters, outline))
    return pack


def render_beat_materials(
    pack: dict[str, Any],
    chapter: dict[str, Any],
    beat_index: int,
) -> str:
    """World, cast, and only the current beat. Later outline stays hidden."""

    slice_card = {
        "number": chapter["number"],
        "title": chapter["title"],
        "function": chapter["function"],
        "must_not_lock": chapter["must_not_lock"],
        "beat": chapter["beats"][beat_index - 1],
    }
    lines = [
        "# 本节材料",
        "",
        "## 文风义务",
        pack.get("style") or FROZEN_STYLE,
        "",
        "## 世界",
        json.dumps(pack["world"], ensure_ascii=False, indent=2),
        "",
        "## 人物",
        json.dumps(pack["characters"], ensure_ascii=False, indent=2),
        "",
        "## 本节章纲",
        json.dumps(slice_card, ensure_ascii=False, indent=2),
    ]
    return "\n".join(lines) + "\n"


def beat_user_prompt(
    prompts: dict[str, str],
    pack: dict[str, Any],
    chapter: dict[str, Any],
    beat_index: int,
    previous_tail: str,
) -> str:
    # JSON and prior prose contain braces. Do not str.format them.
    replacements = {
        "{chapter_number}": str(chapter["number"]),
        "{chapter_title}": str(chapter["title"]),
        "{beat_index}": str(beat_index),
        "{beat_goal}": chapter["beats"][beat_index - 1],
        "{must_not_lock}": "；".join(chapter["must_not_lock"]),
        "{beat_materials}": render_beat_materials(pack, chapter, beat_index),
        "{previous_tail}": previous_tail or "（本章第一节）",
    }
    text = prompts["beat.md"]
    for token, value in replacements.items():
        text = text.replace(token, value)
    return text


def expand_beat_prompt(
    prompts: dict[str, str],
    chapter: dict[str, Any],
    beat_index: int,
    draft: str,
    current_chars: int,
) -> str:
    replacements = {
        "{chapter_number}": str(chapter["number"]),
        "{chapter_title}": str(chapter["title"]),
        "{beat_index}": str(beat_index),
        "{current_chars}": str(current_chars),
        "{beat_goal}": chapter["beats"][beat_index - 1],
        "{draft}": draft,
    }
    text = prompts["expand_beat.md"]
    for token, value in replacements.items():
        text = text.replace(token, value)
    return text


def beat_previous_tail(text: str, limit: int = PREVIOUS_TAIL_CHARS) -> str:
    cleaned = g.canonical_text(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[-limit:]


def inspect_beat(text: str) -> str:
    cleaned = g.normalize_chapter(text)
    if not cleaned:
        raise OpeningError("节拍为空")
    if "```" in cleaned:
        raise OpeningError("节拍包含代码围栏")
    try:
        g._reject_private_reasoning_markers(cleaned, "beat")
    except ValueError as exc:
        raise OpeningError(str(exc)) from exc
    first = cleaned.splitlines()[0].strip()
    if first.startswith("#") or re.match(r"^第\s*\d+\s*[章节]", first):
        raise OpeningError("节拍不要标题")
    return cleaned


def validate_beat(text: str) -> str:
    cleaned = inspect_beat(text)
    chars = g.count_content_chars(cleaned)
    if chars < MIN_BEAT_CHARS:
        raise OpeningError(f"节拍过短：{chars} < {MIN_BEAT_CHARS}")
    if chars > MAX_BEAT_CHARS:
        raise OpeningError(f"节拍过长：{chars} > {MAX_BEAT_CHARS}")
    return cleaned


def assemble_chapter(beats: list[str]) -> str:
    if not 3 <= len(beats) <= 4:
        raise OpeningError(f"一章必须 3–4 节，收到 {len(beats)}")
    body = "\n\n".join(beat.strip() for beat in beats)
    chars = g.count_content_chars(body)
    minimum = MIN_BEAT_CHARS * len(beats)
    maximum = MAX_BEAT_CHARS * len(beats)
    if chars < minimum:
        raise OpeningError(f"整章过短：{chars} < {minimum}")
    if chars > maximum:
        raise OpeningError(f"整章过长：{chars} > {maximum}")
    return body


def render_chapter_markdown(number: int, title: str, body: str) -> str:
    return f"## 第{number}章 {title}\n\n{body.strip()}\n"


def assemble_novel(chapter_texts: list[str]) -> str:
    return "\n".join(text.rstrip() + "\n" for text in chapter_texts)


def load_frozen_pack(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise OpeningError("缺少冻结包，先 assemble-v3")
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpeningError(f"冻结包损坏：{exc}") from exc
    if not isinstance(data, dict):
        raise OpeningError("冻结包不是对象")
    for key in ("world", "characters", "outline"):
        if key not in data:
            raise OpeningError(f"冻结包缺少 {key}")
    return {
        **data,
        "world": validate_world(data["world"]),
        "characters": validate_characters(data["characters"]),
        "outline": validate_outline(data["outline"]),
        "style": _require_text(data.get("style") or FROZEN_STYLE, "style"),
    }


def parse_v3_stop_after(value: str | None, phase: str) -> tuple[str | None, int | None]:
    """Return ``(design_stage, chapter_number)``."""
    if value is None:
        return ("world" if phase == "design" else None, None)
    text = value.strip()
    lowered = text.lower()
    if lowered in DESIGN_STAGES:
        if phase != "design":
            raise OpeningError("正文段 --stop-after 只用 chapter:N")
        return lowered, None
    match = re.fullmatch(r"chapter:([1-9][0-9]*)", lowered)
    if match:
        if phase != "prose":
            raise OpeningError("chapter:N 只用于 --phase prose")
        number = int(match.group(1))
        if number > MAX_CHAPTERS:
            raise OpeningError(f"--stop-after chapter:N 的 N 必须在 1–{MAX_CHAPTERS}")
        return None, number
    raise OpeningError("--stop-after 仅支持 world、characters、outline 或 chapter:N")


def beat_artifact_path(output_dir: Path, chapter: int, beat: int) -> Path:
    return output_dir / "beats" / f"{chapter:02d}-{beat:02d}.md"


def chapter_artifact_path(output_dir: Path, chapter: int) -> Path:
    return output_dir / "chapters" / f"{chapter:02d}.md"


def try_load_beat(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return validate_beat(path.read_bytes().decode("utf-8-sig"))
    except (OSError, OpeningError, ValueError, TypeError):
        return None


_PRINT_LOCK = threading.Lock()
_STAGE_VALIDATORS = {
    "world": validate_world,
    "characters": validate_characters,
    "outline": validate_outline,
}


def _log(message: str) -> None:
    with _PRINT_LOCK:
        print(message, flush=True)


def try_load_stage(output_dir: Path, stage: str) -> dict[str, Any] | None:
    path = output_dir / f"{stage}.json"
    if not path.is_file():
        return None
    try:
        return _STAGE_VALIDATORS[stage](json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, OpeningError, ValueError, TypeError):
        return None


def _complete_with_retry(
    client: ChatClient,
    model_cfg: dict[str, Any],
    system: str,
    user: str,
    accept: Callable[[str], Any],
    *,
    stage: str,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, g.MAX_STAGE_ATTEMPTS + 1):
        try:
            result = client.complete(
                model_cfg,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                stage=stage,
            )
            return accept(result.content)
        except g.LLMAPIError as exc:
            last_error = exc
            if not g.api_error_is_retryable(exc) or attempt >= g.MAX_STAGE_ATTEMPTS:
                raise
            delay = g.retry_delay_seconds(exc, attempt)
            _log(f"[generate-v3] retry {stage} after {exc} in {delay:.1f}s")
            time.sleep(delay)
        except (OpeningError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt >= g.MAX_STAGE_ATTEMPTS:
                raise OpeningError(f"{stage} 解析失败：{exc}") from exc
            _log(f"[generate-v3] repair {stage}: {exc}")
    raise OpeningError(f"{stage} 失败：{last_error}")


def _complete_json(
    client: ChatClient,
    model_cfg: dict[str, Any],
    system: str,
    user: str,
    validator,
    *,
    stage: str,
) -> dict[str, Any]:
    def accept(content: str) -> dict[str, Any]:
        return validator(g.parse_json_object(content))

    return _complete_with_retry(
        client, model_cfg, system, user, accept, stage=stage
    )


def _one_completion(
    client: ChatClient,
    model_cfg: dict[str, Any],
    system: str,
    user: str,
    *,
    stage: str,
) -> str:
    result = client.complete(
        model_cfg,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        stage=stage,
    )
    return inspect_beat(result.content)


def _complete_beat(
    client: ChatClient,
    model_cfg: dict[str, Any],
    system: str,
    user: str,
    *,
    stage: str,
    prompts: dict[str, str],
    chapter: dict[str, Any],
    beat_index: int,
) -> str:
    last_error: Exception | None = None
    for attempt in range(1, g.MAX_STAGE_ATTEMPTS + 1):
        try:
            draft = _one_completion(client, model_cfg, system, user, stage=stage)
            chars = g.count_content_chars(draft)
            if MIN_BEAT_CHARS <= chars <= MAX_BEAT_CHARS:
                return draft
            if chars > MAX_BEAT_CHARS:
                raise OpeningError(f"节拍过长：{chars} > {MAX_BEAT_CHARS}")
            _log(f"[generate-v3] expand {stage}: {chars} < {MIN_BEAT_CHARS}")
            expand_user = expand_beat_prompt(
                prompts, chapter, beat_index, draft, chars
            )
            expanded = _one_completion(
                client,
                model_cfg,
                system,
                expand_user,
                stage=f"{stage}-expand",
            )
            expanded_chars = g.count_content_chars(expanded)
            if MIN_BEAT_CHARS <= expanded_chars <= MAX_BEAT_CHARS:
                return expanded
            if expanded_chars > MAX_BEAT_CHARS:
                raise OpeningError(
                    f"扩写后过长：{expanded_chars} > {MAX_BEAT_CHARS}"
                )
            raise OpeningError(
                f"扩写后仍短：{expanded_chars} < {MIN_BEAT_CHARS}"
            )
        except g.LLMAPIError as exc:
            last_error = exc
            if not g.api_error_is_retryable(exc) or attempt >= g.MAX_STAGE_ATTEMPTS:
                raise
            delay = g.retry_delay_seconds(exc, attempt)
            _log(f"[generate-v3] retry {stage} after {exc} in {delay:.1f}s")
            time.sleep(delay)
        except OpeningError as exc:
            last_error = exc
            if attempt >= g.MAX_STAGE_ATTEMPTS:
                raise OpeningError(f"{stage} 解析失败：{exc}") from exc
            _log(f"[generate-v3] repair {stage}: {exc}")
    raise OpeningError(f"{stage} 失败：{last_error}")


def _prose_conflict(output_dir: Path, frozen_sha256: str) -> None:
    manifest_path = output_dir / "prose.json"
    recorded = None
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_bytes().decode("utf-8-sig"))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            recorded = payload.get("frozen_sha256")
    if recorded in {None, frozen_sha256}:
        return
    leftovers = []
    for folder in (output_dir / "beats", output_dir / "chapters"):
        if folder.is_dir():
            leftovers.extend(folder.glob("*.md"))
    if (output_dir / "novel.md").is_file():
        leftovers.append(output_dir / "novel.md")
    if leftovers:
        raise OpeningError(
            "冻结包已变，旧正文不能续跑；删掉 beats/、chapters/、novel.md、prose.json 后再跑"
        )


def _write_prose_manifest(
    output_dir: Path,
    *,
    model_id: str,
    benchmark: str,
    frozen_sha256: str,
    chapters: list[dict[str, Any]],
    complete: bool,
    stop_after_chapter: int | None,
) -> dict[str, Any]:
    artifacts: dict[str, str] = {}
    for item in chapters:
        number = item["number"]
        beat_count = item["beats"]
        for beat_index in range(1, beat_count + 1):
            path = beat_artifact_path(output_dir, number, beat_index)
            artifacts[path.relative_to(output_dir).as_posix()] = g.sha256_file(path)
        chapter_path = chapter_artifact_path(output_dir, number)
        artifacts[chapter_path.relative_to(output_dir).as_posix()] = g.sha256_file(
            chapter_path
        )
    novel_path = output_dir / "novel.md"
    if complete and novel_path.is_file():
        artifacts["novel.md"] = g.sha256_file(novel_path)
    payload = {
        "schema": PROSE_SCHEMA,
        "benchmark": benchmark,
        "model_id": model_id,
        "frozen_sha256": frozen_sha256,
        "status": "complete" if complete else "partial",
        "stop_after_chapter": stop_after_chapter,
        "chapters": chapters,
        "total_chars": sum(int(item["chars"]) for item in chapters),
        "artifact_sha256": artifacts,
    }
    g.atomic_write_json(output_dir / "prose.json", payload)
    return payload


def run_prose(
    *,
    client: ChatClient,
    model_cfg: dict[str, Any],
    prompts: dict[str, str],
    pack: dict[str, Any],
    pack_path: Path,
    output_dir: Path,
    model_id: str,
    stop_after_chapter: int | None = None,
) -> tuple[str, str]:
    frozen_sha256 = g.sha256_file(pack_path)
    _prose_conflict(output_dir, frozen_sha256)
    chapters = pack["outline"]["chapters"]
    if stop_after_chapter is not None:
        targets = [item for item in chapters if item["number"] <= stop_after_chapter]
    else:
        targets = list(chapters)
    if not targets:
        raise OpeningError("冻结章纲是空的")
    complete = targets[-1]["number"] >= chapters[-1]["number"]
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, Any]] = []
    chapter_texts: list[str] = []
    for chapter in targets:
        number = chapter["number"]
        title = chapter["title"]
        goals = chapter["beats"]
        beats: list[str] = []
        missing: list[int] = []
        for beat_index in range(1, len(goals) + 1):
            cached = try_load_beat(beat_artifact_path(output_dir, number, beat_index))
            if cached is None:
                missing.append(beat_index)
            else:
                beats.append(cached)
        if not missing and len(beats) == len(goals):
            _log(f"[generate-v3] {model_id} chapter {number}: cached")
        else:
            beats = []
            for beat_index, _goal in enumerate(goals, start=1):
                path = beat_artifact_path(output_dir, number, beat_index)
                cached = try_load_beat(path)
                if cached is not None:
                    _log(f"[generate-v3] {model_id} beat {number}.{beat_index}: cached")
                    beats.append(cached)
                    continue
                tail = beat_previous_tail(beats[-1]) if beats else ""
                user = beat_user_prompt(prompts, pack, chapter, beat_index, tail)
                text = _complete_beat(
                    client,
                    model_cfg,
                    prompts["system.md"],
                    user,
                    stage=f"v3-beat-{number}-{beat_index}",
                    prompts=prompts,
                    chapter=chapter,
                    beat_index=beat_index,
                )
                g.atomic_write_text(path, text + "\n")
                _log(f"[generate-v3] {model_id} beat {number}.{beat_index}: wrote")
                beats.append(text)
        body = assemble_chapter(beats)
        chapter_text = render_chapter_markdown(number, title, body)
        g.atomic_write_text(chapter_artifact_path(output_dir, number), chapter_text)
        chapter_texts.append(chapter_text)
        written.append(
            {
                "number": number,
                "title": title,
                "beats": len(beats),
                "chars": g.count_content_chars(body),
            }
        )
    if complete:
        g.atomic_write_text(output_dir / "novel.md", assemble_novel(chapter_texts))
        _log(f"[generate-v3] {model_id} novel: wrote")
    _write_prose_manifest(
        output_dir,
        model_id=model_id,
        benchmark=str(pack.get("benchmark") or DEFAULT_BENCHMARK),
        frozen_sha256=frozen_sha256,
        chapters=written,
        complete=complete,
        stop_after_chapter=None if complete else stop_after_chapter,
    )
    return model_id, "complete" if complete else f"partial:{written[-1]['number']}"


def run_design_stage(
    *,
    client: ChatClient,
    model_cfg: dict[str, Any],
    prompts: dict[str, str],
    direction: str,
    stage: str,
    output_dir: Path,
    world: dict[str, Any] | None = None,
    characters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cached = try_load_stage(output_dir, stage)
    if cached is not None:
        _log(f"[generate-v3] {output_dir.name} {stage}: cached")
        return cached
    user = design_user_prompt(
        prompts, stage, direction, world=world, characters=characters
    )
    assert_design_prompt(user, direction, stage, world=world, characters=characters)
    data = _complete_json(
        client,
        model_cfg,
        prompts["system.md"],
        user,
        _STAGE_VALIDATORS[stage],
        stage=f"v3-{stage}",
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    g.atomic_write_json(output_dir / f"{stage}.json", data)
    _log(f"[generate-v3] {output_dir.name} {stage}: wrote")
    return data


def _require_stage(
    results_root: Path, model_id: str, stage: str, fallback_dir: Path | None = None
) -> dict[str, Any]:
    for folder in (results_root / model_id, fallback_dir):
        if folder is None:
            continue
        loaded = try_load_stage(folder, stage)
        if loaded is not None:
            return loaded
    raise OpeningError(f"缺少已定{stage}：{model_id}")


def _run_one_model(
    *,
    model_id: str,
    config: dict[str, Any],
    client: ChatClient,
    prompts: dict[str, str],
    direction: str,
    root: Path,
    benchmark: str,
    phase: str,
    stages: tuple[str, ...],
    from_world: str | None,
    from_characters: str | None,
    stop_after_chapter: int | None = None,
) -> tuple[str, str]:
    model_cfg = with_provider_request_defaults(config, get_model_config(config, model_id))
    results_root = root / "results" / benchmark
    out = results_root / model_id
    if phase == "design":
        if "characters" in stages and not from_world:
            raise OpeningError("写人物必须先指定 --from-world，不能用各家自己的世界")
        if "outline" in stages and not from_characters:
            raise OpeningError("写章纲必须先指定 --from-characters，不能用各家自己的人物")
        locked_world = (
            _require_stage(results_root, from_world, "world") if from_world else None
        )
        locked_characters = (
            _require_stage(results_root, from_characters, "characters")
            if from_characters
            else None
        )
        for stage in stages:
            run_design_stage(
                client=client,
                model_cfg=model_cfg,
                prompts=prompts,
                direction=direction,
                stage=stage,
                output_dir=out,
                world=locked_world,
                characters=locked_characters,
            )
        return model_id, "ok"
    frozen = root / "benchmark" / benchmark / "frozen" / "pack.json"
    pack = load_frozen_pack(frozen)
    _log(f"[generate-v3] {model_id} prose against {frozen}")
    return run_prose(
        client=client,
        model_cfg=model_cfg,
        prompts=prompts,
        pack=pack,
        pack_path=frozen,
        output_dir=out,
        model_id=model_id,
        stop_after_chapter=stop_after_chapter,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V3 开局：先锁世界，再锁人物，再锁章纲，再写正文")
    parser.add_argument("--model", action="append", dest="models")
    parser.add_argument("--all", action="store_true")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="跳过模型 id 或此前缀，可重复，例如 claude-",
    )
    parser.add_argument("--phase", choices=("design", "prose"), default="design")
    parser.add_argument(
        "--stop-after",
        default=None,
        help="设计段：world / characters / outline（默认 world）；正文段：chapter:N",
    )
    parser.add_argument("--from-world", help="人物/章纲使用这份已定世界（模型 id）")
    parser.add_argument("--from-characters", help="章纲使用这份已定人物（模型 id）")
    parser.add_argument("--jobs", type=int, default=8, help="并发模型数，默认 8")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    args = parser.parse_args(argv)

    root = g.repo_root()
    config = load_config(root / args.config)
    models = config.get("models") or []
    registry = [item["id"] for item in models if isinstance(item, dict) and item.get("id")]
    selected = list(registry) if args.all else list(args.models or [])
    if args.all:
        selected = [model_id for model_id in selected if model_id not in SKIP_FROM_ALL]
    if args.exclude:
        selected = [
            model_id
            for model_id in selected
            if not any(
                model_id == prefix or model_id.startswith(prefix)
                for prefix in args.exclude
            )
        ]
    if not selected:
        print("[generate-v3] 必须指定 --model 或 --all", file=sys.stderr)
        return 2
    if args.jobs < 1:
        print("[generate-v3] --jobs 必须 >= 1", file=sys.stderr)
        return 2
    try:
        stop_design, stop_chapter = parse_v3_stop_after(args.stop_after, args.phase)
    except OpeningError as exc:
        print(f"[generate-v3] {exc}", file=sys.stderr)
        return 2

    direction_path = root / "benchmark" / args.benchmark / "direction.md"
    prompt_dir = root / "runner" / "prompts" / "v3"
    direction = g.canonical_text(direction_path.read_bytes().decode("utf-8-sig"))
    prompts = load_v3_prompts(prompt_dir)
    assert_design_prompt(design_user_prompt(prompts, "world", direction), direction, "world")

    stages: tuple[str, ...] = ()
    if args.phase == "design":
        assert stop_design is not None
        stages = DESIGN_STAGES[: DESIGN_STAGES.index(stop_design) + 1]
        if args.from_world:
            stages = tuple(stage for stage in stages if stage != "world")
        if args.from_characters:
            stages = tuple(stage for stage in stages if stage not in {"world", "characters"})
        if "characters" in stages and not args.from_world:
            print("[generate-v3] 写人物必须先 --from-world 锁住同一套世界", file=sys.stderr)
            return 2
        if "outline" in stages and not args.from_characters:
            print("[generate-v3] 写章纲必须先 --from-characters 锁住同一套人物", file=sys.stderr)
            return 2
        if not stages:
            print("[generate-v3] 没有可跑的设计段", file=sys.stderr)
            return 2
    else:
        frozen = root / "benchmark" / args.benchmark / "frozen" / "pack.json"
        if not frozen.is_file():
            print("[generate-v3] 缺少冻结包，先 assemble-v3", file=sys.stderr)
            return 2
    if args.dry_run:
        print(
            f"[generate-v3] dry-run phase={args.phase} models={len(selected)} "
            f"jobs={args.jobs} stages={','.join(stages) or '-'} layered=ok"
            + (
                f" stop-after=chapter:{stop_chapter}"
                if stop_chapter
                else ""
            )
        )
        return 0

    env = load_env_file(root / args.env)
    env.update(os.environ)
    client = ChatClient.from_config(config, env, provider_id="new-api")
    workers = min(args.jobs, len(selected))
    _log(
        f"[generate-v3] start phase={args.phase} models={len(selected)} "
        f"jobs={workers} stages={','.join(stages) or '-'}"
    )
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _run_one_model,
                model_id=model_id,
                config=config,
                client=client,
                prompts=prompts,
                direction=direction,
                root=root,
                benchmark=args.benchmark,
                phase=args.phase,
                stages=stages,
                from_world=args.from_world,
                from_characters=args.from_characters,
                stop_after_chapter=stop_chapter,
            ): model_id
            for model_id in selected
        }
        for future in as_completed(futures):
            model_id = futures[future]
            try:
                name, status = future.result()
                _log(f"[generate-v3] done {name} {status}")
            except Exception as exc:
                failures.append(f"{model_id}: {exc}")
                _log(f"[generate-v3] FAIL {model_id}: {exc}")
    if failures:
        _log(f"[generate-v3] failed {len(failures)}/{len(selected)}")
        return 1
    _log(f"[generate-v3] all {len(selected)} ok")
    return 0


def assemble_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把三段设计冻成同一份开局提示词")
    parser.add_argument("--world", required=True)
    parser.add_argument("--characters", required=True)
    parser.add_argument("--outline", required=True)
    parser.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    parser.add_argument("--allow-incompatible", action="store_true")
    args = parser.parse_args(argv)
    root = g.repo_root()
    pack = assemble_frozen(
        root / "results" / args.benchmark,
        root / "benchmark" / args.benchmark / "frozen",
        world_model=args.world,
        characters_model=args.characters,
        outline_model=args.outline,
        allow_incompatible=args.allow_incompatible,
    )
    print(
        "[assemble-v3] wrote "
        f"world={pack['sources']['world']} "
        f"characters={pack['sources']['characters']} "
        f"outline={pack['sources']['outline']}"
    )
    if pack["compatibility_problems"]:
        print("[assemble-v3] warnings: " + "；".join(pack["compatibility_problems"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
