from __future__ import annotations

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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            '{"score":82,"ai_flavor":17,"comment":"优点明确，问题具体。"}',
            {"score": 82, "ai_flavor": 17, "comment": "优点明确，问题具体。"},
        ),
        (
            '```json\n{"score":0,"ai_flavor":100,"comment":"  首尾边界。  "}\n```',
            {"score": 0, "ai_flavor": 100, "comment": "首尾边界。"},
        ),
    ],
)
def test_parse_score_response(raw: str, expected: dict) -> None:
    assert score.parse_score_response(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        '{"score":101,"ai_flavor":10,"comment":"越界"}',
        '{"score":80.5,"ai_flavor":10,"comment":"浮点"}',
        '{"score":true,"ai_flavor":10,"comment":"布尔"}',
        '{"score":80,"ai_flavor":10,"comment":"好","extra":1}',
        '{"score":80,"ai_flavor":10,"comment":""}',
        '{"score":80,"ai_flavor":10,"comment":"' + ('评' * 201) + '"}',
    ],
)
def test_parse_score_response_rejects_invalid_schema(raw: str) -> None:
    with pytest.raises(score.ScoreError):
        score.parse_score_response(raw)


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
            content='{"score":84,"ai_flavor":21,"comment":"人物可信，但中段节奏略平。"}',
            reasoning_content="private reasoning",
            usage={"prompt_tokens": 1000, "completion_tokens": 100},
            requested_model=model_cfg["model"],
            response_model=model_cfg["model"],
            finish_reason="stop",
            response_id="response-1",
            latency_ms=123,
            raw_response={"id": "response-1"},
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
    assert public["score"] == 84
    assert client.calls == 1

    public_path = submission.candidate_dir / "scores" / "sol.json"
    diagnostic_path = tmp_path / "work" / "scoring" / "reform-era" / "candidate-a" / "sol.json"
    public_disk = json.loads(public_path.read_text(encoding="utf-8"))
    private_disk = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    assert "usage" not in public_disk
    assert "raw_response" not in public_disk
    assert public_disk["judge_config_sha256"] == score.judge_config_sha256(
        model_cfg, None
    )
    assert private_disk["usage"]["prompt_tokens"] == 1000
    assert private_disk["reasoning_content"] == "private reasoning"

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
    diagnostic = json.loads(
        (
            tmp_path
            / "work"
            / "scoring"
            / "reform-era"
            / "candidate-a"
            / "sol.json"
        ).read_text(encoding="utf-8")
    )
    assert diagnostic["finish_reason"] == "length"


def _write_score(
    submission: score.Submission,
    judge_id: str,
    cache_key: str,
    value: int,
    ai_flavor: int,
    identity: dict,
) -> None:
    path = submission.candidate_dir / "scores" / f"{judge_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                **identity,
                "cache_key": cache_key,
                "score": value,
                "ai_flavor": ai_flavor,
                "comment": f"{judge_id} 点评",
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
    assert incomplete["score"] is None
    assert incomplete["ai_flavor"] is None

    _write_score(submission, "kimi", keys["kimi"], 85, 15, identities["kimi"])
    complete = score.aggregate_scores(submission, keys, identities)
    assert complete["status"] == "complete"
    assert complete["eligible_for_ranking"] is True
    assert complete["score"] == 85.0
    assert complete["ai_flavor"] == 15.0

    # A stale result must invalidate the complete aggregate.
    keys["kimi"] = "new-key-kimi"
    stale = score.aggregate_scores(submission, keys, identities)
    assert stale["status"] == "incomplete"
    assert stale["score"] is None


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
