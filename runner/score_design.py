#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Score isolated V3 design tracks, then pick a frozen opening pack."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

try:
    from . import generate as g
    from . import generate_v3 as v3
    from .llm_api import (
        ChatClient,
        LLMAPIError,
        get_judge_config,
        get_model_config,
        load_config,
        load_env_file,
        with_provider_request_defaults,
    )
except ImportError:  # pragma: no cover
    import generate as g  # type: ignore
    import generate_v3 as v3  # type: ignore
    from llm_api import (  # type: ignore
        ChatClient,
        LLMAPIError,
        get_judge_config,
        get_model_config,
        load_config,
        load_env_file,
        with_provider_request_defaults,
    )


SCHEMA = "novel-design-eval.v3"
AGGREGATE_SCHEMA = "novel-design-aggregate.v3"
TRACKS = ("world", "characters", "outline")
BANDS = {
    "world": ("constraint", "institutions", "focus"),
    "characters": ("agency", "differentiation", "playable"),
    "outline": ("incident", "irreversible", "handoff"),
}
# V3 第五席用 glm-5.3，不改 V2.1 冻死的 opus 席。
DEFAULT_JUDGES = ("sol", "grok", "k3", "ds-v4-pro", "glm-5.3")
_PRINT = threading.Lock()


def resolve_v3_judge_config(config: dict[str, Any], judge_id: str) -> dict[str, Any]:
    """Use the V2.1 judge registry when possible; else the generator of the same id."""

    try:
        return get_judge_config(config, judge_id)
    except ValueError:
        pass
    model_cfg = dict(get_model_config(config, judge_id))
    request = dict(model_cfg.get("request") or {})
    request.setdefault("max_tokens", 32768)
    stages = dict(model_cfg.get("stages") or {})
    judge_stage = dict(stages.get("judge") or {})
    judge_stage.setdefault("temperature", 0.2)
    if model_cfg.get("protocol") != "anthropic-messages":
        judge_stage.setdefault("response_format", {"type": "json_object"})
    stages["judge"] = judge_stage
    model_cfg["request"] = request
    model_cfg["stages"] = stages
    return model_cfg


def _log(message: str) -> None:
    with _PRINT:
        print(message, flush=True)


def render_judge_user(template: str, **fields: str) -> str:
    """Fill `{name}` slots without str.format, then unescape doubled braces."""

    text = template
    for key, value in fields.items():
        text = text.replace("{" + key + "}", value)
    return text.replace("{{", "{").replace("}}", "}")


def list_complete_candidates(results_root: Path) -> list[str]:
    names: list[str] = []
    if not results_root.is_dir():
        return names
    for path in sorted(results_root.iterdir()):
        if not path.is_dir() or path.name.startswith("_"):
            continue
        payload = v3.load_design_dir(path)
        if all(track in payload for track in TRACKS):
            names.append(path.name)
    return names


