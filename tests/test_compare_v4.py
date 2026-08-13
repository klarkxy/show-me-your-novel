from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "runner"
if str(RUNNER) not in sys.path:
    sys.path.insert(0, str(RUNNER))
import compare_v4 as compare  # noqa: E402


def _candidate(root: Path, name: str, score: float, *, status: str = "complete") -> None:
    benchmark = root / "benchmark" / "reform-era"
    base = root / "results" / "reform-era" / name
    benchmark.mkdir(parents=True, exist_ok=True)
    (benchmark / "direction.md").write_text("改革年代的长篇小说", encoding="utf-8")
    base.mkdir(parents=True)
    chapters = base / "chapters"; chapters.mkdir()
    chapter_text = "第一章 开端\n人物行动。\n第二章 转折\n后果显现。"
    (chapters / "01.md").write_text(chapter_text, encoding="utf-8")
    chapter_hash = hashlib.sha256(chapter_text.encode("utf-8")).hexdigest()
    (base / "manifest.json").write_text(json.dumps({"status": "completed", "artifact_sha256": {"chapters/01.md": chapter_hash}, "model": "SECRET"}), encoding="utf-8")
    scores = base / "scores-v4"; scores.mkdir()
    config_path = root / "config.yaml"
    if not config_path.exists():
        config_path.write_text(json.dumps({
            "providers": {"new-api": {"base_url_env": "API_URL", "api_key_env": "API_KEY"}},
            "judges": [
                {
                    "id": judge,
                    "model": compare.EXPECTED_JUDGE_MODELS[judge],
                    **({"protocol": "anthropic-messages", "protocol_required": {"max_tokens": 16384}} if judge == "opus" else {}),
                }
                for judge in compare.JUDGE_IDS
            ],
        }), encoding="utf-8")
    prompt_dir = root / "runner" / "prompts" / "v4"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    absolute_path = prompt_dir / "absolute_system.md"
    if not absolute_path.exists():
        absolute_path.write_text("绝对评分\n{{DIMENSION_SPECS}}", encoding="utf-8")
    submission = compare._score_v4.load_submission(root, "reform-era", name)
    raw_dimensions = {
        spec.key: {
            "subscores": {sub: 3 for sub in spec.subscores},
            "evidence": [
                {"chapter": "01", "excerpt": "第一章 开端"},
                {"chapter": "01", "excerpt": "人物行动。"},
            ],
            "major_defect": {"severity": "none", "description": "无重大缺陷"},
            "confidence": 0.8,
        }
        for spec in compare.DIMENSION_SPECS
    }
    parsed_dimensions = compare._score_v4.parse_score_response(json.dumps({"dimensions": raw_dimensions}, ensure_ascii=False), submission.chapters)["dimensions"]
    cfg = compare._score_v4.load_config(config_path)
    judge_configs = compare._score_v4.resolve_judge_configs(cfg)
    absolute_prompt = compare._score_v4.load_system_prompt(root)
    keys = {}
    identities = {}
    for judge in compare.JUDGE_IDS:
        overrides = compare._score_v4.request_overrides_for(judge)
        keys[judge] = compare._score_v4.score_cache_key(submission, absolute_prompt, judge, judge_configs[judge], overrides)
        identities[judge] = compare._score_v4.public_score_identity(submission, judge, judge_configs[judge], absolute_prompt, overrides)
        if status == "complete":
            vote = {**identities[judge], "response_model": compare.EXPECTED_JUDGE_MODELS[judge], "cache_key": keys[judge], "repair": {"attempted": False, "validation_error": None}, "dimensions": parsed_dimensions}
            (scores / f"{judge}.json").write_text(json.dumps(vote), encoding="utf-8")
    aggregate = compare._score_v4.aggregate_scores(submission, keys, identities)
    (scores / "aggregate.json").write_text(json.dumps(aggregate), encoding="utf-8")


def _dimensions(winner: str = "A", margin: int = 2) -> dict:
    return {key: {"winner": winner, "margin": margin, "evidence": "第一章人物行动造成第二章后果。"} for key in compare.DIMENSION_KEYS}


def _prompt(root: Path) -> str:
    prompt = root / "runner" / "prompts" / "v4"; prompt.mkdir(parents=True, exist_ok=True)
    (prompt / "pairwise_system.md").write_text("评审\n{{DIMENSION_SPECS}}", encoding="utf-8")
    return compare.load_system_prompt(root)


