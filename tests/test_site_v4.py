from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.generate_site import (
    V4_AGGREGATE_SCHEMA,
    V4_DIMENSION_SPECS,
    V4_JUDGE_IDS,
    V4_RANKING_SCHEMA,
    attach_v4_results,
    render_v4_home,
    _canonical_json,
    _load_v4_outline_audit,
    _v4_leaderboard_row,
    _v4_default_ranking_quality,
    _normalise_v4_aggregate,
    _normalise_v4_ranking,
    _sha256_text,
)


def _aggregate(candidate: str = "candidate-a", input_hash: str = "v4-work", subscore: int = 3) -> dict:
    dimensions = {}
    judge_dimensions = {}
    for spec in V4_DIMENSION_SPECS:
        dimensions[spec.key] = {
            "label": spec.label,
            "weight": spec.weight,
            "median": round(subscore / 4 * 100, 1),
            "min": round(subscore / 4 * 100, 1),
            "max": round(subscore / 4 * 100, 1),
            "subscores": {
                subkey: {"median": float(subscore), "min": subscore, "max": subscore}
                for subkey in spec.subscores
            },
        }
        judge_dimensions[spec.key] = {
            "score": round(subscore / 4 * 100, 1),
            "subscores": {subkey: subscore for subkey in spec.subscores},
            "evidence": [
                {"chapter": "01", "excerpt": "可核验文本一"},
                {"chapter": "02", "excerpt": "可核验文本二"},
            ],
            "major_defect": {"severity": "none", "description": "无"},
            "confidence": 0.8,
        }
    return {
        "schema": V4_AGGREGATE_SCHEMA,
        "benchmark": "reform-era",
        "candidate": candidate,
        "input_hash": input_hash,
        "provenance": {"binding_hash": "current"},
        "status": "complete",
        "eligible_for_ranking": True,
        "overall_score": round(subscore / 4 * 100, 1),
        "dimensions": dimensions,
        "expected_judges": list(V4_JUDGE_IDS),
        "completed_judges": list(V4_JUDGE_IDS),
        "judges": {
            judge: {"dimensions": json.loads(json.dumps(judge_dimensions))}
            for judge in V4_JUDGE_IDS
        },
    }


def test_v4_aggregate_is_independent_of_v3_ai_flavor() -> None:
    aggregate = _aggregate()
    parsed = _normalise_v4_aggregate(
        aggregate, benchmark="reform-era", candidate="candidate-a", input_hash="v4-work", provenance=aggregate["provenance"]
    )
    assert parsed["valid"] is True
    assert "naturalness" in parsed["dimensions"]
    assert "ai_flavor" not in parsed["dimensions"]

    aggregate["dimensions"]["ai_flavor"] = aggregate["dimensions"].pop("naturalness")
    aggregate["judges"]["sol"]["dimensions"]["ai_flavor"] = aggregate["judges"]["sol"]["dimensions"].pop("naturalness")
    assert not _normalise_v4_aggregate(
        aggregate, benchmark="reform-era", candidate="candidate-a", input_hash="v4-work", provenance=aggregate["provenance"]
    )["valid"]


def test_v4_aggregate_rejects_missing_judge_or_recomputed_tampering() -> None:
    aggregate = _aggregate()
    del aggregate["judges"]["grok"]
    assert not _normalise_v4_aggregate(
        aggregate, benchmark="reform-era", candidate="candidate-a", input_hash="v4-work", provenance=aggregate["provenance"]
    )["valid"]

    aggregate = _aggregate()
    aggregate["dimensions"]["characters"]["median"] = 99.0
    assert not _normalise_v4_aggregate(
        aggregate, benchmark="reform-era", candidate="candidate-a", input_hash="v4-work", provenance=aggregate["provenance"]
    )["valid"]


def test_v4_ranking_accepts_bradley_terry_rating_and_probability() -> None:
    payload = {
            "schema": V4_RANKING_SCHEMA,
            "benchmark": "reform-era",
            "ranking": [
                {
                    "candidate": "candidate-a",
                    "rank": 1,
                    "rating": -0.25,
                    "win_probability": 0.625,
                    "rating_ci95": [-0.75, 0.2],
                    "ci_overlaps_next": False,
                }
            ],
        }
    ranked = _normalise_v4_ranking(payload, "reform-era")
    assert ranked["candidate-a"]["relative_score"] == -0.25
    assert ranked["candidate-a"]["win_probability"] == 0.625
    assert ranked["candidate-a"]["ci_overlaps_next"] is False
    payload["ranking"][0]["ci_overlaps_next"] = True
    assert _normalise_v4_ranking(payload, "reform-era")["candidate-a"]["ci_overlaps_next"] is True
    del payload["ranking"][0]["rating_ci95"]
    assert _normalise_v4_ranking(payload, "reform-era") == {}


def test_v4_overlap_label_is_rendered_only_when_declared() -> None:
    v4 = _normalise_v4_aggregate(
        _aggregate(), benchmark="reform-era", candidate="candidate-a", input_hash="v4-work", provenance={"binding_hash": "current"}
    )
    v4.update({"rank": 1, "relative_score": 0.1, "win_probability": 0.5, "ci95": (-0.1, 0.2), "ci_overlaps_next": True})
    result = {"v4": v4, "model_id": "candidate-a", "model_name": "A", "title": "作品", "detail_available": True}
    assert "无法可靠区分" in _v4_leaderboard_row(result)
    result["v4"]["ci_overlaps_next"] = False
    assert "无法可靠区分" not in _v4_leaderboard_row(result)


