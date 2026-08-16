from __future__ import annotations

import json
import unittest
from pathlib import Path

from runner.reagg_v3 import (
    COMPLETE,
    INSUFFICIENT,
    attach_reagg_v3,
    build_cohort,
    orient_score,
)
from runner.rwsc import pearson
from runner.score import DIMENSION_KEYS, JUDGE_IDS


REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS = REPO_ROOT / "results" / "reform-era"


def _load_rankable_rows() -> list[dict]:
    rows: list[dict] = []
    config_order = 0
    for path in sorted(RESULTS.iterdir()):
        if not path.is_dir() or path.name.startswith("_"):
            continue
        aggregate_path = path / "scores" / "aggregate.json"
        if not aggregate_path.is_file():
            continue
        aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
        if (
            aggregate.get("status") != "complete"
            or aggregate.get("eligible_for_ranking") is not True
        ):
            continue
        judges = {}
        complete = True
        for judge_id in JUDGE_IDS:
            ballot_path = path / "scores" / f"{judge_id}.json"
            if not ballot_path.is_file():
                complete = False
                break
            raw = json.loads(ballot_path.read_text(encoding="utf-8"))
            dimensions = raw.get("dimensions")
            if not isinstance(dimensions, dict):
                complete = False
                break
            judges[judge_id] = {"valid": True, "dimensions": dimensions}
        if not complete or set(judges) != set(JUDGE_IDS):
            continue
        rows.append(
            {
                "model_id": path.name,
                "config_order": config_order,
                "rankable": True,
                "judges": judges,
                "overall_score": aggregate.get("overall_score"),
                "aggregate_dimensions": aggregate.get("dimensions") or {},
            }
        )
        config_order += 1
    return rows


class ReaggV3GoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = _load_rankable_rows()
        if len(cls.rows) < 10:
            raise unittest.SkipTest("仓库里没有足够的齐套 V3 票")
        cls.cohort = build_cohort(
            cls.rows, benchmark="reform-era", results_dir=RESULTS
        )

    def test_insufficient_when_fewer_than_two_books(self) -> None:
        empty = build_cohort([], benchmark="reform-era")
        self.assertEqual(empty["status"], INSUFFICIENT)
        one = build_cohort(self.rows[:1], benchmark="reform-era")
        self.assertEqual(one["status"], INSUFFICIENT)

    def test_cohort_complete_on_live_ballots(self) -> None:
        self.assertEqual(self.cohort["status"], COMPLETE)
        self.assertGreaterEqual(self.cohort["n"], 10)
        self.assertTrue(self.cohort["binding_hash"])

    def test_prototype_head_and_tail(self) -> None:
        overall = self.cohort["overall"]
        ordered = self.cohort["ordered"]
        self.assertEqual(ordered[0], "gpt-5.6-sol")
        self.assertEqual(ordered[1], "gpt-5.6-terra")
        self.assertEqual(ordered[-1], "minimax-m3")
        self.assertAlmostEqual(overall["gpt-5.6-sol"], 64.7, delta=0.15)
        self.assertAlmostEqual(overall["gpt-5.6-terra"], 61.5, delta=0.15)
        self.assertAlmostEqual(overall["deepseek-v4-pro"], 57.9, delta=0.15)
        self.assertAlmostEqual(overall["gpt-5.6-luna"], 57.85, delta=0.15)
        self.assertAlmostEqual(overall["minimax-m3"], 35.9, delta=0.15)

    def test_luna_tied_with_deepseek(self) -> None:
        self.assertTrue(self.cohort["ties_with_next"]["deepseek-v4-pro"])
        gaps = [
            abs(
                self.cohort["overall"][self.cohort["ordered"][index]]
                - self.cohort["overall"][self.cohort["ordered"][index + 1]]
            )
            for index in range(len(self.cohort["ordered"]) - 1)
        ]
        self.assertGreaterEqual(sum(1 for gap in gaps if gap < 2.0), 4)

    def test_residual_span_and_sol_teeth(self) -> None:
        residuals = self.cohort["residuals"]
        wide = 0
        for book, axis in residuals.items():
            span = max(axis.values()) - min(axis.values())
            if span >= 8:
                wide += 1
        self.assertGreaterEqual(wide, 10)
        sol = residuals["gpt-5.6-sol"]
        self.assertGreaterEqual(max(sol.values()) - min(sol.values()), 10)
        sol_p = self.cohort["residual_p"]["gpt-5.6-sol"]
        self.assertGreaterEqual(max(sol_p.values()) - min(sol_p.values()), 25)
        self.assertAlmostEqual(sol_p["ai_flavor"], 26.0, delta=2.0)

    def test_halo_drops_on_residuals(self) -> None:
        books = self.cohort["books"]
        residual = self.cohort["residuals"]

        def series(key: str) -> list[float]:
            return [residual[book][key] for book in books]

        self.assertLess(pearson(series("characters"), series("style_control")), 0.70)
        self.assertLess(pearson(series("characters"), series("theme_fulfillment")), 0.70)

    def test_grok_scene_is_downweighted(self) -> None:
        rel = self.cohort["reliability"]
        grok_w = rel["grok"]["scene_execution"]["weight"]
        sol_w = rel["sol"]["scene_execution"]["weight"]
        self.assertLess(grok_w / sol_w, 0.20)
        self.assertLess(rel["grok"]["scene_execution"]["spread"], 0.15)

    def test_binding_changes_when_ballot_comment_changes(self) -> None:
        path = RESULTS / "gpt-5.6-sol" / "scores" / "sol.json"
        original = path.read_text(encoding="utf-8")
        first = self.cohort["binding_hash"]
        try:
            payload = json.loads(original)
            payload["dimensions"]["theme_fulfillment"]["comment"] = (
                payload["dimensions"]["theme_fulfillment"]["comment"] + " "
            )
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            changed = build_cohort(
                self.rows, benchmark="reform-era", results_dir=RESULTS
            )["binding_hash"]
            self.assertNotEqual(first, changed)
        finally:
            path.write_text(original, encoding="utf-8")

    def test_attach_stamps_rows_and_skips_unrankable(self) -> None:
        rows = [dict(row) for row in self.rows]
        rows.append(
            {
                "model_id": "pending-model",
                "config_order": 99,
                "rankable": False,
                "judges": {},
            }
        )
        cohort = attach_reagg_v3(rows, benchmark="reform-era", results_dir=RESULTS)
        self.assertEqual(cohort["status"], COMPLETE)
        self.assertIsNone(rows[-1]["reagg"])
        stamped = next(row for row in rows if row["model_id"] == "gpt-5.6-sol")
        self.assertEqual(stamped["reagg"]["rank"], 1)
        self.assertEqual(stamped["reagg"]["n"], cohort["n"])

    def test_orient_inverts_only_ai_flavor(self) -> None:
        self.assertEqual(orient_score("theme_fulfillment", 80.0), 80.0)
        self.assertEqual(orient_score("ai_flavor", 20.0), 80.0)