def _judge_configs() -> dict:
    return {
        judge: {
            "model_cfg": {
                "model": compare.EXPECTED_JUDGE_MODELS[judge],
                "context_window": 131_072,
                **({"protocol": "anthropic-messages", "protocol_required": {"max_tokens": 16384}} if judge == "opus" else {}),
            },
            "request_overrides": None,
        }
        for judge in compare.JUDGE_IDS
    }


def test_completed_aggregate_selection_edges_and_anonymous_chapter_content(tmp_path: Path) -> None:
    _candidate(tmp_path, "alpha", 90); _candidate(tmp_path, "bravo", 80); _candidate(tmp_path, "charlie", 70)
    _candidate(tmp_path, "incomplete", 99, status="incomplete")
    candidates = compare.load_completed_candidates(tmp_path)
    assert [item.name for item in candidates] == ["alpha", "bravo", "charlie"]
    assert compare.select_edges(candidates) == [("alpha", "bravo"), ("bravo", "charlie"), ("alpha", "charlie")]
    assert "SECRET" not in candidates[0].content
    messages = compare.build_messages("system", candidates[0], candidates[1], ("alpha", "bravo"))
    assert "alpha" not in messages[1]["content"] and "bravo" not in messages[1]["content"]
    assert '<A_chapter id="01">' in messages[1]["content"] and '<B_chapter id="01">' in messages[1]["content"]


def test_stale_aggregate_identity_is_not_rankable(tmp_path: Path) -> None:
    _candidate(tmp_path, "alpha", 90)
    path = tmp_path / "results" / "reform-era" / "alpha" / "scores-v4" / "aggregate.json"
    aggregate = json.loads(path.read_text(encoding="utf-8")); aggregate["input_hash"] = "stale"
    path.write_text(json.dumps(aggregate), encoding="utf-8")
    assert compare.load_completed_candidates(tmp_path) == []


def test_aggregate_is_invalidated_when_a_current_vote_payload_changes(tmp_path: Path) -> None:
    _candidate(tmp_path, "alpha", 90)
    vote_path = tmp_path / "results" / "reform-era" / "alpha" / "scores-v4" / "sol.json"
    vote = json.loads(vote_path.read_text(encoding="utf-8"))
    vote["dimensions"]["characters"]["confidence"] = 0.7
    vote_path.write_text(json.dumps(vote), encoding="utf-8")
    assert compare.load_completed_candidates(tmp_path) == []


def test_complete_aggregate_with_missing_judge_dimensions_is_not_rankable(tmp_path: Path) -> None:
    _candidate(tmp_path, "alpha", 90)
    path = tmp_path / "results" / "reform-era" / "alpha" / "scores-v4" / "aggregate.json"
    aggregate = json.loads(path.read_text(encoding="utf-8")); aggregate["judges"].pop("opus")
    path.write_text(json.dumps(aggregate), encoding="utf-8")
    assert compare.load_completed_candidates(tmp_path) == []


def test_orders_are_stable_and_balanced() -> None:
    first = compare.balanced_display_orders("a", "b")
    assert first == compare.balanced_display_orders("a", "b")
    assert set(first) == set(compare.JUDGE_IDS)
    assert sum(order[0] == "a" for order in first.values()) in {2, 3}


def test_global_order_plan_balances_each_judge_across_full_edge_set() -> None:
    edges = [("a", "b"), ("b", "c"), ("c", "d"), ("a", "c"), ("b", "d")]
    plan = compare.build_order_plan(edges)
    assert plan == compare.build_order_plan(edges)
    for judge in compare.JUDGE_IDS:
        shown_a = sum(order[judge][0] == edge[0] for edge, order in plan.items())
        assert abs(shown_a - (len(edges) - shown_a)) <= 1


def test_canonical_input_has_no_outline_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _candidate(tmp_path, "alpha", 90)
    monkeypatch.setattr(compare, "_score_v4", None)
    with pytest.raises(compare.CompareError, match="非规范比较输入"):
        compare._candidate_input(tmp_path, "reform-era", "alpha")


