"""Deterministic V3 re-aggregation.  Not a new judge protocol."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from runner.rwsc import (
    V3_SIGMA0,
    V3_SIGMA_FLOOR,
    adjacent_ties,
    bootstrap_ci,
    dimension_tscores,
    judge_reliability,
    percentile_from_scores,
    rank_books,
    residual_profile,
    residual_to_p,
)
from runner.score import DIMENSION_KEYS, DIMENSION_SPECS, JUDGE_IDS, dimension_radar_value


SCHEMA_VERSION = "novel-reagg.v3"
FORMULA_VERSION = "rwsc-residual-v1"
SOURCE_SCHEMA = "novel-eval.v3"
INSUFFICIENT = "insufficient"
COMPLETE = "complete"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def orient_score(dimension_key: str, value: float) -> float:
    return dimension_radar_value(dimension_key, value)


def _dimension_weights() -> dict[str, float]:
    return {spec.key: spec.weight for spec in DIMENSION_SPECS}


def extract_oriented_matrix(
    rows: list[Mapping[str, Any]],
) -> tuple[list[str], dict[str, dict[str, dict[str, float]]]]:
    books: list[str] = []
    oriented: dict[str, dict[str, dict[str, float]]] = {}
    for row in rows:
        if not row.get("rankable"):
            continue
        book = str(row["model_id"])
        judges = row.get("judges") or {}
        ticket: dict[str, dict[str, float]] = {}
        complete = True
        for judge_id in JUDGE_IDS:
            entry = judges.get(judge_id) or {}
            dimensions = entry.get("dimensions") if entry.get("valid") else None
            if not isinstance(dimensions, Mapping):
                complete = False
                break
            ticket[judge_id] = {}
            for key in DIMENSION_KEYS:
                cell = dimensions.get(key) or {}
                score = cell.get("score")
                if not isinstance(score, (int, float)):
                    complete = False
                    break
                ticket[judge_id][key] = orient_score(key, float(score))
            if not complete:
                break
        if not complete:
            continue
        books.append(book)
        oriented[book] = ticket
    return books, oriented


def _vote_and_aggregate_hashes(
    rows: list[Mapping[str, Any]],
    results_dir: Path | None,
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    vote_hashes: dict[str, dict[str, str]] = {}
    aggregate_hashes: dict[str, str] = {}
    for row in rows:
        book = str(row["model_id"])
        vote_hashes[book] = {}
        if results_dir is not None:
            for judge_id in JUDGE_IDS:
                path = results_dir / book / "scores" / f"{judge_id}.json"
                if path.is_file():
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    vote_hashes[book][judge_id] = _sha256_text(_canonical_json(payload))
            aggregate_path = results_dir / book / "scores" / "aggregate.json"
            if aggregate_path.is_file():
                payload = json.loads(aggregate_path.read_text(encoding="utf-8"))
                aggregate_hashes[book] = _sha256_text(_canonical_json(payload))
            continue
        judges = row.get("judges") or {}
        for judge_id in JUDGE_IDS:
            entry = judges.get(judge_id) or {}
            vote_hashes[book][judge_id] = _sha256_text(
                _canonical_json(entry.get("dimensions") or {})
            )
        aggregate_hashes[book] = _sha256_text(
            _canonical_json(
                {
                    "overall_score": row.get("overall_score"),
                    "dimensions": row.get("aggregate_dimensions") or {},
                }
            )
        )
    return vote_hashes, aggregate_hashes


def _binding_payload(
    books: list[str],
    vote_hashes: Mapping[str, Mapping[str, str]],
    aggregate_hashes: Mapping[str, str],
    *,
    benchmark: str,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "formula_version": FORMULA_VERSION,
        "benchmark": benchmark,
        "n": len(books),
        "source_schema": SOURCE_SCHEMA,
        "judge_ids": list(JUDGE_IDS),
        "books": books,
        "vote_hashes": {book: dict(vote_hashes.get(book) or {}) for book in books},
        "aggregate_hashes": {book: aggregate_hashes.get(book, "") for book in books},
    }


def empty_cohort(*, reason: str, benchmark: str = "reform-era") -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "formula_version": FORMULA_VERSION,
        "benchmark": benchmark,
        "status": INSUFFICIENT,
        "reason": reason,
        "n": 0,
        "books": [],
        "reliability": {},
        "tscores": {},
        "overall": {},
        "residuals": {},
        "residual_p": {},
        "percentiles": {},
        "ci95": {},
        "ordered": [],
        "ties_with_next": {},
        "binding_hash": "",
    }


def build_cohort(
    rows: list[Mapping[str, Any]],
    *,
    benchmark: str = "reform-era",
    results_dir: Path | None = None,
) -> dict[str, Any]:
    books, oriented = extract_oriented_matrix(rows)
    if len(books) < 2:
        return empty_cohort(reason="n<2", benchmark=benchmark)

    weights = _dimension_weights()
    reliability = judge_reliability(
        oriented,
        books,
        JUDGE_IDS,
        DIMENSION_KEYS,
        sigma0=V3_SIGMA0,
        sigma_floor=V3_SIGMA_FLOOR,
    )
    t_dims, overall = dimension_tscores(
        oriented,
        books,
        JUDGE_IDS,
        DIMENSION_KEYS,
        reliability,
        sigma_floor=V3_SIGMA_FLOOR,
        weights=weights,
    )
    residuals = residual_profile(
        oriented, books, JUDGE_IDS, DIMENSION_KEYS, reliability
    )
    residual_p = {
        book: {key: residual_to_p(value) for key, value in residual.items()}
        for book, residual in residuals.items()
    }
    config_order: dict[str, int] = {}
    for row in rows:
        raw_order = row.get("config_order")
        config_order[str(row["model_id"])] = (
            int(raw_order) if raw_order is not None else 10**9
        )
    ordered = rank_books(overall, config_order)
    vote_hashes, aggregate_hashes = _vote_and_aggregate_hashes(
        [row for row in rows if str(row["model_id"]) in set(books)],
        results_dir,
    )
    binding = _binding_payload(
        ordered, vote_hashes, aggregate_hashes, benchmark=benchmark
    )
    binding_hash = _sha256_text(_canonical_json(binding))
    seed = int(binding_hash[:16], 16)
    intervals = bootstrap_ci(
        oriented,
        books,
        JUDGE_IDS,
        DIMENSION_KEYS,
        sigma0=V3_SIGMA0,
        sigma_floor=V3_SIGMA_FLOOR,
        weights=weights,
        seed=seed,
    )
    ties = adjacent_ties(ordered, overall, intervals)
    percentiles: dict[str, dict[str, float]] = {book: {} for book in books}
    for key in DIMENSION_KEYS:
        axis = {book: t_dims[book][key] for book in books}
        mapped = percentile_from_scores(axis)
        for book, value in mapped.items():
            percentiles[book][key] = value

    compact_reliability = {
        judge: {
            key: {
                "sigma": stats["sigma"],
                "spread": stats["spread"],
                "agree": stats["agree"],
                "weight": stats["weight"],
            }
            for key, stats in reliability[judge].items()
        }
        for judge in JUDGE_IDS
    }
    return {
        "schema": SCHEMA_VERSION,
        "formula_version": FORMULA_VERSION,
        "benchmark": benchmark,
        "status": COMPLETE,
        "n": len(books),
        "books": ordered,
        "reliability": compact_reliability,
        "tscores": t_dims,
        "overall": overall,
        "residuals": residuals,
        "residual_p": residual_p,
        "percentiles": percentiles,
        "ci95": {book: list(intervals[book]) for book in books},
        "ordered": ordered,
        "ties_with_next": ties,
        "binding": binding,
        "binding_hash": binding_hash,
        "source_schema": SOURCE_SCHEMA,
    }


def attach_reagg_v3(
    results: list[dict[str, Any]],
    *,
    benchmark: str = "reform-era",
    results_dir: Path | None = None,
) -> dict[str, Any]:
    """Stamp each rankable row with cohort fields.  Non-rankable rows stay bare."""

    cohort = build_cohort(results, benchmark=benchmark, results_dir=results_dir)
    for row in results:
        book = str(row.get("model_id") or "")
        if cohort["status"] != COMPLETE or book not in cohort["overall"]:
            row["reagg"] = None
            continue
        residual_p = cohort["residual_p"][book]
        strongest = max(residual_p, key=lambda key: (residual_p[key], key))
        weakest = min(residual_p, key=lambda key: (residual_p[key], key))
        row["reagg"] = {
            "status": COMPLETE,
            "tscore": cohort["overall"][book],
            "t_dimensions": cohort["tscores"][book],
            "residual": cohort["residuals"][book],
            "residual_p": residual_p,
            "percentiles": cohort["percentiles"][book],
            "ci95": cohort["ci95"][book],
            "ties_with_next": bool(cohort["ties_with_next"].get(book)),
            "rank": cohort["ordered"].index(book) + 1,
            "strongest": strongest,
            "weakest": weakest,
            "binding_hash": cohort["binding_hash"],
            "n": cohort["n"],
        }
    return cohort
