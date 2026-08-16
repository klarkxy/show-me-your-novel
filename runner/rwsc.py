"""Reliability-weighted standardized composites and residual profiles.

Shared by ``novel-reagg.v3`` and later ``novel-eval.v5``.  This module is
pure arithmetic: no files, no network, no judge prompts.
"""

from __future__ import annotations

import math
import random
from decimal import Decimal, ROUND_HALF_UP
from statistics import mean
from typing import Mapping, Sequence

_ONE_DECIMAL = Decimal("0.1")
BOOTSTRAP_DRAWS = 400
V3_SIGMA0 = 8.0
V3_SIGMA_FLOOR = 4.0
V5_SIGMA0 = 0.60
V5_SIGMA_FLOOR = 0.35
RESIDUAL_GAIN = 3.0
TIE_GAP = 2.0
WEIGHT_FLOOR = 0.05
WEIGHT_CEILING = 1.0
AGREE_FLOOR_WHEN_SPREAD = 0.20
AGREE_FLOOR_WHEN_FLAT = 0.05


def quantize_one_decimal(value: float) -> float:
    """Match ``score.overall_score_from_medians``: half-up to one decimal."""

    return float(Decimal(str(value)).quantize(_ONE_DECIMAL, rounding=ROUND_HALF_UP))


