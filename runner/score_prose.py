#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Score V3 opening prose against the frozen pack. Style and scene only."""

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
        load_config,
        load_env_file,
        with_provider_request_defaults,
    )
    from .score_design import DEFAULT_JUDGES, render_judge_user
except ImportError:  # pragma: no cover
    import generate as g  # type: ignore
    import generate_v3 as v3  # type: ignore
    from llm_api import (  # type: ignore
        ChatClient,
        LLMAPIError,
        get_judge_config,
        load_config,
        load_env_file,
        with_provider_request_defaults,
    )
    from score_design import DEFAULT_JUDGES, render_judge_user  # type: ignore


SCHEMA = "novel-prose-eval.v3"
AGGREGATE_SCHEMA = "novel-prose-aggregate.v3"
BANDS = ("naturalness", "voice", "scene", "continuity")
_PRINT = threading.Lock()


def _log(message: str) -> None:
    with _PRINT:
        print(message, flush=True)


def parse_prose_score(text: str) -> dict[str, Any]:
    raw = g.parse_json_object(text)
    if not isinstance(raw, dict):
        raise v3.OpeningError("评委输出必须是对象")
    bands_raw = raw.get("bands")
    if not isinstance(bands_raw, dict):
        raise v3.OpeningError("缺少 bands")
    if set(bands_raw) != set(BANDS):
        raise v3.OpeningError(f"bands 必须恰好是 {BANDS}")
    bands: dict[str, int] = {}
    for key in BANDS:
        value = bands_raw[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 4:
            raise v3.OpeningError(f"{key} 必须是 0–4 整数")
        bands[key] = value
    comment = raw.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        raise v3.OpeningError("comment 必须非空")
    score = round(sum(bands.values()) / 16 * 100, 1)
    return {"bands": bands, "score": score, "comment": comment.strip()[:240]}


def score_path(model_dir: Path, judge_id: str) -> Path:
    return model_dir / "scores-prose" / f"{judge_id}.json"


def try_load_score(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if raw.get("schema") != SCHEMA:
        return None
    if not isinstance(raw.get("bands"), dict) or not isinstance(raw.get("score"), (int, float)):
        return None
    return raw


def list_complete_prose(results_root: Path) -> list[str]:
    names: list[str] = []
    if not results_root.is_dir():
        return names
    for path in sorted(results_root.iterdir()):
        if not path.is_dir() or path.name.startswith("_"):
            continue
        try:
            prose = (
                g.read_json(path / "prose.json") if (path / "prose.json").is_file() else {}
            )
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        novel = path / "novel.md"
        if (
            prose.get("schema") == v3.PROSE_SCHEMA
            and prose.get("status") == "complete"
            and prose.get("model_id") == path.name
            and novel.is_file()
            and novel.read_text(encoding="utf-8-sig").strip()
        ):
            names.append(path.name)
    return names


def _complete_score(
    client: ChatClient,
    judge_cfg: dict[str, Any],
    system: str,
    user: str,
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
            return parse_prose_score(result.content)
        except LLMAPIError as exc:
            last_error = exc
            if not g.api_error_is_retryable(exc) or attempt >= g.MAX_STAGE_ATTEMPTS:
                raise
            delay = g.retry_delay_seconds(exc, attempt)
            _log(f"[score-prose] retry after {exc} in {delay:.1f}s")
            time.sleep(delay)
        except (v3.OpeningError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt >= g.MAX_STAGE_ATTEMPTS:
                raise v3.OpeningError(f"prose 解析失败：{exc}") from exc
            _log(f"[score-prose] repair: {exc}")
    raise v3.OpeningError(f"prose 失败：{last_error}")


def score_one(
    *,
    client: ChatClient,
    config: dict[str, Any],
    prompts: dict[str, str],
    direction: str,
    model_dir: Path,
    candidate: str,
    judge_id: str,
    frozen_sha256: str,
    input_hash: str,
) -> str:
    dest = score_path(model_dir, judge_id)
    cached = try_load_score(dest)
    if (
        cached is not None
        and cached.get("frozen_sha256") == frozen_sha256
        and cached.get("input_hash") == input_hash
    ):
        _log(f"[score-prose] {candidate} {judge_id}: cached")
        return "cached"
    novel = g.canonical_text((model_dir / "novel.md").read_bytes().decode("utf-8-sig"))
    user = render_judge_user(
        prompts["judge_prose.md"],
        direction=direction,
        artifact=novel,
    )
    judge_cfg = with_provider_request_defaults(config, get_judge_config(config, judge_id))
    parsed = _complete_score(client, judge_cfg, prompts["judge_system.md"], user)
    payload = {
        "schema": SCHEMA,
        "benchmark": v3.DEFAULT_BENCHMARK,
        "candidate": candidate,
        "judge": judge_id,
        "frozen_sha256": frozen_sha256,
        "input_hash": input_hash,
        **parsed,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    g.atomic_write_json(dest, payload)
    _log(f"[score-prose] {candidate} {judge_id}: {parsed['score']}")
    return "scored"


def aggregate_candidate(
    model_dir: Path,
    judge_ids: tuple[str, ...],
    *,
    frozen_sha256: str,
    input_hash: str,
) -> dict[str, Any]:
    scores: list[float] = []
    ballots: dict[str, Any] = {}
    band_values: dict[str, list[int]] = {key: [] for key in BANDS}
    for judge_id in judge_ids:
        raw = try_load_score(score_path(model_dir, judge_id))
        if (
            raw is None
            or raw.get("frozen_sha256") != frozen_sha256
            or raw.get("input_hash") != input_hash
        ):
            continue
        scores.append(float(raw["score"]))
        ballots[judge_id] = {
            "score": raw["score"],
            "bands": raw["bands"],
            "comment": raw.get("comment", ""),
        }
        for key in BANDS:
            band_values[key].append(int(raw["bands"][key]))
    complete = len(scores) == len(judge_ids)
    overall = round(float(statistics.median(scores)), 1) if scores else None
    bands = {
        key: {
            "median": round(float(statistics.median(values)), 1) if values else None,
            "n": len(values),
        }
        for key, values in band_values.items()
    }
    payload = {
        "schema": AGGREGATE_SCHEMA,
        "candidate": model_dir.name,
        "complete": complete and overall is not None,
        "overall": overall,
        "frozen_sha256": frozen_sha256,
        "input_hash": input_hash,
        "n": len(scores),
        "bands": bands,
        "judges": ballots,
    }
    dest = model_dir / "scores-prose" / "aggregate.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    g.atomic_write_json(dest, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="给 V3 开局正文打文风与场景分")
    parser.add_argument("--model", action="append", dest="models")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--judge", action="append", dest="judges")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--benchmark", default=v3.DEFAULT_BENCHMARK)
    args = parser.parse_args(argv)

    root = g.repo_root()
    results_root = root / "results" / args.benchmark
    pack_path = root / "benchmark" / args.benchmark / "frozen" / "pack.json"
    if not pack_path.is_file():
        print("[score-prose] 没有冻结包", file=sys.stderr)
        return 2
    available = list_complete_prose(results_root)
    selected = available if args.all else list(args.models or [])
    if not selected:
        print("[score-prose] 没有可评的完整正文", file=sys.stderr)
        return 2
    missing = [name for name in selected if name not in available]
    if missing:
        print("[score-prose] 未完成，跳过：" + ", ".join(missing), file=sys.stderr)
        selected = [name for name in selected if name in available]
    judges = tuple(args.judges or DEFAULT_JUDGES)
    if args.jobs < 1 or not selected:
        return 2

    prompt_dir = root / "runner" / "prompts" / "v3"
    prompts = {
        name: g.canonical_text((prompt_dir / name).read_bytes().decode("utf-8-sig"))
        for name in ("judge_system.md", "judge_prose.md")
    }
    direction = g.canonical_text(
        (root / "benchmark" / args.benchmark / "direction.md").read_bytes().decode("utf-8-sig")
    )
    frozen_sha256 = g.sha256_file(pack_path)
    tasks = [(candidate, judge_id) for candidate in selected for judge_id in judges]
    if args.dry_run:
        print(
            f"[score-prose] dry-run candidates={len(selected)} "
            f"judges={','.join(judges)} tasks={len(tasks)} jobs={args.jobs}"
        )
        return 0

    config = load_config(root / args.config)
    env = load_env_file(root / args.env)
    env.update(os.environ)
    client = ChatClient.from_config(config, env, provider_id="new-api")
    workers = min(args.jobs, len(tasks))
    _log(f"[score-prose] start tasks={len(tasks)} jobs={workers}")
    failures: list[str] = []
    hashes = {
        name: g.sha256_file(results_root / name / "novel.md") for name in selected
    }
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
                judge_id=judge_id,
                frozen_sha256=frozen_sha256,
                input_hash=hashes[candidate],
            ): f"{candidate}/{judge_id}"
            for candidate, judge_id in tasks
        }
        for future in as_completed(futures):
            label = futures[future]
            try:
                future.result()
            except Exception as exc:
                failures.append(f"{label}: {exc}")
                _log(f"[score-prose] FAIL {label}: {exc}")

    aggregates = [
        aggregate_candidate(
            results_root / name,
            judges,
            frozen_sha256=frozen_sha256,
            input_hash=hashes[name],
        )
        for name in selected
    ]
    complete = [item for item in aggregates if item.get("complete")]
    _log(f"[score-prose] aggregates complete={len(complete)}/{len(selected)}")
    for item in sorted(
        complete, key=lambda row: (-float(row["overall"]), row["candidate"])
    ):
        _log(f"[score-prose] {item['overall']:5.1f} {item['candidate']}")
    if failures:
        _log(f"[score-prose] failed {len(failures)}/{len(tasks)}")
        return 1
    if not complete:
        _log("[score-prose] 没有完整聚合")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
