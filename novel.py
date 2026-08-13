#!/usr/bin/env python3
"""Unified command line entry point for the novel benchmark."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Callable, Iterable

from runner import compare_v4, generate, score, score_v4
from runner.llm_api import (
    ANTHROPIC_MESSAGES,
    ChatClient,
    get_model_config,
    load_config,
    load_env_file,
    model_protocol,
)
from scripts import generate_site


ROOT = Path(__file__).resolve().parent
FORWARDED: dict[str, tuple[str, Callable[[Iterable[str] | None], int]]] = {
    "generate": ("V2.1 可恢复小说生成", generate.main),
    "score": ("V3 Sol/Grok/Opus/K3/DeepSeek 评分", score.main),
    "score-v4": ("V4 绝对评分", score_v4.main),
    "compare-v4": ("V4 匿名成对比较", compare_v4.main),
    "site": ("离线构建站点", generate_site.main),
}


def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _load_runtime(config_name: str, env_name: str) -> tuple[dict, ChatClient]:
    config = load_config(_path(config_name))
    env = load_env_file(_path(env_name))
    env.update(os.environ)
    return config, ChatClient.from_config(config, env, provider_id="new-api")


def _models(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="novel.py models", description="检查本地注册表与上游精确模型目录")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config, client = _load_runtime(args.config, args.env)
    live = set(client.list_models())
    generators = [str(item["model"]) for item in config.get("models", [])]
    judges = [str(item["model"]) for item in config.get("judges", [])]
    required = list(dict.fromkeys([*generators, *judges]))
    missing = [model for model in required if model not in live]
    extra = sorted(live - set(required))
    payload = {
        "transport_default": "stream" if client.stream else "non-stream",
        "configured_generators": generators,
        "configured_judges": judges,
        "live_models": sorted(live),
        "missing": missing,
        "unconfigured": extra,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"transport={payload['transport_default']}")
        print(f"generators={len(generators)} judges={len(config.get('judges', []))} live={len(live)}")
        print("missing=" + (", ".join(missing) if missing else "none"))
        print("unconfigured=" + (", ".join(extra) if extra else "none"))
    return 1 if missing else 0


def _probe(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="novel.py probe",
        description="对作者模型执行小型流式/非流式真实请求；both 会产生两次调用",
    )
    parser.add_argument("--model", action="append", required=True, help="生成模型 id 或显式别名；可重复")
    parser.add_argument("--mode", choices=("stream", "non-stream", "both"), default="both")
    parser.add_argument("--prompt", default="只回复：OK")
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.max_tokens <= 0:
        parser.error("--max-tokens 必须是正整数")
    config, client = _load_runtime(args.config, args.env)
    live = set(client.list_models())
    modes = (True, False) if args.mode == "both" else (args.mode == "stream",)
    records: list[dict[str, object]] = []
    had_error = False
    for requested in args.model:
        model_cfg = get_model_config(config, requested)
        wire_model = str(model_cfg["model"])
        if wire_model not in live:
            records.append({"model": requested, "wire_model": wire_model, "status": "missing"})
            had_error = True
            continue
        probe_cfg = dict(model_cfg)
        request_overrides: dict[str, object] = {}
        if model_protocol(probe_cfg) == ANTHROPIC_MESSAGES:
            probe_cfg["protocol_required"] = {"max_tokens": args.max_tokens}
        else:
            request_overrides["max_tokens"] = args.max_tokens
        for use_stream in modes:
            mode = "stream" if use_stream else "non-stream"
            try:
                result = client.complete(
                    probe_cfg,
                    [{"role": "user", "content": args.prompt}],
                    stage="probe",
                    request_overrides=request_overrides,
                    stream=use_stream,
                )
                if str(result.finish_reason or "").strip().lower() != "stop":
                    raise RuntimeError(
                        "探针响应缺少可接受的 stop 终止原因："
                        f"{result.finish_reason or 'missing'}"
                    )
                records.append(
                    {
                        "model": requested,
                        "wire_model": wire_model,
                        "mode": mode,
                        "status": "ok",
                        "finish_reason": result.finish_reason,
                        "characters": len(result.content),
                        "latency_ms": result.latency_ms,
                        "response_model": result.response_model,
                    }
                )
            except Exception as exc:
                records.append(
                    {
                        "model": requested,
                        "wire_model": wire_model,
                        "mode": mode,
                        "status": "error",
                        "error": str(exc),
                    }
                )
                had_error = True
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
    else:
        for item in records:
            detail = " ".join(f"{key}={value}" for key, value in item.items())
            print(detail)
    return 1 if had_error else 0


def _print_help() -> None:
    print("usage: python novel.py <command> [options]\n")
    print("commands:")
    print("  models       检查本地注册表与实时 /v1/models")
    print("  probe        对模型比较流式/非流式小请求")
    for command, (description, _handler) in FORWARDED.items():
        print(f"  {command:<12} {description}")
    print("\n使用 `python novel.py <command> --help` 查看子命令参数。")


def main(argv: list[str] | None = None) -> int:
    # Long model calls should expose runner progress immediately even when this
    # unified entry point is captured by a parent process or CI log collector.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(line_buffering=True)
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        _print_help()
        return 0
    command, rest = args[0], args[1:]
    try:
        if command == "models":
            return _models(rest)
        if command == "probe":
            return _probe(rest)
        forwarded = FORWARDED.get(command)
        if forwarded is None:
            print(f"novel: 未知命令：{command}", file=sys.stderr)
            _print_help()
            return 2
        return forwarded[1](rest)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"novel {command}: ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