def test_pairwise_override_is_explicit_cached_and_does_not_duplicate_opus_max_tokens(tmp_path: Path) -> None:
    _candidate(tmp_path, "alpha", 90); _candidate(tmp_path, "bravo", 80)
    left, right = compare.load_completed_candidates(tmp_path)
    configs = _judge_configs()
    opus = compare.effective_request_overrides("opus", None)
    assert "max_tokens" not in opus
    assert opus["tool_choice"]["name"] == "submit_v4_pairwise_vote"
    assert opus["tools"][0]["strict"] is True
    opus_with_temperature = compare.effective_request_overrides(
        "opus", {"temperature": 0.2}
    )
    assert opus_with_temperature["temperature"] == 0.2
    assert opus_with_temperature["tools"] == opus["tools"]
    assert opus_with_temperature["tool_choice"] == opus["tool_choice"]
    first = compare.pair_cache_key(left, right, "prompt", configs)
    configs["sol"]["request_overrides"] = {"max_tokens": 123}
    assert first == compare.pair_cache_key(left, right, "prompt", configs)
    configs["sol"]["request_overrides"] = {"top_p": 0.8}
    assert first != compare.pair_cache_key(left, right, "prompt", configs)


def test_pairwise_uses_judge_stage_for_grok_tools_and_opus_protocol_budget(tmp_path: Path) -> None:
    _candidate(tmp_path, "alpha", 90); _candidate(tmp_path, "bravo", 80)
    left, right = compare.load_completed_candidates(tmp_path)
    configs = compare.resolve_judge_configs(compare._llm_api.load_config(ROOT / "config.yaml"))
    grok_stage = compare.effective_stage_config(configs["grok"]["model_cfg"])
    assert grok_stage["tool_choice"] == "none" and grok_stage["tools"][0]["function"]["name"] == "unused_judge_tool"
    assert configs["opus"]["model_cfg"]["protocol"] == "anthropic-messages"
    opus_overrides = compare.effective_request_overrides(
        "opus", configs["opus"]["request_overrides"]
    )
    assert "max_tokens" not in opus_overrides
    assert opus_overrides["tool_choice"]["name"] == "submit_v4_pairwise_vote"
    captured = {}

    class Client:
        def complete(self, model_cfg, _messages, *, stage, request_overrides):
            captured.update({"model_cfg": model_cfg, "stage": stage, "request_overrides": request_overrides})
            return SimpleNamespace(content=json.dumps({"dimensions": _dimensions()}), requested_model=model_cfg["model"], response_model=model_cfg["model"], finish_reason="stop")

    compare._run_vote(Client(), "grok", configs["grok"], "system", left, right, (left.name, right.name))
    assert captured["stage"] == "judge"
    assert captured["model_cfg"]["stages"]["judge"]["tool_choice"] == "none"


def test_pilot_candidate_filter_yields_all_five_adjacent_and_distance_two_edges(tmp_path: Path) -> None:
    for index, name in enumerate(compare.PILOT_CANDIDATES):
        _candidate(tmp_path, name, 100 - index)
    _candidate(tmp_path, "not-a-pilot", 200)
    candidates = compare.load_completed_candidates(tmp_path, allowed=compare.PILOT_CANDIDATES)
    assert {candidate.name for candidate in candidates} == set(compare.PILOT_CANDIDATES)
    assert len(compare.select_edges(candidates)) == 5


def test_pilot_cli_refuses_a_partial_four_candidate_set(tmp_path: Path) -> None:
    for index, name in enumerate(compare.PILOT_CANDIDATES[:3]):
        _candidate(tmp_path, name, 100 - index)
    _prompt(tmp_path)
    with pytest.raises(compare.CompareError, match="pilot 必须先具备四份"):
        compare.run(["--pilot", "--dry-run"], root=tmp_path)


def test_parser_is_strict_and_rejects_nonzero_tie() -> None:
    parsed = compare.parse_pairwise_response(json.dumps({"dimensions": _dimensions()}))
    assert parsed["dimensions"][compare.DIMENSION_KEYS[0]]["winner"] == "A"
    invalid = _dimensions("tie", 1)
    with pytest.raises(compare.CompareError, match="tie"):
        compare.parse_pairwise_response(json.dumps({"dimensions": invalid}))


def test_reverse_rule_handles_no_majority_and_small_margin() -> None:
    votes = {
        "sol": {"displayed_a": "left", "dimensions": _dimensions("A", 1)},
        "opus": {"displayed_a": "right", "dimensions": _dimensions("A", 1)},
        "grok": {"displayed_a": "left", "dimensions": _dimensions("tie", 0)},
        "k3": {"displayed_a": "left", "dimensions": _dimensions("A", 1)},
        "ds-v4-pro": {"displayed_a": "right", "dimensions": _dimensions("A", 1)},
    }
    summary = compare.summarize_votes(votes, "left")
    assert summary["has_majority"] is False
    assert compare.needs_reverse(summary) is True