def test_v4_home_declares_five_fixed_judges() -> None:
    assert "5 位活动评委" in render_v4_home([], 0, preview=True)


def test_v4_outline_audit_requires_current_input_hash(tmp_path: Path) -> None:
    path = tmp_path / "outline-audit.json"
    identity = {
        "schema": "outline-audit.v4",
        "outline_input_hash": "fresh",
        "judge": "sol",
        "requested_model": "gpt-5.6-sol",
    }
    path.write_text(
        json.dumps(
            {
                **identity,
                "response_model": "gpt-5.6-sol",
                "cache_key": "current-key",
                "audit": {
                    "outline_quality": "结构完整",
                    "execution_fidelity": "正文兑现",
                    "major_deviations": ["无"],
                    "deviation_improved": "否",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert _load_v4_outline_audit(
        path, "fresh", expected_key="current-key", expected_identity=identity
    ) == {
        "outline_quality": "结构完整",
        "execution_fidelity": "正文兑现",
        "major_deviations": ["无"],
        "deviation_improved": "否",
    }
    assert _load_v4_outline_audit(
        path, "stale", expected_key="current-key", expected_identity=identity
    ) is None


def test_v4_default_gate_rejects_pilot_unsaturated_or_missing_ci_metadata() -> None:
    results = []
    for index, candidate in enumerate(("candidate-a", "candidate-b", "candidate-c")):
        v4 = _normalise_v4_aggregate(
            _aggregate(candidate, subscore=index + 2), benchmark="reform-era", candidate=candidate, input_hash="v4-work", provenance={"binding_hash": "current"}
        )
        results.append({"v4": v4})
    complete = {"status": "complete", "scope": "all", "eligible_for_default": True, "connected_graph": True}
    assert _v4_default_ranking_quality(complete, results)
    assert not _v4_default_ranking_quality({**complete, "scope": "pilot"}, results)
    assert not _v4_default_ranking_quality({**complete, "eligible_for_default": False}, results)
    assert not _v4_default_ranking_quality({k: v for k, v in complete.items() if k != "connected_graph"}, results)


def test_attach_v4_defaults_only_for_full_eligible_ranking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    results_dir = tmp_path / "results" / "reform-era"
    results = []
    ranking = []
    for index, candidate in enumerate(("candidate-a", "candidate-b", "candidate-c"), 1):
        aggregate = _aggregate(candidate, subscore=index + 1)
        path = results_dir / candidate / "scores-v4" / "aggregate.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(aggregate), encoding="utf-8")
        results.append({"model_id": candidate, "detail_available": True})
        ranking.append({"candidate": candidate, "rank": index, "rating": float(4 - index), "win_probability": 0.5, "rating_ci95": [-1.0, 1.0], "ci_overlaps_next": index == 1})
    ranking_path = results_dir / "_pairwise-v4" / "ranking.json"
    ranking_path.parent.mkdir(parents=True)
    monkeypatch.setattr("scripts.generate_site.load_v4_submission", lambda *_: SimpleNamespace(input_hash="v4-work", chapters=None))
    monkeypatch.setattr("scripts.generate_site.load_outline_audit_submission", lambda *_: SimpleNamespace(outline_input_hash="outline-work"))
    monkeypatch.setattr("scripts.generate_site.expected_aggregate_provenance", lambda *_: {"binding_hash": "current"})
    monkeypatch.setattr("scripts.generate_site.load_v4_pairwise_prompt", lambda *_: "pair prompt")
    monkeypatch.setattr("scripts.generate_site.V4_ALL_CANDIDATES", ("candidate-a", "candidate-b", "candidate-c"))
    monkeypatch.setattr("scripts.generate_site.V4_ALL_EDGE_COUNT", 2)
    pairs_dir = results_dir / "_pairwise-v4" / "pairs"
    pairs_dir.mkdir()
    pair_hashes = {}
    for pair_id, payload in (("a" * 20, {"edge": 1}), ("b" * 20, {"edge": 2})):
        (pairs_dir / f"{pair_id}.json").write_text(json.dumps(payload), encoding="utf-8")
        pair_hashes[pair_id] = _sha256_text(_canonical_json(payload))
    aggregate_hashes = {
        candidate: _sha256_text(_canonical_json(_aggregate(candidate, subscore=index + 1)))
        for index, candidate in enumerate(("candidate-a", "candidate-b", "candidate-c"), 1)
    }
    binding_payload = {
        "pairwise_rubric_hash": _sha256_text("pair prompt"),
        "aggregate_hashes": aggregate_hashes,
        "pair_record_hashes": pair_hashes,
    }
    metadata = {"schema": V4_RANKING_SCHEMA, "benchmark": "reform-era", "status": "complete", "scope": "all", "eligible_for_default": True, "connected_graph": True, "completed_edges": 2, "expected_edges": 2, "input_binding": {**binding_payload, "binding_hash": _sha256_text(_canonical_json(binding_payload))}, "ranking": ranking}
    ranking_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert attach_v4_results(results, results_dir)
    assert results[0]["v4"]["rank"] == 1

    tampered_pair = pairs_dir / f"{'a' * 20}.json"
    tampered_pair.write_text(json.dumps({"edge": 99}), encoding="utf-8")
    assert not attach_v4_results(results, results_dir)
    tampered_pair.write_text(json.dumps({"edge": 1}), encoding="utf-8")

    metadata["scope"] = "pilot"
    ranking_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert not attach_v4_results(results, results_dir)
