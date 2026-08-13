from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

RUNNER_DIR = Path(__file__).resolve().parents[1] / "runner"
if str(RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(RUNNER_DIR))

import score_v4 as score  # noqa: E402


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _submission(tmp_path: Path) -> score.Submission:
    (tmp_path / "benchmark" / "reform-era").mkdir(parents=True)
    (tmp_path / "benchmark" / "reform-era" / "direction.md").write_text("工厂改革中的人物选择。", encoding="utf-8")
    candidate = tmp_path / "results" / "reform-era" / "candidate-a"
    chapters = candidate / "chapters"
    chapters.mkdir(parents=True)
    source = {
        "01.md": "陈建国在车间里停下扳手，\n听见门外的脚步声。\n他没有回头。",
        "02.md": "夜班结束后，吴跃进把账本塞进抽屉，窗外开始下雪。",
    }
    for name, text in source.items():
        (chapters / name).write_text(text, encoding="utf-8")
    outlines = {
        "book.json": {"title": "雪线", "model": "SECRET"},
        "macro_outline.json": {"acts": ["开端", "转折"]},
        "opening_outline.json": {"chapters": ["第一章", "第二章"]},
    }
    for name, value in outlines.items():
        (candidate / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    (candidate / "novel.md").write_text("This must never be scoring input.", encoding="utf-8")
    (candidate / "manifest.json").write_text(json.dumps({
        "status": "completed",
        "artifact_sha256": {
            **{f"chapters/{name}": _hash(text) for name, text in source.items()},
            **{name: _hash(json.dumps(value, ensure_ascii=False)) for name, value in outlines.items()},
        },
    }), encoding="utf-8")
    return score.load_submission(tmp_path, "reform-era", "candidate-a")


def _response(subscore: int = 3, severity: str = "none") -> dict:
    return {
        "dimensions": {
            spec.key: {
                "subscores": {name: subscore for name in spec.subscores},
                "evidence": [
                    {"chapter": "01", "excerpt": "陈建国在车间里停下扳手，\n听见门外的脚步声。"},
                    {"chapter": "02", "excerpt": "吴跃进把账本塞进抽屉"},
                ],
                "major_defect": {"severity": severity, "description": "存在可定位的问题", "chapter": "01"},
                "confidence": 0.8,
            }
            for spec in score.DIMENSION_SPECS
        }
    }


def _judge_dimensions(value: int = 3) -> dict:
    return score.parse_score_response(json.dumps(_response(value)), {"01": "陈建国在车间里停下扳手，\n听见门外的脚步声。 他没有回头。", "02": "夜班结束后，吴跃进把账本塞进抽屉，窗外开始下雪。"})["dimensions"]


def test_submission_is_chapter_tagged_and_manifest_verified(tmp_path: Path) -> None:
    submission = _submission(tmp_path)
    assert "<direction>" in submission.user_content
    assert '<chapter id="01">' in submission.user_content
    assert '<chapter id="02">' in submission.user_content
    assert "This must never" not in submission.user_content
    (submission.candidate_dir / "chapters" / "02.md").write_text("changed", encoding="utf-8")
    with pytest.raises(score.ScoreError, match="哈希"):
        score.load_submission(tmp_path, "reform-era", "candidate-a")


def test_response_requires_three_integer_subscores_two_verifiable_evidence_and_caps(tmp_path: Path) -> None:
    submission = _submission(tmp_path)
    critical = _response(4, "critical")
    parsed = score.parse_score_response(json.dumps(critical), submission.chapters)
    assert parsed["dimensions"]["characters"]["score"] == 25.0
    assert parsed["dimensions"]["characters"]["evidence"][0]["excerpt"] == "陈建国在车间里停下扳手， 听见门外的脚步声。"
    invalid = _response()
    invalid["dimensions"]["characters"]["evidence"] = invalid["dimensions"]["characters"]["evidence"][:1]
    with pytest.raises(score.ScoreError, match="恰好有 2"):
        score.parse_score_response(json.dumps(invalid), submission.chapters)
    invalid = _response()
    invalid["dimensions"]["characters"]["subscores"]["agency"] = 2.0
    with pytest.raises(score.ScoreError, match="整数"):
        score.parse_score_response(json.dumps(invalid), submission.chapters)


def test_aggregate_uses_locked_weights_and_subscore_ranges() -> None:
    assert score.AGGREGATE_SCHEMA_VERSION == "novel-eval-aggregate.v4"
    assert score.PILOT_MODELS == ("gpt-5.6-sol", "grok-4.6", "gemini-3.1-pro", "minimax-m3")
    assert score._DIMENSIONS["naturalness"].weight == 0.10
    votes = {
        "sol": _judge_dimensions(4),
        "grok": _judge_dimensions(3),
        "opus": _judge_dimensions(2),
        "k3": _judge_dimensions(3),
        "ds-v4-pro": _judge_dimensions(2),
    }
    aggregate = score.aggregate_dimension_scores(votes)
    characters = aggregate["characters"]
    assert characters["weight"] == 0.15
    assert characters["median"] == 75.0
    assert characters["subscores"]["agency"] == {"median": 3.0, "min": 2, "max": 4}
    assert score.overall_score_from_medians(aggregate) == 75.0


def test_public_cache_is_fail_closed(tmp_path: Path) -> None:
    submission = _submission(tmp_path)
    model = {"model": "gpt-5.6-sol"}
    identity = score.public_score_identity(submission, "sol", model, "prompt")
    cache_key = score.score_cache_key(submission, "prompt", "sol", model)
    public = {**identity, "response_model": "gpt-5.6-sol", "cache_key": cache_key, "repair": {"attempted": False, "validation_error": None}, "dimensions": score.parse_score_response(json.dumps(_response()), submission.chapters)["dimensions"]}
    assert score._valid_public_score(public, cache_key, identity, submission.chapters)
    public["cache_key"] = "old-cache"
    assert not score._valid_public_score(public, cache_key, identity, submission.chapters)


def test_aggregate_provenance_binds_actual_vote_payloads() -> None:
    keys = {judge: f"key-{judge}" for judge in score.JUDGE_IDS}
    identities = {
        judge: {"rubric_hash": "rubric", "judge": judge}
        for judge in score.JUDGE_IDS
    }
    votes = {judge: {"vote": 1} for judge in score.JUDGE_IDS}
    first = score.aggregate_provenance(keys, identities, votes)
    votes["sol"] = {"vote": 2}
    second = score.aggregate_provenance(keys, identities, votes)
    assert first["vote_hashes"]["sol"] != second["vote_hashes"]["sol"]
    assert first["binding_hash"] != second["binding_hash"]


def test_judge_mapping_is_fixed_and_rejects_substitution() -> None:
    cfg = {
        "providers": {"new-api": {"base_url_env": "API_URL", "api_key_env": "API_KEY"}},
        "judges": [
            {"id": "sol", "model": "gpt-5.6-sol"},
            {"id": "grok", "model": "grok-4.6"},
            {"id": "opus", "model": "claude-opus-5", "protocol": "anthropic-messages", "protocol_required": {"max_tokens": 16384}},
            {"id": "k3", "model": "kimi-k3"},
            {"id": "ds-v4-pro", "model": "deepseek-v4-pro"},
        ],
    }
    resolved = score.resolve_judge_configs(cfg)
    assert resolved["opus"]["model"] == "claude-opus-5"
    assert resolved["opus"]["context_window"] == score.V4_DEFAULT_CONTEXT_WINDOW
    cfg["judges"][1]["model"] = "some-fallback"
    with pytest.raises(score.ScoreError, match="不允许静默替换"):
        score.resolve_judge_configs(cfg)


def test_opus_v4_uses_native_anthropic_json_schema_without_duplicate_budget() -> None:
    overrides = score.request_overrides_for("opus")
    assert "max_tokens" not in overrides
    assert overrides["tool_choice"]["name"] == "submit_v4_novel_score"
    assert overrides["tools"][0]["strict"] is True
    schema = overrides["tools"][0]["input_schema"]
    assert set(schema["properties"]["dimensions"]["required"]) == set(
        score.DIMENSION_KEYS
    )
    assert score.request_overrides_for("sol") == score.V4_REQUEST_OVERRIDES


def _audit_response() -> dict:
    return {
        "outline_quality": "主线目标明确，但中段转折仍需加强因果。",
        "execution_fidelity": ["第一章已经落实工厂困境。", "人物选择开始承接大纲。"],
        "major_deviations": "暂无结构性偏离。",
        "deviation_improved": "暂无偏离，因此不适用改善判断。",
    }


def test_outline_audit_uses_full_outline_input_and_structured_cache(tmp_path: Path) -> None:
    submission = _submission(tmp_path)
    audit = score.load_outline_audit_submission(tmp_path, submission)
    assert "<book>" in audit.user_content and "<macro_outline>" in audit.user_content
    assert "<opening_outline>" in audit.user_content and '<chapter id="01">' in audit.user_content
    assert "SECRET" not in audit.user_content

    class Client:
        def __init__(self) -> None: self.calls = 0
        def complete(self, *_args, **_kwargs):
            self.calls += 1
            return SimpleNamespace(content=json.dumps(_audit_response()), finish_reason="stop", requested_model="gpt-5.6-sol", response_model="gpt-5.6-sol")

    client = Client()
    assert score._outline_audit(root=tmp_path, audit=audit, client=client, model_cfg={"model": "gpt-5.6-sol"}, prompt="审读") == "audited"
    assert score._outline_audit(root=tmp_path, audit=audit, client=client, model_cfg={"model": "gpt-5.6-sol"}, prompt="审读") == "cached"
    assert client.calls == 1
    public = json.loads((submission.candidate_dir / "scores-v4" / "outline-audit.json").read_text(encoding="utf-8"))
    assert public["outline_input_hash"] == audit.outline_input_hash
    assert "content" not in public and public["audit"] == _audit_response()
    with pytest.raises(score.ScoreError, match="只能包含"):
        score.parse_outline_audit_response(json.dumps({**_audit_response(), "extra": "x"}))


def test_repair_provenance_and_context_guard_fail_closed(tmp_path: Path) -> None:
    submission = _submission(tmp_path)
    valid = json.dumps(_response())

    class RepairClient:
        def __init__(self) -> None: self.calls: list[list[dict[str, str]]] = []
        def complete(self, _model, messages, **_kwargs):
            self.calls.append(messages)
            content = "{}" if len(self.calls) == 1 else valid
            return SimpleNamespace(content=content, finish_reason="stop", requested_model="gpt-5.6-sol", response_model="gpt-5.6-sol")

    client = RepairClient()
    _, public = score.evaluate_judge(root=tmp_path, submission=submission, judge_id="sol", model_cfg={"model": "gpt-5.6-sol"}, system_prompt="原始系统约束", repair_prompt="修复", client=client)
    assert public["repair"]["attempted"] is True
    assert public["repair"]["validation_error"]
    assert submission.user_content in client.calls[1][1]["content"]
    assert "原始系统约束" in client.calls[1][0]["content"]

    class ForbiddenClient:
        calls = 0
        def complete(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("oversized request must not be sent")

    forbidden = ForbiddenClient()
    with pytest.raises(score.ScoreError, match="未发送请求"):
        score.evaluate_judge(root=tmp_path, submission=submission, judge_id="sol", model_cfg={"model": "gpt-5.6-sol", "context_window": 20_000}, system_prompt="x" * 6_000, repair_prompt="修复", client=forbidden)
    assert forbidden.calls == 0


def test_missing_response_model_is_rejected_for_score_and_outline(tmp_path: Path) -> None:
    submission = _submission(tmp_path)

    class MissingIdentityClient:
        def complete(self, _model, _messages, **_kwargs):
            return SimpleNamespace(
                content=json.dumps(_response()),
                finish_reason="stop",
                requested_model="gpt-5.6-sol",
            )

    with pytest.raises(score.ScoreError, match="模型不一致"):
        score.evaluate_judge(
            root=tmp_path,
            submission=submission,
            judge_id="sol",
            model_cfg={"model": "gpt-5.6-sol"},
            system_prompt="评分",
            repair_prompt="修复",
            client=MissingIdentityClient(),
        )

    audit = score.load_outline_audit_submission(tmp_path, submission)

    class MissingAuditIdentityClient:
        def complete(self, _model, _messages, **_kwargs):
            return SimpleNamespace(
                content=json.dumps(_audit_response()),
                finish_reason="stop",
                requested_model="gpt-5.6-sol",
            )

    assert score._outline_audit(
        root=tmp_path,
        audit=audit,
        client=MissingAuditIdentityClient(),
        model_cfg={"model": "gpt-5.6-sol"},
        prompt="审读",
    ) == "failed"
    assert not (submission.candidate_dir / "scores-v4" / "outline-audit.json").exists()


def test_cli_cached_votes_and_outline_skip_network_and_show_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    submission = _submission(tmp_path)
    prompt_dir = tmp_path / "runner" / "prompts" / "v4"; prompt_dir.mkdir(parents=True)
    (prompt_dir / "absolute_system.md").write_text("评审\n{{DIMENSION_SPECS}}", encoding="utf-8")
    (prompt_dir / "repair_json.md").write_text("修复", encoding="utf-8")
    (prompt_dir / "outline_system.md").write_text("审读", encoding="utf-8")
    configs = {
        judge: {
            "model": wire,
            **({"protocol": "anthropic-messages", "protocol_required": {"max_tokens": 16384}} if judge == "opus" else {}),
        }
        for judge, wire in score.EXPECTED_JUDGE_MODELS.items()
    }
    monkeypatch.setattr(score, "load_config", lambda _path: {})
    monkeypatch.setattr(score, "resolve_judge_configs", lambda _cfg: configs)
    system_prompt = score.load_system_prompt(tmp_path)
    for judge, model in configs.items():
        overrides = score.request_overrides_for(judge)
        identity = score.public_score_identity(submission, judge, model, system_prompt, overrides)
        key = score.score_cache_key(submission, system_prompt, judge, model, overrides)
        public = {**identity, "response_model": model["model"], "cache_key": key, "repair": {"attempted": False, "validation_error": None}, "dimensions": score.parse_score_response(json.dumps(_response()), submission.chapters)["dimensions"]}
        score._atomic_write_json(submission.candidate_dir / "scores-v4" / f"{judge}.json", public)
    outline = score.load_outline_audit_submission(tmp_path, submission)
    outline_prompt = score.load_outline_prompt(tmp_path)
    outline_identity = score.outline_audit_identity(outline, outline_prompt, configs["sol"], score.V4_REQUEST_OVERRIDES)
    outline_key = score.outline_audit_cache_key(outline, outline_prompt, configs["sol"], score.V4_REQUEST_OVERRIDES)
    score._atomic_write_json(submission.candidate_dir / "scores-v4" / "outline-audit.json", {**outline_identity, "response_model": "gpt-5.6-sol", "cache_key": outline_key, "audit": _audit_response()})

    assert score.run(["--model", "candidate-a", "--dry-run"], root=tmp_path) == 0
    output = capsys.readouterr().out
    assert "/ sol: cached" in output and "/ outline-audit: cached" in output
    assert "absolute_calls=0 outline_audits=0" in output and "estimated_tokens=" in output
    (tmp_path / "results" / "reform-era" / "_pairwise-v4").mkdir()
    assert "_pairwise-v4" not in score.discover_candidates(tmp_path, "reform-era")


def test_cli_all_is_blocked_by_current_pilot_before_config_or_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(score, "_current_pilot_gate", lambda _root, _benchmark: (False, "pilot hashes are stale"))
    monkeypatch.setattr(score, "load_config", lambda _path: (_ for _ in ()).throw(AssertionError("must block before config/network")))
    assert score.run(["--all", "--dry-run"], root=tmp_path) == 1
    assert "BLOCKED: pilot hashes are stale" in capsys.readouterr().err