def test_compare_writes_cache_and_valid_cache_makes_no_client_call(tmp_path: Path) -> None:
    _candidate(tmp_path, "alpha", 90); _candidate(tmp_path, "bravo", 80)
    left, right = compare.load_completed_candidates(tmp_path)
    prompt = _prompt(tmp_path); configs = _judge_configs()

    class Client:
        def __init__(self): self.calls = 0
        def complete(self, *_args, **_kwargs):
            self.calls += 1
            model = _args[0]["model"]
            return SimpleNamespace(content=json.dumps({"dimensions": _dimensions("A", 2)}), requested_model=model, response_model=model, finish_reason="stop")

    client = Client()
    status, record = compare.compare_edge(tmp_path, "reform-era", left, right, prompt, configs, client)
    assert status == "compared" and record and client.calls == 5
    status, cached = compare.compare_edge(tmp_path, "reform-era", left, right, prompt, configs, None)
    assert status == "cached" and cached and client.calls == 5
    tampered = json.loads(json.dumps(cached))
    tampered["decision"]["weighted_margin"] = -1.0
    tampered["decision"]["winner"] = right.name
    key = compare.pair_cache_key(left, right, prompt, configs)
    assert not compare._record_valid(tampered, left, right, key)


def test_context_limit_fails_closed_without_truncation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _candidate(tmp_path, "alpha", 90); _candidate(tmp_path, "bravo", 80)
    left, right = compare.load_completed_candidates(tmp_path)
    configs = _judge_configs()
    configs["sol"]["model_cfg"]["context_window"] = 10
    with pytest.raises(compare.CompareError, match="未截断"):
        compare.validate_edge_context("system", left, right, configs)


def test_lock_revalidation_prevents_duplicate_paid_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _candidate(tmp_path, "alpha", 90); _candidate(tmp_path, "bravo", 80)
    left, right = compare.load_completed_candidates(tmp_path)
    prompt = _prompt(tmp_path); configs = _judge_configs()

    class SeedClient:
        def complete(self, model_cfg, *_args, **_kwargs):
            return SimpleNamespace(content=json.dumps({"dimensions": _dimensions()}), requested_model=model_cfg["model"], response_model=model_cfg["model"], finish_reason="stop")

    _status, record = compare.compare_edge(tmp_path, "reform-era", left, right, prompt, configs, SeedClient())
    assert record is not None
    path = tmp_path / "results" / "reform-era" / "_pairwise-v4" / "pairs" / f"{compare.edge_id(left.name, right.name)}.json"
    path.unlink()

    class Lock:
        def __init__(self, _path): pass
        def __enter__(self): compare._atomic_write_json(path, record); return self
        def __exit__(self, *_args): return False

    class PaidCallForbidden:
        def complete(self, *_args, **_kwargs): raise AssertionError("must revalidate inside lock")

    monkeypatch.setattr(compare, "WorkDirLock", Lock)
    status, cached = compare.compare_edge(tmp_path, "reform-era", left, right, prompt, configs, PaidCallForbidden())
    assert status == "cached" and cached == record