def quantile(values: Sequence[float], fraction: float) -> float:
    """Linear interpolation on sorted draws, same as ``compare_v4._quantile``."""

    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def midranks(values: Sequence[float]) -> list[float]:
    """1-based average ranks for ties (scipy / V4 percentile convention)."""

    n = len(values)
    order = sorted(range(n), key=lambda index: values[index])
    ranks = [0.0] * n
    index = 0
    while index < n:
        end = index
        while end + 1 < n and values[order[end + 1]] == values[order[index]]:
            end += 1
        average = (index + end) / 2.0 + 1.0
        for cursor in range(index, end + 1):
            ranks[order[cursor]] = average
        index = end + 1
    return ranks


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    mean_x = mean(xs)
    mean_y = mean(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x == 0.0 or den_y == 0.0:
        return 0.0
    return num / (den_x * den_y)


def spearman_midrank(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    return pearson(midranks(xs), midranks(ys))


def sample_sd(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        raise ValueError("sample_sd requires n>=2")
    centre = mean(values)
    return math.sqrt(sum((value - centre) ** 2 for value in values) / (n - 1))


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def judge_reliability(
    oriented: Mapping[str, Mapping[str, Mapping[str, float]]],
    books: Sequence[str],
    judges: Sequence[str],
    dimensions: Sequence[str],
    *,
    sigma0: float,
    sigma_floor: float,
) -> dict[str, dict[str, dict[str, float]]]:
    """Return per-judge per-dimension μ, σ, spread, agree, and weight.

    ``oriented[book][judge][dim]`` is already direction-corrected.
    """

    result: dict[str, dict[str, dict[str, float]]] = {
        judge: {} for judge in judges
    }
    for judge in judges:
        for dimension in dimensions:
            series = [oriented[book][judge][dimension] for book in books]
            mu = mean(series)
            sigma = sample_sd(series)
            spread = (sigma * sigma) / (sigma * sigma + sigma0 * sigma0)
            others = []
            for book in books:
                peer = [
                    oriented[book][peer_id][dimension]
                    for peer_id in judges
                    if peer_id != judge
                ]
                others.append(mean(peer) if peer else mu)
            agree = spearman_midrank(series, others)
            alpha = (
                AGREE_FLOOR_WHEN_SPREAD
                if sigma >= sigma_floor
                else AGREE_FLOOR_WHEN_FLAT
            )
            weight = _clip(spread * max(agree, alpha), WEIGHT_FLOOR, WEIGHT_CEILING)
            result[judge][dimension] = {
                "mu": mu,
                "sigma": sigma,
                "spread": spread,
                "agree": agree,
                "weight": weight,
            }
    return result


def dimension_tscores(
    oriented: Mapping[str, Mapping[str, Mapping[str, float]]],
    books: Sequence[str],
    judges: Sequence[str],
    dimensions: Sequence[str],
    reliability: Mapping[str, Mapping[str, Mapping[str, float]]],
    *,
    sigma_floor: float,
    weights: Mapping[str, float],
) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """Return ``T_id`` per book/dim and the weighted ``T_i`` per book."""

    per_dim: dict[str, dict[str, float]] = {book: {} for book in books}
    overall: dict[str, float] = {}
    for book in books:
        total = 0.0
        for dimension in dimensions:
            num = 0.0
            den = 0.0
            for judge in judges:
                stats = reliability[judge][dimension]
                sigma = max(stats["sigma"], sigma_floor)
                z = (oriented[book][judge][dimension] - stats["mu"]) / sigma
                num += stats["weight"] * z
                den += stats["weight"]
            z_bar = num / den if den else 0.0
            t_dim = quantize_one_decimal(50.0 + 10.0 * z_bar)
            per_dim[book][dimension] = t_dim
            total += float(weights[dimension]) * t_dim
        overall[book] = quantize_one_decimal(total)
    return per_dim, overall


def residual_profile(
    oriented: Mapping[str, Mapping[str, Mapping[str, float]]],
    books: Sequence[str],
    judges: Sequence[str],
    dimensions: Sequence[str],
    reliability: Mapping[str, Mapping[str, Mapping[str, float]]],
) -> dict[str, dict[str, float]]:
    """Spread-weighted within-ballot residuals ``R_id``."""

    residuals: dict[str, dict[str, float]] = {book: {} for book in books}
    for book in books:
        for dimension in dimensions:
            num = 0.0
            den = 0.0
            for judge in judges:
                ticket = [oriented[book][judge][key] for key in dimensions]
                residual = oriented[book][judge][dimension] - mean(ticket)
                spread = reliability[judge][dimension]["spread"]
                num += spread * residual
                den += spread
            residuals[book][dimension] = 0.0 if den == 0.0 else num / den
    return residuals


def residual_to_p(residual: float) -> float:
    return _clip(50.0 + RESIDUAL_GAIN * residual, 0.0, 100.0)


def percentile_from_scores(scores: Mapping[str, float]) -> dict[str, float]:
    books = list(scores)
    if len(books) == 1:
        return {books[0]: 50.0}
    if not books:
        return {}
    ranks = midranks([scores[book] for book in books])
    scale = len(books) - 1
    return {
        book: 100.0 * (rank - 1.0) / scale for book, rank in zip(books, ranks)
    }


def bootstrap_ci(
    oriented: Mapping[str, Mapping[str, Mapping[str, float]]],
    books: Sequence[str],
    judges: Sequence[str],
    dimensions: Sequence[str],
    *,
    sigma0: float,
    sigma_floor: float,
    weights: Mapping[str, float],
    seed: int,
    draws: int = BOOTSTRAP_DRAWS,
) -> dict[str, tuple[float, float]]:
    """Resample **books**, recompute μ/σ/w, apply those params to original scores."""

    rng = random.Random(seed)
    n = len(books)
    samples: dict[str, list[float]] = {book: [] for book in books}
    for _ in range(draws):
        drawn = [books[rng.randrange(n)] for _ in range(n)]
        try:
            reliability = judge_reliability(
                oriented,
                drawn,
                judges,
                dimensions,
                sigma0=sigma0,
                sigma_floor=sigma_floor,
            )
        except ValueError:
            for book in books:
                samples[book].append(50.0)
            continue
        _, overall = dimension_tscores(
            oriented,
            books,
            judges,
            dimensions,
            reliability,
            sigma_floor=sigma_floor,
            weights=weights,
        )
        for book in books:
            samples[book].append(overall[book])
    return {
        book: (quantile(values, 0.025), quantile(values, 0.975))
        for book, values in samples.items()
    }


def rank_books(
    overall: Mapping[str, float],
    config_order: Mapping[str, int],
) -> list[str]:
    return sorted(
        overall,
        key=lambda book: (-overall[book], config_order.get(book, 10**9), book),
    )


def adjacent_ties(
    ordered: Sequence[str],
    overall: Mapping[str, float],
    intervals: Mapping[str, tuple[float, float]],
) -> dict[str, bool]:
    flags = {book: False for book in ordered}
    for index in range(len(ordered) - 1):
        left, right = ordered[index], ordered[index + 1]
        gap = abs(overall[left] - overall[right])
        left_lo, left_hi = intervals[left]
        right_lo, right_hi = intervals[right]
        overlap = not (left_hi < right_lo or right_hi < left_lo)
        flags[left] = gap < TIE_GAP or overlap
    return flags
