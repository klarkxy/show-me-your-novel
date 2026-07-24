from __future__ import annotations

import copy
import json
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


RUNNER_DIR = Path(__file__).resolve().parents[1] / "runner"
ROOT = Path(__file__).resolve().parents[1]
if str(RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(RUNNER_DIR))

import score  # noqa: E402
from scripts.generate_site import build_score_expectations  # noqa: E402


def _write_v2_candidate(root: Path, candidate: str = "candidate-a") -> Path:
    benchmark_dir = root / "benchmark" / "reform-era"
    candidate_dir = root / "results" / "reform-era" / candidate
    benchmark_dir.mkdir(parents=True, exist_ok=True)
    candidate_dir.mkdir(parents=True)
    (benchmark_dir / "direction.md").write_text("写一部长篇改革题材小说。", encoding="utf-8")
    (candidate_dir / "book.json").write_text(
        json.dumps({"title": "潮起", "model": "SECRET-MODEL"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (candidate_dir / "macro_outline.json").write_text(
        json.dumps({"acts": ["起", "承", "转", "合"], "provider": "SECRET-PROVIDER"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (candidate_dir / "opening_outline.json").write_text(
        json.dumps({"chapters": [{"title": "第一章"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    novel = "第一章\n" + ("这是完整正文，不得截断。" * 1000) + "\n全文最后一句。"
    (candidate_dir / "novel.md").write_text(novel, encoding="utf-8")
    (candidate_dir / "manifest.json").write_text(
        json.dumps({"status": "completed", "model": "SECRET-MODEL"}),
        encoding="utf-8",
    )
    return candidate_dir


def _submission(tmp_path: Path) -> score.Submission:
    _write_v2_candidate(tmp_path)
    return score.load_submission(tmp_path, "reform-era", "candidate-a")


def _judge_cfg(model: str) -> dict:
    return {"id": model, "model": model, "provider": "new-api"}


def _dimensions(value: int | float = 80, ai_flavor: int | float = 20) -> dict:
    return {
        spec.key: {
            "score": ai_flavor if spec.key == "ai_flavor" else value,
            "comment": f"{spec.label} 点评",
        }
        for spec in score.DIMENSION_SPECS
    }


def _score_response(
    value: int | float = 80, ai_flavor: int | float = 20
) -> str:
    return json.dumps(
        {"dimensions": _dimensions(value, ai_flavor)},
        ensure_ascii=False,
    )


def test_configured_wire_models_covers_generators_and_fixed_judges() -> None:
    cfg = {
        "models": [
            _judge_cfg("generator-a"),
            _judge_cfg("sol-wire"),
            _judge_cfg("grok-4.5"),
            _judge_cfg("kimi-wire"),
        ]
    }
    judges = {
        "sol": {"model_cfg": _judge_cfg("sol-wire")},
        "grok": {"model_cfg": _judge_cfg("grok-4.5")},
        "kimi": {"model_cfg": _judge_cfg("kimi-wire")},
    }

    assert score.configured_wire_models(cfg, judges) == (
        "generator-a",
        "sol-wire",
        "grok-4.5",
        "kimi-wire",
    )


def test_grok_judge_supplies_disabled_tool_required_by_gateway_on_wire() -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        @staticmethod
        def read() -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": '{"score": 80}'},
                            "finish_reason": "stop",
                        }
                    ]
                }
            ).encode("utf-8")

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    cfg = score.load_config(ROOT / "config.yaml")
    grok = score.resolve_judge_configs(cfg)["grok"]
    client = score.ChatClient.from_config(
        cfg,
        {"API_URL": "https://gateway.test/v1", "API_KEY": "fake"},
        urlopen=opener,
    )
    client.complete(
        grok["model_cfg"],
        [{"role": "user", "content": "judge"}],
        stage="judge",
        request_overrides=grok["request_overrides"],
    )

    assert captured["url"] == "https://gateway.test/v1/chat/completions"
    assert captured["payload"] == {
        "model": "grok-4.5",
        "messages": [{"role": "user", "content": "judge"}],
        "max_tokens": 4096,
        "temperature": 0.2,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "unused_judge_tool",
                    "description": "Never call this tool.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            }
        ],
        "tool_choice": "none",
    }


def test_site_score_fingerprints_match_scoring_writer() -> None:
    root = Path(__file__).resolve().parents[1]
    config_path = root / "config.yaml"
    cfg = score.load_config(config_path)
    resolved = score.resolve_judge_configs(cfg)
    site_expectations = build_score_expectations(config_path, cfg)
    rubric = score.load_system_prompt(root)

    for judge_id in score.JUDGE_IDS:
        model_cfg = resolved[judge_id]["model_cfg"]
        request_overrides = resolved[judge_id]["request_overrides"]
        assert site_expectations[judge_id] == {
            "rubric_hash": hashlib.sha256(rubric.encode("utf-8")).hexdigest(),
            "judge_config_sha256": score.judge_config_sha256(
                model_cfg, request_overrides
            ),
            "requested_model": model_cfg["model"],
        }


def test_score_runtime_rejects_fixed_judge_substitution() -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = score.load_config(root / "config.yaml")
    cfg["judges"][0]["model"] = "silent-substitute"

    with pytest.raises(score.ScoreError, match="不允许静默替换"):
        score.resolve_judge_configs(cfg)


def test_judge_fingerprint_changes_with_provider_request_defaults() -> None:
    root = Path(__file__).resolve().parents[1]
    config_path = root / "config.yaml"
    base_cfg = score.load_config(config_path)
    changed_cfg = score.load_config(config_path)
    changed_cfg["providers"]["new-api"]["request_defaults"] = {"top_p": 0.75}

    base = score.resolve_judge_configs(base_cfg)["sol"]
    changed = score.resolve_judge_configs(changed_cfg)["sol"]
    assert score.judge_config_sha256(
        base["model_cfg"], base["request_overrides"]
    ) != score.judge_config_sha256(
        changed["model_cfg"], changed["request_overrides"]
    )
    site_expected = build_score_expectations(config_path, changed_cfg)["sol"]
    assert site_expected["judge_config_sha256"] == score.judge_config_sha256(
        changed["model_cfg"], changed["request_overrides"]
    )


def test_kimi_output_budget_change_only_invalidates_kimi_fingerprint() -> None:
    config_path = ROOT / "config.yaml"
    base_cfg = score.load_config(config_path)
    changed_cfg = copy.deepcopy(base_cfg)
    kimi = next(
        judge for judge in changed_cfg["judges"] if judge["id"] == "kimi"
    )
    kimi["request"] = {"max_tokens": 16_384}

    base = score.resolve_judge_configs(base_cfg)
    changed = score.resolve_judge_configs(changed_cfg)
    fingerprints = {
        judge_id: score.judge_config_sha256(
            base[judge_id]["model_cfg"],
            base[judge_id]["request_overrides"],
        )
        for judge_id in score.JUDGE_IDS
    }
    changed_fingerprints = {
        judge_id: score.judge_config_sha256(
            changed[judge_id]["model_cfg"],
            changed[judge_id]["request_overrides"],
        )
        for judge_id in score.JUDGE_IDS
    }

    assert changed_fingerprints["sol"] == fingerprints["sol"]
    assert changed_fingerprints["grok"] == fingerprints["grok"]
    assert changed_fingerprints["kimi"] != fingerprints["kimi"]


def test_full_submission_is_anonymous_and_unabridged(tmp_path: Path) -> None:
    submission = _submission(tmp_path)

    assert "写一部长篇改革题材小说" in submission.user_content
    assert '"title": "潮起"' in submission.user_content
    assert '"acts"' in submission.user_content
    assert '<opening_outline>' in submission.user_content
    assert "全文最后一句" in submission.user_content
    assert len(submission.user_content) > 6000
    assert "SECRET-MODEL" not in submission.user_content
    assert "SECRET-PROVIDER" not in submission.user_content
    assert "candidate-a" not in submission.user_content
    assert "manifest" not in submission.user_content


def test_in_progress_manifest_is_rejected(tmp_path: Path) -> None:
    candidate_dir = _write_v2_candidate(tmp_path)
    (candidate_dir / "manifest.json").write_text('{"status":"in_progress"}', encoding="utf-8")

    with pytest.raises(score.ScoreError, match="尚未完成"):
        score.load_submission(tmp_path, "reform-era", "candidate-a")


def test_system_prompt_renders_canonical_dimension_contract() -> None:
    prompt = score.load_system_prompt(ROOT)

    assert "{{DIMENSION_SPECS}}" not in prompt
    assert [spec.key for spec in score.DIMENSION_SPECS] == list(
        score.DIMENSION_KEYS
    )
    assert sum(spec.weight for spec in score.DIMENSION_SPECS) == pytest.approx(1)
    for spec in score.DIMENSION_SPECS:
        assert f"`{spec.key}`" in prompt


def test_parse_score_response_normalises_decimals_and_comments() -> None:
    payload = {"dimensions": _dimensions(82.25, 17)}
    payload["dimensions"]["characters"]["comment"] = "  人物可信，\n  关系仍可加深。 "
    parsed = score.parse_score_response(
        "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    )

    assert tuple(parsed["dimensions"]) == score.DIMENSION_KEYS
    assert parsed["dimensions"]["theme_fulfillment"]["score"] == 82.3
    assert type(parsed["dimensions"]["theme_fulfillment"]["score"]) is float
    assert parsed["dimensions"]["ai_flavor"]["score"] == 17.0
    assert (
        parsed["dimensions"]["characters"]["comment"]
        == "人物可信， 关系仍可加深。"
    )


def test_parse_score_response_repairs_only_missing_trailing_container_braces() -> None:
    payload = {"dimensions": _dimensions(82.0, 17.0)}
    complete = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    assert score.parse_score_response(complete[:-1]) == score.parse_score_response(
        complete
    )
    assert score.parse_score_response(complete[:-2]) == score.parse_score_response(
        complete
    )

    truncated_string = complete[: complete.rfind('"')]
    with pytest.raises(score.ScoreError, match="JSON 解析失败"):
        score.parse_score_response(truncated_string)

    comment_start = complete.index('"comment":"') + len('"comment":"')
    earlier_syntax_error = (
        complete[: comment_start + 1] + '"' + complete[comment_start + 1 :]
    )
    with pytest.raises(score.ScoreError, match="JSON 解析失败"):
        score.parse_score_response(earlier_syntax_error)


def test_parse_score_response_rejects_invalid_schema() -> None:
    invalid_payloads: list[dict] = []

    invalid_payloads.append(
        {"score": 80, "ai_flavor": 20, "comment": "旧版结构"}
    )

    missing = {"dimensions": _dimensions()}
    missing["dimensions"].pop("characters")
    invalid_payloads.append(missing)

    extra_dimension = {"dimensions": _dimensions()}
    extra_dimension["dimensions"]["extra"] = {"score": 80, "comment": "额外"}
    invalid_payloads.append(extra_dimension)

    boolean_score = {"dimensions": _dimensions()}
    boolean_score["dimensions"]["characters"]["score"] = True
    invalid_payloads.append(boolean_score)

    nonfinite = {"dimensions": _dimensions()}
    nonfinite["dimensions"]["characters"]["score"] = float("nan")
    invalid_payloads.append(nonfinite)

    out_of_range = {"dimensions": _dimensions()}
    out_of_range["dimensions"]["characters"]["score"] = 100.1
    invalid_payloads.append(out_of_range)

    extra_entry_field = {"dimensions": _dimensions()}
    extra_entry_field["dimensions"]["characters"]["extra"] = 1
    invalid_payloads.append(extra_entry_field)

    empty_comment = {"dimensions": _dimensions()}
    empty_comment["dimensions"]["characters"]["comment"] = " \n "
    invalid_payloads.append(empty_comment)

    long_comment = {"dimensions": _dimensions()}
    long_comment["dimensions"]["characters"]["comment"] = "评" * 241
    invalid_payloads.append(long_comment)

    for payload in invalid_payloads:
        with pytest.raises(score.ScoreError):
            score.parse_score_response(
                json.dumps(payload, ensure_ascii=False, allow_nan=True)
            )


def test_cache_key_tracks_content_rubric_model_and_parameters(tmp_path: Path) -> None:
    submission = _submission(tmp_path)
    base = score.score_cache_key(submission, "rubric-a", "sol", _judge_cfg("sol-model"), None)

    assert base == score.score_cache_key(
        submission, "rubric-a", "sol", _judge_cfg("sol-model"), None
    )
    assert base != score.score_cache_key(
        submission, "rubric-b", "sol", _judge_cfg("sol-model"), None
    )
    assert base != score.score_cache_key(
        submission, "rubric-a", "sol", _judge_cfg("sol-model-v2"), None
    )
    assert base != score.score_cache_key(
        submission,
        "rubric-a",
        "sol",
        _judge_cfg("sol-model"),
        {"temperature": 0.2},
    )

    changed = score.dataclasses.replace(submission, input_hash="different")
    assert base != score.score_cache_key(changed, "rubric-a", "sol", _judge_cfg("sol-model"), None)


class FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, model_cfg, messages, *, stage, request_overrides=None):
        self.calls += 1
        assert stage == "judge"
        assert "全文最后一句" in messages[1]["content"]
        return SimpleNamespace(
            content=_score_response(84, 21),
            reasoning_content="private reasoning",
            usage={"prompt_tokens": 1000, "completion_tokens": 100},
            requested_model=model_cfg["model"],
            response_model=model_cfg["model"],
            finish_reason="stop",
            response_id="response-1",
            latency_ms=123,
            raw_response={"id": "response-1"},
        )


def _audit_event_paths(
    root: Path,
    *,
    judge_id: str = "sol",
) -> list[Path]:
    return sorted(
        (
            root
            / "work"
            / "scoring"
            / "reform-era"
            / "candidate-a"
            / judge_id
        ).glob("*.json")
    )


def test_evaluate_judge_writes_public_and_private_then_hits_cache(tmp_path: Path) -> None:
    submission = _submission(tmp_path)
    client = FakeClient()
    model_cfg = _judge_cfg("sol-model")

    status, public = score.evaluate_judge(
        root=tmp_path,
        submission=submission,
        judge_id="sol",
        model_cfg=model_cfg,
        request_overrides=None,
        system_prompt="rubric",
        client=client,
    )
    assert status == "scored"
    assert public["dimensions"]["characters"]["score"] == 84.0
    assert "score" not in public
    assert "ai_flavor" not in public
    assert "comment" not in public
    assert client.calls == 1

    public_path = submission.candidate_dir / "scores" / "sol.json"
    diagnostic_paths = _audit_event_paths(tmp_path)
    assert len(diagnostic_paths) == 1
    public_disk = json.loads(public_path.read_text(encoding="utf-8"))
    private_disk = json.loads(diagnostic_paths[0].read_text(encoding="utf-8"))
    assert "usage" not in public_disk
    assert "raw_response" not in public_disk
    assert public_disk["judge_config_sha256"] == score.judge_config_sha256(
        model_cfg, None
    )
    assert private_disk["usage"]["prompt_tokens"] == 1000
    assert private_disk["reasoning"] == "private reasoning"

    status, cached = score.evaluate_judge(
        root=tmp_path,
        submission=submission,
        judge_id="sol",
        model_cfg=model_cfg,
        request_overrides=None,
        system_prompt="rubric",
        client=client,
    )
    assert status == "cached"
    assert cached == public_disk
    assert client.calls == 1
    assert _audit_event_paths(tmp_path) == diagnostic_paths


def test_evaluate_judge_rejects_nonempty_truncated_json(tmp_path: Path) -> None:
    submission = _submission(tmp_path)

    class TruncatedClient(FakeClient):
        def complete(self, model_cfg, messages, *, stage, request_overrides=None):
            result = super().complete(
                model_cfg,
                messages,
                stage=stage,
                request_overrides=request_overrides,
            )
            result.finish_reason = "length"
            return result

    client = TruncatedClient()
    with pytest.raises(score.ScoreError, match="拒绝截断评分"):
        score.evaluate_judge(
            root=tmp_path,
            submission=submission,
            judge_id="sol",
            model_cfg=_judge_cfg("sol-model"),
            request_overrides=None,
            system_prompt="rubric",
            client=client,
        )
    assert not (submission.candidate_dir / "scores" / "sol.json").exists()
    diagnostic_paths = _audit_event_paths(tmp_path)
    assert len(diagnostic_paths) == 1
    diagnostic = json.loads(diagnostic_paths[0].read_text(encoding="utf-8"))
    assert diagnostic["finish_reason"] == "length"


def test_llm_api_error_with_raw_response_is_audited_without_public_score(
    tmp_path: Path,
) -> None:
    submission = _submission(tmp_path)
    api_error = score.LLMAPIError(
        "LLM API 返回空内容（finish_reason=length，reasoning=present）",
        raw_response={
            "id": "response-error",
            "model": "sol-response-model",
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "content": "",
                        "reasoning_content": "PRIVATE TRACE",
                    },
                }
            ],
            "usage": {"completion_tokens": 4096},
        },
    )

    class ErrorClient:
        calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            raise api_error

    client = ErrorClient()
    with pytest.raises(score.LLMAPIError) as raised:
        score.evaluate_judge(
            root=tmp_path,
            submission=submission,
            judge_id="sol",
            model_cfg=_judge_cfg("sol-model"),
            request_overrides=None,
            system_prompt="rubric",
            client=client,
        )

    assert raised.value is api_error
    assert not (submission.candidate_dir / "scores" / "sol.json").exists()
    paths = _audit_event_paths(tmp_path)
    assert len(paths) == 1
    event = json.loads(paths[0].read_text(encoding="utf-8"))
    assert event["finish_reason"] == "length"
    assert event["usage"] == {"completion_tokens": 4096}
    assert event["reasoning"] == "PRIVATE TRACE"
    assert event["raw_response"]["id"] == "response-error"
    assert event["content"] == ""
    assert event["parse_error"] == str(api_error)
    assert "PRIVATE TRACE" not in event["parse_error"]


def test_repeated_api_failures_append_unique_audit_events(tmp_path: Path) -> None:
    submission = _submission(tmp_path)

    class ErrorClient:
        calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            raise score.LLMAPIError(
                "safe failure",
                raw_response={
                    "model": "sol-model",
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": ""},
                        }
                    ],
                },
            )

    client = ErrorClient()
    for _ in range(2):
        with pytest.raises(score.LLMAPIError, match="safe failure"):
            score.evaluate_judge(
                root=tmp_path,
                submission=submission,
                judge_id="sol",
                model_cfg=_judge_cfg("sol-model"),
                request_overrides=None,
                system_prompt="rubric",
                client=client,
            )

    paths = _audit_event_paths(tmp_path)
    assert client.calls == 2
    assert len(paths) == 2
    assert paths[0].name != paths[1].name
    assert all(
        json.loads(path.read_text(encoding="utf-8"))["parse_error"]
        == "safe failure"
        for path in paths
    )


def test_missing_public_score_recovers_from_success_event_without_api_call(
    tmp_path: Path,
) -> None:
    submission = _submission(tmp_path)
    client = FakeClient()
    kwargs = {
        "root": tmp_path,
        "submission": submission,
        "judge_id": "sol",
        "model_cfg": _judge_cfg("sol-model"),
        "request_overrides": None,
        "system_prompt": "rubric",
        "client": client,
    }
    status, expected = score.evaluate_judge(**kwargs)
    assert status == "scored"
    event_paths = _audit_event_paths(tmp_path)

    public_path = submission.candidate_dir / "scores" / "sol.json"
    public_path.unlink()
    status, recovered = score.evaluate_judge(**kwargs)

    assert status == "recovered"
    assert recovered == expected
    assert client.calls == 1
    assert _audit_event_paths(tmp_path) == event_paths
    public = json.loads(public_path.read_text(encoding="utf-8"))
    assert public == expected
    assert not {
        "usage",
        "reasoning",
        "reasoning_content",
        "raw_response",
        "content",
        "parse_error",
    }.intersection(public)


@pytest.mark.parametrize(
    "invalid_kind",
    ("wrong-cache", "length", "invalid-content", "old-schema"),
)
def test_invalid_private_events_never_recover(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    submission = _submission(tmp_path)
    model_cfg = _judge_cfg("sol-model")
    identity = score.public_score_identity(
        submission, "sol", model_cfg, "rubric", None
    )
    cache_key = score.score_cache_key(
        submission, "rubric", "sol", model_cfg, None
    )
    event = {
        **identity,
        "cache_key": cache_key,
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "response_model": "sol-model",
        "finish_reason": "stop",
        "usage": {},
        "reasoning": "",
        "raw_response": {},
        "content": _score_response(81, 19),
        "parse_error": None,
    }
    if invalid_kind == "wrong-cache":
        event["cache_key"] = "different-cache-key"
    elif invalid_kind == "length":
        event["finish_reason"] = "length"
    elif invalid_kind == "invalid-content":
        event["content"] = '{"dimensions": {}}'
    elif invalid_kind == "old-schema":
        event["schema"] = "novel-eval.v2"

    event_dir = (
        tmp_path
        / "work"
        / "scoring"
        / "reform-era"
        / "candidate-a"
        / "sol"
    )
    event_dir.mkdir(parents=True)
    (event_dir / "20260101T000000.000000Z-invalid.json").write_text(
        json.dumps(event, ensure_ascii=False),
        encoding="utf-8",
    )

    client = FakeClient()
    status, _public = score.evaluate_judge(
        root=tmp_path,
        submission=submission,
        judge_id="sol",
        model_cfg=model_cfg,
        request_overrides=None,
        system_prompt="rubric",
        client=client,
    )
    assert status == "scored"
    assert client.calls == 1
    assert len(_audit_event_paths(tmp_path)) == 2


def _write_score(
    submission: score.Submission,
    judge_id: str,
    cache_key: str,
    value: int | float,
    ai_flavor: int | float,
    identity: dict,
) -> None:
    path = submission.candidate_dir / "scores" / f"{judge_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                **identity,
                "response_model": identity["requested_model"],
                "cache_key": cache_key,
                "dimensions": _dimensions(float(value), float(ai_flavor)),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_aggregate_requires_all_three_fresh_judges(tmp_path: Path) -> None:
    submission = _submission(tmp_path)
    keys = {judge_id: f"key-{judge_id}" for judge_id in score.JUDGE_IDS}
    identities = {
        judge_id: score.public_score_identity(
            submission,
            judge_id,
            _judge_cfg(f"{judge_id}-model"),
            "rubric",
            None,
        )
        for judge_id in score.JUDGE_IDS
    }
    _write_score(submission, "sol", keys["sol"], 90, 10, identities["sol"])
    _write_score(submission, "grok", keys["grok"], 80, 20, identities["grok"])

    incomplete = score.aggregate_scores(submission, keys, identities)
    assert incomplete["status"] == "incomplete"
    assert incomplete["eligible_for_ranking"] is False
    assert incomplete["dimensions"] == {}
    assert incomplete["overall_score"] is None
    assert set(incomplete["judges"]) == {"sol", "grok"}

    _write_score(submission, "kimi", keys["kimi"], 85, 15, identities["kimi"])
    complete = score.aggregate_scores(submission, keys, identities)
    assert complete["status"] == "complete"
    assert complete["eligible_for_ranking"] is True
    assert complete["overall_score"] == 85.0
    assert complete["dimensions"]["characters"] == {
        "label": "人物与关系",
        "weight": 0.15,
        "higher_is_better": True,
        "median": 85.0,
        "min": 80.0,
        "max": 90.0,
    }
    assert complete["dimensions"]["ai_flavor"]["median"] == 15.0
    assert complete["judges"]["sol"]["dimensions"]["characters"]["score"] == 90.0

    # A stale result must invalidate the complete aggregate.
    keys["kimi"] = "new-key-kimi"
    stale = score.aggregate_scores(submission, keys, identities)
    assert stale["status"] == "incomplete"
    assert stale["dimensions"] == {}
    assert stale["overall_score"] is None


def test_dimension_helpers_use_median_direction_and_half_up_rounding() -> None:
    judge_dimensions = {
        "sol": _dimensions(90, 10),
        "grok": _dimensions(40, 90),
        "kimi": _dimensions(80, 20),
    }
    aggregate = score.aggregate_dimension_scores(judge_dimensions)

    assert aggregate["plot_causality"]["median"] == 80.0
    assert aggregate["plot_causality"]["min"] == 40.0
    assert aggregate["plot_causality"]["max"] == 90.0
    assert aggregate["ai_flavor"]["median"] == 20.0
    assert score.dimension_radar_value("ai_flavor", 20) == 80.0
    assert score.dimension_radar_value("characters", 82.25) == 82.3

    medians = {
        "theme_fulfillment": 80,
        "historical_grounding": 70,
        "characters": 60,
        "plot_causality": 50,
        "longform_structure": 40,
        "scene_execution": 30,
        "style_control": 20,
        "ai_flavor": 10,
    }
    assert score.overall_score_from_medians(medians) == 55.0


def test_score_cli_all_cached_skips_env_key_and_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "config.yaml").write_bytes((ROOT / "config.yaml").read_bytes())
    for version in ("v2", "v2.1"):
        prompt_target = tmp_path / "runner" / "prompts" / version
        prompt_target.mkdir(parents=True)
        for source in (ROOT / "runner" / "prompts" / version).glob("*.md"):
            (prompt_target / source.name).write_bytes(source.read_bytes())
    _write_v2_candidate(tmp_path, "deepseek-v4-flash")
    submission = score.load_submission(tmp_path, "reform-era", "deepseek-v4-flash")
    cfg = score.load_config(tmp_path / "config.yaml")
    judges = score.resolve_judge_configs(cfg)
    rubric = score.load_system_prompt(tmp_path)
    for judge_id in score.JUDGE_IDS:
        judge = judges[judge_id]
        cache_key = score.score_cache_key(
            submission,
            rubric,
            judge_id,
            judge["model_cfg"],
            judge["request_overrides"],
        )
        identity = score.public_score_identity(
            submission,
            judge_id,
            judge["model_cfg"],
            rubric,
            judge["request_overrides"],
        )
        _write_score(submission, judge_id, cache_key, 80, 20, identity)

    monkeypatch.setattr(
        score,
        "load_submission",
        lambda *args, **kwargs: submission,
    )

    def forbidden_env_read(path: Path) -> dict[str, str]:
        raise AssertionError("fresh score cache must skip .env")

    class ForbiddenClient:
        @classmethod
        def from_config(cls, *args, **kwargs):
            raise AssertionError("fresh score cache must skip network preflight")

    monkeypatch.setattr(score, "load_env_file", forbidden_env_read)
    monkeypatch.setattr(score, "ChatClient", ForbiddenClient)
    expected_prompts = score.load_generation_prompts(
        tmp_path / "runner" / "prompts" / "v2.1"
    )
    calculated_prompt_sets: list[dict[str, str]] = []
    real_calculate_run_id = score.calculate_generation_run_id

    def capture_calculate_run_id(
        benchmark: str,
        direction: str,
        prompts: dict[str, str],
        model_cfg: dict,
    ) -> str:
        calculated_prompt_sets.append(prompts)
        return real_calculate_run_id(benchmark, direction, prompts, model_cfg)

    lock_paths: list[Path] = []
    real_work_dir_lock = score.WorkDirLock

    class RecordingWorkDirLock:
        def __init__(self, path: Path) -> None:
            lock_paths.append(path)
            self._delegate = real_work_dir_lock(path)

        def __enter__(self):
            return self._delegate.__enter__()

        def __exit__(self, exc_type, exc_value, traceback):
            return self._delegate.__exit__(exc_type, exc_value, traceback)

    monkeypatch.setattr(score, "calculate_generation_run_id", capture_calculate_run_id)
    monkeypatch.setattr(score, "WorkDirLock", RecordingWorkDirLock)
    assert score.run(["--model", "deepseek-v4-flash"], root=tmp_path) == 0
    assert calculated_prompt_sets == [expected_prompts, expected_prompts]
    assert lock_paths == [
        tmp_path
        / "work"
        / "v2.1"
        / "reform-era"
        / "deepseek-v4-flash"
        / ".run.lock"
    ]
    aggregate = json.loads(
        (
            submission.candidate_dir / "scores" / "aggregate.json"
        ).read_text(encoding="utf-8")
    )
    assert aggregate["status"] == "complete"
    assert aggregate["eligible_for_ranking"] is True


def test_discover_candidates_only_returns_v2_artifact_directories(tmp_path: Path) -> None:
    _write_v2_candidate(tmp_path, "b")
    _write_v2_candidate(tmp_path, "a")
    (tmp_path / "results" / "reform-era" / "empty").mkdir()
    (tmp_path / "results" / "reform-era" / "index.json").write_text("{}", encoding="utf-8")

    assert score.discover_candidates(tmp_path, "reform-era") == ["a", "b"]
