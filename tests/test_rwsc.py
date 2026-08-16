from __future__ import annotations

import unittest

from runner.rwsc import (
    V3_SIGMA_FLOOR,
    V3_SIGMA0,
    adjacent_ties,
    bootstrap_ci,
    dimension_tscores,
    judge_reliability,
    midranks,
    percentile_from_scores,
    quantize_one_decimal,
    residual_profile,
    residual_to_p,
    sample_sd,
    spearman_midrank,
)


def _matrix(books, judges, dimensions, fill):
    return {
        book: {
            judge: {dim: float(fill(book, judge, dim)) for dim in dimensions}
            for judge in judges
        }
        for book in books
    }


class RwscUnitTests(unittest.TestCase):
    def test_quantize_half_up_matches_v3(self) -> None:
        self.assertEqual(quantize_one_decimal(50.05), 50.1)
        self.assertEqual(quantize_one_decimal(50.04), 50.0)

    def test_midranks_average_ties(self) -> None:
        self.assertEqual(midranks([10.0, 20.0, 20.0, 40.0]), [1.0, 2.5, 2.5, 4.0])

    def test_spearman_zero_variance_is_zero(self) -> None:
        self.assertEqual(spearman_midrank([1.0, 1.0, 1.0], [3.0, 4.0, 5.0]), 0.0)
        self.assertAlmostEqual(spearman_midrank([1.0, 2.0], [3.0, 4.0]), 1.0)
        self.assertAlmostEqual(spearman_midrank([1.0, 2.0], [4.0, 3.0]), -1.0)

    def test_sample_sd_requires_two_points(self) -> None:
        with self.assertRaises(ValueError):
            sample_sd([1.0])
        self.assertAlmostEqual(sample_sd([1.0, 3.0]), 2**0.5)

    def test_zero_sigma_weight_floor_and_zero_z(self) -> None:
        books = ("a", "b", "c")
        judges = ("sol", "grok")
        dims = ("theme_fulfillment",)
        oriented = _matrix(books, judges, dims, lambda *_: 80.0)
        rel = judge_reliability(
            oriented, books, judges, dims, sigma0=V3_SIGMA0, sigma_floor=V3_SIGMA_FLOOR
        )
        self.assertEqual(rel["sol"]["theme_fulfillment"]["sigma"], 0.0)
        self.assertEqual(rel["sol"]["theme_fulfillment"]["spread"], 0.0)
        self.assertEqual(rel["sol"]["theme_fulfillment"]["agree"], 0.0)
        self.assertEqual(rel["sol"]["theme_fulfillment"]["weight"], 0.05)
        t_dims, overall = dimension_tscores(
            oriented,
            books,
            judges,
            dims,
            rel,
            sigma_floor=V3_SIGMA_FLOOR,
            weights={"theme_fulfillment": 1.0},
        )
        self.assertEqual(t_dims["a"]["theme_fulfillment"], 50.0)
        self.assertEqual(overall["a"], 50.0)

    def test_all_spread_zero_residual_is_zero(self) -> None:
        books = ("a", "b")
        judges = ("sol", "grok")
        dims = ("theme_fulfillment", "style_control")
        oriented = _matrix(books, judges, dims, lambda *_: 70.0)
        rel = judge_reliability(
            oriented, books, judges, dims, sigma0=V3_SIGMA0, sigma_floor=V3_SIGMA_FLOOR
        )
        residuals = residual_profile(oriented, books, judges, dims, rel)
        self.assertEqual(residuals["a"]["theme_fulfillment"], 0.0)
        self.assertEqual(residual_to_p(-24.4), 0.0)
        self.assertEqual(residual_to_p(20.0), 100.0)
        self.assertEqual(residual_to_p(0.0), 50.0)

    def test_percentile_single_and_midrank(self) -> None:
        self.assertEqual(percentile_from_scores({"only": 12.0}), {"only": 50.0})
        mapped = percentile_from_scores({"lo": 1.0, "mid": 2.0, "hi": 3.0})
        self.assertEqual(mapped["lo"], 0.0)
        self.assertEqual(mapped["hi"], 100.0)
        self.assertEqual(mapped["mid"], 50.0)

    def test_adjacent_ties_use_quantized_gap_and_overlap(self) -> None:
        ordered = ["a", "b", "c"]
        overall = {"a": 60.0, "b": 58.1, "c": 50.0}
        intervals = {"a": (59.0, 61.0), "b": (57.0, 59.0), "c": (40.0, 45.0)}
        flags = adjacent_ties(ordered, overall, intervals)
        self.assertTrue(flags["a"])
        self.assertFalse(flags["b"])
        self.assertFalse(flags["c"])

    def test_bootstrap_is_book_unit_and_seeded(self) -> None:
        books = ("a", "b", "c")
        judges = ("sol",)
        dims = ("theme_fulfillment",)
        oriented = {
            "a": {"sol": {"theme_fulfillment": 90.0}},
            "b": {"sol": {"theme_fulfillment": 70.0}},
            "c": {"sol": {"theme_fulfillment": 50.0}},
        }
        first = bootstrap_ci(
            oriented,
            books,
            judges,
            dims,
            sigma0=V3_SIGMA0,
            sigma_floor=V3_SIGMA_FLOOR,
            weights={"theme_fulfillment": 1.0},
            seed=1,
            draws=20,
        )
        second = bootstrap_ci(
            oriented,
            books,
            judges,
            dims,
            sigma0=V3_SIGMA0,
            sigma_floor=V3_SIGMA_FLOOR,
            weights={"theme_fulfillment": 1.0},
            seed=1,
            draws=20,
        )
        self.assertEqual(first, second)
        self.assertLess(first["a"][0], first["a"][1])