def parse_design_score(text: str, track: str) -> dict[str, Any]:
    raw = g.parse_json_object(text)
    if not isinstance(raw, dict):
        raise v3.OpeningError("评委输出必须是对象")
    bands_raw = raw.get("bands")
    if not isinstance(bands_raw, dict):
        raise v3.OpeningError("缺少 bands")
    expected = BANDS[track]
    if set(bands_raw) != set(expected):
        raise v3.OpeningError(f"bands 必须恰好是 {expected}")
    bands: dict[str, int] = {}
    for key in expected:
        value = bands_raw[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 4:
            raise v3.OpeningError(f"{key} 必须是 0–4 整数")
        bands[key] = value
    comment = raw.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        raise v3.OpeningError("comment 必须非空")
    score = round(sum(bands.values()) / 12 * 100, 1)
    return {"bands": bands, "score": score, "comment": comment.strip()[:240]}


def score_path(model_dir: Path, track: str, judge_id: str) -> Path:
    return model_dir / "scores-design" / track / f"{judge_id}.json"


def try_load_score(path: Path, track: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if raw.get("schema") != SCHEMA or raw.get("track") != track:
        return None
    if not isinstance(raw.get("bands"), dict) or not isinstance(raw.get("score"), (int, float)):
        return None
    return raw


def _complete_score(
    client: ChatClient,
    judge_cfg: dict[str, Any],
    system: str,
    user: str,
    track: str,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, g.MAX_STAGE_ATTEMPTS + 1):
        try:
            result = client.complete(
                judge_cfg,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                stage="judge",
            )
            return parse_design_score(result.content, track)
        except LLMAPIError as exc:
            last_error = exc
            if not g.api_error_is_retryable(exc) or attempt >= g.MAX_STAGE_ATTEMPTS:
                raise
            delay = g.retry_delay_seconds(exc, attempt)
            _log(f"[score-design] retry {track} after {exc} in {delay:.1f}s")
            time.sleep(delay)
        except (v3.OpeningError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt >= g.MAX_STAGE_ATTEMPTS:
                raise v3.OpeningError(f"{track} 解析失败：{exc}") from exc
            _log(f"[score-design] repair {track}: {exc}")
    raise v3.OpeningError(f"{track} 失败：{last_error}")


def score_one(
    *,
    client: ChatClient,
    config: dict[str, Any],
    prompts: dict[str, str],
    direction: str,
    model_dir: Path,
    candidate: str,
    track: str,
    judge_id: str,
) -> str:
    dest = score_path(model_dir, track, judge_id)
    cached = try_load_score(dest, track)
    if cached is not None:
        _log(f"[score-design] {candidate} {track} {judge_id}: cached")
        return "cached"
    artifact = v3.load_design_dir(model_dir)[track]
    user = render_judge_user(
        prompts[f"judge_{track}.md"],
        direction=direction,
        artifact=json.dumps(artifact, ensure_ascii=False, indent=2),
    )
    judge_cfg = with_provider_request_defaults(
        config, resolve_v3_judge_config(config, judge_id)
    )
    parsed = _complete_score(client, judge_cfg, prompts["judge_system.md"], user, track)
    payload = {
        "schema": SCHEMA,
        "benchmark": v3.DEFAULT_BENCHMARK,
        "candidate": candidate,
        "track": track,
        "judge": judge_id,
        **parsed,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    g.atomic_write_json(dest, payload)
    _log(f"[score-design] {candidate} {track} {judge_id}: {parsed['score']}")
    return "scored"


def aggregate_candidate(model_dir: Path, judge_ids: tuple[str, ...]) -> dict[str, Any]:
    tracks: dict[str, Any] = {}
    totals: list[float] = []
    complete = True
    for track in TRACKS:
        scores: list[float] = []
        ballots: dict[str, Any] = {}
        for judge_id in judge_ids:
            raw = try_load_score(score_path(model_dir, track, judge_id), track)
            if raw is None:
                complete = False
                continue
            scores.append(float(raw["score"]))
            ballots[judge_id] = {
                "score": raw["score"],
                "bands": raw["bands"],
                "comment": raw.get("comment", ""),
            }
        if not scores:
            complete = False
            tracks[track] = {"median": None, "n": 0, "judges": ballots}
            continue
        median = round(float(statistics.median(scores)), 1)
        tracks[track] = {"median": median, "n": len(scores), "judges": ballots}
        totals.append(median)
    overall = round(float(statistics.mean(totals)), 1) if len(totals) == 3 else None
    payload = {
        "schema": AGGREGATE_SCHEMA,
        "candidate": model_dir.name,
        "complete": complete and overall is not None,
        "overall": overall,
        "tracks": tracks,
    }
    g.atomic_write_json(model_dir / "scores-design" / "aggregate.json", payload)
    return payload


def pick_winners(aggregates: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [item for item in aggregates if item.get("complete") and item.get("overall") is not None]
    if not complete:
        raise v3.OpeningError("没有完整设计聚合，无法挑选")

    def best_for(track: str) -> str:
        ranked = sorted(
            complete,
            key=lambda item: (
                -float(item["tracks"][track]["median"]),
                -float(item["overall"]),
                item["candidate"],
            ),
        )
        return ranked[0]["candidate"]

    mixed = {
        "world": best_for("world"),
        "characters": best_for("characters"),
        "outline": best_for("outline"),
    }
    package = max(complete, key=lambda item: (float(item["overall"]), item["candidate"]))["candidate"]
    return {"mixed": mixed, "package": package}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="给 V3 设计段打分并挑选冻结包")
    parser.add_argument("--model", action="append", dest="models")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--judge", action="append", dest="judges")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--assemble", action="store_true", help="打完分后按规则冻结")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--benchmark", default=v3.DEFAULT_BENCHMARK)
    args = parser.parse_args(argv)

    root = g.repo_root()
    results_root = root / "results" / args.benchmark
    available = list_complete_candidates(results_root)
    selected = available if args.all else list(args.models or [])
    if not selected:
        print("[score-design] 没有可评的齐套设计", file=sys.stderr)
        return 2
    missing = [name for name in selected if name not in available]
    if missing:
        print("[score-design] 未齐套，跳过：" + ", ".join(missing), file=sys.stderr)
        selected = [name for name in selected if name in available]
    judges = tuple(args.judges or DEFAULT_JUDGES)
    if args.jobs < 1:
        return 2

    prompt_dir = root / "runner" / "prompts" / "v3"
    prompts = {
        name: g.canonical_text((prompt_dir / name).read_bytes().decode("utf-8-sig"))
        for name in (
            "judge_system.md",
            "judge_world.md",
            "judge_characters.md",
            "judge_outline.md",
        )
    }
    direction = g.canonical_text(
        (root / "benchmark" / args.benchmark / "direction.md").read_bytes().decode("utf-8-sig")
    )
    tasks = [
        (candidate, track, judge_id)
        for candidate in selected
        for track in TRACKS
        for judge_id in judges
    ]
    if args.dry_run:
        print(
            f"[score-design] dry-run candidates={len(selected)} "
            f"judges={','.join(judges)} tasks={len(tasks)} jobs={args.jobs}"
        )
        return 0

    config = load_config(root / args.config)
    env = load_env_file(root / args.env)
    env.update(os.environ)
    client = ChatClient.from_config(config, env, provider_id="new-api")
    workers = min(args.jobs, len(tasks))
    _log(f"[score-design] start tasks={len(tasks)} jobs={workers}")
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                score_one,
                client=client,
                config=config,
                prompts=prompts,
                direction=direction,
                model_dir=results_root / candidate,
                candidate=candidate,
                track=track,
                judge_id=judge_id,
            ): f"{candidate}/{track}/{judge_id}"
            for candidate, track, judge_id in tasks
        }
        for future in as_completed(futures):
            label = futures[future]
            try:
                future.result()
            except Exception as exc:
                failures.append(f"{label}: {exc}")
                _log(f"[score-design] FAIL {label}: {exc}")

    aggregates = [
        aggregate_candidate(results_root / name, DEFAULT_JUDGES) for name in selected
    ]
    complete = [item for item in aggregates if item.get("complete")]
    _log(f"[score-design] aggregates complete={len(complete)}/{len(selected)}")
    for item in sorted(complete, key=lambda row: (-float(row["overall"]), row["candidate"])):
        tracks = " ".join(
            f"{track[0]}={item['tracks'][track]['median']}" for track in TRACKS
        )
        _log(f"[score-design] {item['overall']:5.1f} {item['candidate']:22} {tracks}")

    if not complete:
        _log("[score-design] 没有完整聚合")
        return 1
    winners = pick_winners(complete)
    mixed = winners["mixed"]
    _log(
        f"[score-design] mixed world={mixed['world']} "
        f"characters={mixed['characters']} outline={mixed['outline']}"
    )
    _log(f"[score-design] best package={winners['package']}")
    if args.assemble:
        try:
            pack = v3.assemble_frozen(
                results_root,
                root / "benchmark" / args.benchmark / "frozen",
                world_model=mixed["world"],
                characters_model=mixed["characters"],
                outline_model=mixed["outline"],
            )
            chosen = "mixed"
        except v3.OpeningError as exc:
            _log(f"[score-design] mixed incompatible ({exc}); fall back to package")
            pack = v3.assemble_frozen(
                results_root,
                root / "benchmark" / args.benchmark / "frozen",
                world_model=winners["package"],
                characters_model=winners["package"],
                outline_model=winners["package"],
            )
            chosen = "package"
        _log(f"[score-design] froze {chosen} {pack['sources']}")
    if failures:
        _log(f"[score-design] failed {len(failures)}/{len(tasks)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