def test_changed_completed_aggregate_during_votes_discards_response(tmp_path: Path) -> None:
    _candidate(tmp_path, "alpha", 90); _candidate(tmp_path, "bravo", 80)
    left, right = compare.load_completed_candidates(tmp_path)
    prompt = _prompt(tmp_path); configs = _judge_configs()
    aggregate_path = tmp_path / "results" / "reform-era" / "alpha" / "scores-v4" / "aggregate.json"

    class MutatingClient:
        calls = 0
        def complete(self, model_cfg, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                aggregate = json.loads(aggregate_path.read_text(encoding="utf-8")); aggregate["overall_score"] = 89.0
                aggregate_path.write_text(json.dumps(aggregate), encoding="utf-8")
            return SimpleNamespace(content=json.dumps({"dimensions": _dimensions()}), requested_model=model_cfg["model"], response_model=model_cfg["model"], finish_reason="stop")

    with pytest.raises(compare.CompareError, match="已丢弃响应"):
        compare.compare_edge(tmp_path, "reform-era", left, right, prompt, configs, MutatingClient())
    path = tmp_path / "results" / "reform-era" / "_pairwise-v4" / "pairs" / f"{compare.edge_id(left.name, right.name)}.json"
    assert not path.exists()


def test_cli_all_valid_cache_skips_client_and_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for index, name in enumerate(compare.PILOT_CANDIDATES):
        _candidate(tmp_path, name, 100 - index)
    candidates = compare.load_completed_candidates(tmp_path, allowed=compare.PILOT_CANDIDATES)
    by_name = {candidate.name: candidate for candidate in candidates}
    prompt = _prompt(tmp_path); configs = _judge_configs()

    class SeedClient:
        def complete(self, *_args, **_kwargs):
            model = _args[0]["model"]
            return SimpleNamespace(content=json.dumps({"dimensions": _dimensions("A", 2)}), requested_model=model, response_model=model, finish_reason="stop")

    edges = compare.select_edges(candidates)
    order_plan = compare.build_order_plan(edges)
    for left_name, right_name in edges:
        compare.compare_edge(tmp_path, "reform-era", by_name[left_name], by_name[right_name], prompt, configs, SeedClient(), orders=order_plan[(left_name, right_name)])
    monkeypatch.setattr(compare._llm_api, "load_config", lambda _path: {})
    monkeypatch.setattr(compare, "resolve_judge_configs", lambda _cfg: configs)

    class NetworkForbidden:
        @staticmethod
        def from_config(*_args, **_kwargs):
            raise AssertionError("valid cache must not construct a client")

    monkeypatch.setattr(compare._llm_api, "ChatClient", NetworkForbidden)
    assert compare.run(["--pilot"], root=tmp_path) == 0
    assert (tmp_path / "results" / "reform-era" / "_pairwise-v4" / "ranking.json").is_file()


def test_all_dry_run_is_blocked_without_pilot_or_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _candidate(tmp_path, "alpha", 90); _candidate(tmp_path, "bravo", 80)
    class NetworkForbidden:
        @staticmethod
        def from_config(*_args, **_kwargs): raise AssertionError("blocked before network")
    monkeypatch.setattr(compare._llm_api, "ChatClient", NetworkForbidden)
    assert compare.run(["--all", "--dry-run"], root=tmp_path) == 1
    assert "BLOCKED" in capsys.readouterr().err


def test_all_refuses_partial_fixed_cohort_before_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in compare.ALL_CANDIDATES[:-1]:
        _candidate(tmp_path, name, 75)
    monkeypatch.setattr(compare, "current_pilot_gate", lambda *_: (True, "passed"))

    class NetworkForbidden:
        @staticmethod
        def from_config(*_args, **_kwargs):
            raise AssertionError("fixed-cohort preflight must run before network")

    monkeypatch.setattr(compare._llm_api, "ChatClient", NetworkForbidden)
    with pytest.raises(compare.CompareError, match="固定 14 本"):
        compare.run(["--all", "--dry-run"], root=tmp_path)


def test_pilot_dry_run_reports_request_counts_and_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    for index, name in enumerate(compare.PILOT_CANDIDATES): _candidate(tmp_path, name, 100 - index)
    _prompt(tmp_path)
    configs = _judge_configs()
    monkeypatch.setattr(compare._llm_api, "load_config", lambda _path: {})
    monkeypatch.setattr(compare, "resolve_judge_configs", lambda _cfg: configs)
    assert compare.run(["--pilot", "--dry-run"], root=tmp_path) == 0
    output = capsys.readouterr().out
    assert "base=25" in output and "max-reversal-additions=25" in output and "input_tokens=" in output


def test_weighted_bradley_terry_bootstrap_and_overlap_are_deterministic(tmp_path: Path) -> None:
    _candidate(tmp_path, "alpha", 90); _candidate(tmp_path, "bravo", 80); _candidate(tmp_path, "charlie", 70)
    candidates = compare.load_completed_candidates(tmp_path)
    records = [
        {"schema": compare.PAIRWISE_SCHEMA_VERSION, "left": "alpha", "right": "bravo", "decision": {"weighted_margin": 0.8}},
        {"schema": compare.PAIRWISE_SCHEMA_VERSION, "left": "bravo", "right": "charlie", "decision": {"weighted_margin": 0.7}},
    ]
    first = compare.build_ranking("reform-era", candidates, records)
    second = compare.build_ranking("reform-era", candidates, records)
    assert first == second
    assert first["schema"] == compare.RANKING_SCHEMA_VERSION
    assert first["status"] == "complete" and first["connected_graph"] is True
    assert first["ranking"][0]["candidate"] == "alpha"
    assert all("win_probability" in row and "ci_overlaps_next" in row for row in first["ranking"])
