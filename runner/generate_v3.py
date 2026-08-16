#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V3 opening protocol: lock world, then characters, then outline, then prose."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

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
PROMPT_FILES = ("system.md", "world.md", "characters.md", "outline.md", "beat.md")
DESIGN_STAGES = ("world", "characters", "outline")
MIN_BEAT_CHARS = 500
MAX_BEAT_CHARS = 1_200
MIN_CHAPTER_CHARS = 2_000
MAX_CHAPTER_CHARS = 3_600
MIN_CHAPTERS = 5
MAX_CHAPTERS = 10
FROZEN_STYLE = (
    "叙述经过视角人物的经验、偏见和注意力。"
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


def beat_user_prompt(
    prompts: dict[str, str],
    pack: dict[str, Any],
    chapter: dict[str, Any],
    beat_index: int,
    previous_tail: str,
) -> str:
    return prompts["beat.md"].format(
        chapter_number=chapter["number"],
        chapter_title=chapter["title"],
        beat_index=beat_index,
        beat_goal=chapter["beats"][beat_index - 1],
        must_not_lock="；".join(chapter["must_not_lock"]),
        frozen_pack=render_frozen_markdown(pack["world"], pack["characters"], pack["outline"]),
        previous_tail=previous_tail or "（本章第一节）",
    )


def validate_beat(text: str) -> str:
    cleaned = g.normalize_chapter(text)
    chars = g.count_content_chars(cleaned)
    if chars < MIN_BEAT_CHARS:
        raise OpeningError(f"节拍过短：{chars} < {MIN_BEAT_CHARS}")
    if chars > MAX_BEAT_CHARS:
        raise OpeningError(f"节拍过长：{chars} > {MAX_BEAT_CHARS}")
    return cleaned


def assemble_chapter(beats: list[str]) -> str:
    body = "\n\n".join(beat.strip() for beat in beats)
    chars = g.count_content_chars(body)
    if chars < MIN_CHAPTER_CHARS:
        raise OpeningError(f"整章过短：{chars} < {MIN_CHAPTER_CHARS}")
    if chars > MAX_CHAPTER_CHARS:
        raise OpeningError(f"整章过长：{chars} > {MAX_CHAPTER_CHARS}")
    return body


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


def _complete_json(
    client: ChatClient,
    model_cfg: dict[str, Any],
    system: str,
    user: str,
    validator,
    *,
    stage: str,
) -> dict[str, Any]:
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
            parsed = g.parse_json_object(result.content)
            return validator(parsed)
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
    if not frozen.is_file():
        raise OpeningError("缺少冻结包，先 assemble-v3")
    _log(f"[generate-v3] {model_id} prose against {frozen}")
    raise OpeningError("prose 循环尚未接入")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V3 开局：先锁世界，再锁人物，再锁章纲")
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
        choices=DESIGN_STAGES,
        default="world",
        help="设计段默认只跑到世界；人物和章纲要等上一层锁定后再跑",
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

    direction_path = root / "benchmark" / args.benchmark / "direction.md"
    prompt_dir = root / "runner" / "prompts" / "v3"
    direction = g.canonical_text(direction_path.read_bytes().decode("utf-8-sig"))
    prompts = load_v3_prompts(prompt_dir)
    assert_design_prompt(design_user_prompt(prompts, "world", direction), direction, "world")

    stages = DESIGN_STAGES[: DESIGN_STAGES.index(args.stop_after) + 1]
    if args.from_world:
        stages = tuple(stage for stage in stages if stage != "world")
    if args.from_characters:
        stages = tuple(stage for stage in stages if stage not in {"world", "characters"})
    if args.phase == "design" and "characters" in stages and not args.from_world:
        print("[generate-v3] 写人物必须先 --from-world 锁住同一套世界", file=sys.stderr)
        return 2
    if args.phase == "design" and "outline" in stages and not args.from_characters:
        print("[generate-v3] 写章纲必须先 --from-characters 锁住同一套人物", file=sys.stderr)
        return 2
    if args.phase == "design" and not stages:
        print("[generate-v3] 没有可跑的设计段", file=sys.stderr)
        return 2
    if args.dry_run:
        print(
            f"[generate-v3] dry-run phase={args.phase} models={len(selected)} "
            f"jobs={args.jobs} stages={','.join(stages) or '-'} layered=ok"
        )
        return 0

    env = load_env_file(root / args.env)
    env.update(os.environ)
    client = ChatClient.from_config(config, env, provider_id="new-api")
    workers = min(args.jobs, len(selected))
    _log(
        f"[generate-v3] start phase={args.phase} models={len(selected)} "
        f"jobs={workers} stages={','.join(stages)}"
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
