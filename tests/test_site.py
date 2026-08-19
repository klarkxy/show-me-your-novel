from __future__ import annotations

import json
import hashlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from runner.generate import PROTOCOL_POLICY, count_content_chars as count_protocol_chars
from runner.score import (
    AGGREGATE_SCHEMA_VERSION,
    DIMENSION_SPECS,
    JUDGE_IDS,
    SCHEMA_VERSION,
    ScoreError,
    aggregate_dimension_scores,
    load_submission,
    overall_score_from_medians,
)

from scripts.generate_site import (
    build_protocol_expectations,
    build_score_expectations,
    build_site,
    count_chapters,
    count_chinese_chars,
    main,
    md_to_html,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL_IDS = [f"model-{letter}" for letter in "abcdefghijklmno"]


class SiteGenerationTests(unittest.TestCase):
    def _write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_cli_rejects_parent_and_source_directories_as_publish_targets(self) -> None:
        self.assertEqual(main(["--docs-dir", ".."]), 1)
        self.assertEqual(main(["--docs-dir", "runner"]), 1)
        self.assertEqual(main(["--docs-dir", "docs"]), 1)

    def _write_score(
        self,
        model_dir: Path,
        judge_id: str,
        *,
        score: float = 90.4,
        ai_flavor: float = 10.2,
        input_hash: str = "work-hash",
    ) -> None:
        root = model_dir.parents[2]
        config_path = root / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        expected = build_score_expectations(config_path, config)[judge_id]
        self._write_json(
            model_dir / "scores" / f"{judge_id}.json",
            {
                "schema": SCHEMA_VERSION,
                "benchmark": "reform-era",
                "candidate": model_dir.name,
                "judge": judge_id,
                "input_hash": input_hash,
                "cache_key": f"cache-{model_dir.name}-{judge_id}",
                **expected,
                "response_model": f"{judge_id}-response",
                "dimensions": {
                    spec.key: {
                        "score": float(ai_flavor if spec.key == "ai_flavor" else score),
                        "comment": f"{judge_id} 对{spec.label}的简评 <b data-test=1>",
                    }
                    for spec in DIMENSION_SPECS
                },
            },
        )

    def _write_result(
        self,
        results: Path,
        model_id: str,
        *,
        status: str = "completed",
        judges: tuple[str, ...] | None = None,
        aggregate: bool | None = True,
        omit: str | None = None,
        score: float = 90.4,
    ) -> Path:
        model = results / model_id
        book = {
            "title": f"潮汐<{model_id}>",
            "blurb": "简" * 320 + "<script>alert(1)</script>",
            "protagonist": "何某，县城工人",
            "setting": "改革开放初期的中国南方",
            "core_theme": "机会、责任与人的选择",
            "ending_direction": "主人公承担改变带来的代价",
        }
        macro = {
            "target_total_chars": 2_000_000,
            "volumes": [
                {
                    "number": number,
                    "title": f"第{number}卷",
                    "target_chars": 200_000,
                    "period": f"阶段{number}",
                    "start_state": "旧秩序仍有惯性",
                    "end_state": "人物关系发生变化",
                    "main_conflict": "利益与责任冲突 <svg onload=alert(2)>",
                    "arcs": [
                        {"title": f"弧{arc}", "summary": "人物行动并承担后果"}
                        for arc in range(1, 4)
                    ],
                }
                for number in range(1, 11)
            ],
            "character_arcs": ["主人公从被动承担走向主动选择"],
            "foreshadowing": ["第一卷旧收据在末卷回收"],
            "ending": "主要人物完成长期弧线",
        }
        opening = {
            "target_total_chars": 50_000,
            "macro_scope": "第一卷开端",
            "chapters": [
                {
                    "number": number,
                    "title": "开端" if number == 1 else f"潮声{number}",
                    "target_chars": 3_125,
                    "summary": "主人公面对一个具体选择",
                    "beats": ["进入场景", "冲突发生", "做出选择"],
                    "continuity_in": [f"进入本章时状态为{number}"],
                    "continuity_out": [f"本章结束后状态为{number + 1}"],
                    "foreshadowing": ["旧收据"],
                }
                for number in range(1, 17)
            ],
        }
        if omit != "book.json":
            self._write_json(model / "book.json", book)
        if omit != "macro_outline.json":
            self._write_json(model / "macro_outline.json", macro)
        if omit != "opening_outline.json":
            self._write_json(model / "opening_outline.json", opening)
        chapter_texts: list[str] = []
        if omit != "novel.md":
            chapter_dir = model / "chapters"
            chapter_dir.mkdir(parents=True, exist_ok=True)
            for chapter in opening["chapters"]:
                number = chapter["number"]
                text = (
                    f"## 第{number}章 {chapter['title']}\n\n"
                    f"{'汉' * 3000} <script>alert(3)</script>。"
                )
                chapter_texts.append(text)
                (chapter_dir / f"{number:02d}.md").write_text(
                    text + "\n", encoding="utf-8"
                )
            novel = f"# {book['title']}\n\n" + "\n\n".join(chapter_texts).rstrip() + "\n"
            (model / "novel.md").write_text(
                novel,
                encoding="utf-8",
            )
        if omit != "manifest.json":
            artifact_names = [
                "book.json",
                "macro_outline.json",
                "opening_outline.json",
                "novel.md",
                *[f"chapters/{number:02d}.md" for number in range(1, 17)],
            ]
            artifact_hashes = {
                name: hashlib.sha256(
                    (model / name)
                    .read_bytes()
                    .decode("utf-8-sig")
                    .replace("\r\n", "\n")
                    .replace("\r", "\n")
                    .encode("utf-8")
                ).hexdigest()
                for name in artifact_names
                if (model / name).is_file()
            }
            root = results.parents[1]
            config_data = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
            model_by_id = {item["id"]: item for item in config_data["models"]}
            protocol = build_protocol_expectations(root / "config.yaml", model_by_id)[model_id]
            chapter_chars = [count_protocol_chars(text) for text in chapter_texts]
            attempts = {
                "book": 1,
                "macro_outline": 1,
                "opening_outline": 1,
                **{f"chapter_{number:02d}": 1 for number in range(1, 17)},
            }
            self._write_json(
                model / "manifest.json",
                {
                    "status": status,
                    "manuscript_completed_at": "2026-08-13T01:02:03+00:00",
                    "completed_at": "2026-08-13T01:02:03+00:00",
                    "protocol_policy": PROTOCOL_POLICY,
                    "run_origin": "fresh",
                    "code_sha256": "0" * 64,
                    "artifact_sha256": artifact_hashes,
                    "body_chars": sum(chapter_chars),
                    "chapters": [
                        {
                            "number": chapter["number"],
                            "title": chapter["title"],
                            "chars": chars,
                            "attempt_count": 1,
                            "retry_count": 0,
                            "initial_chars": chars,
                            "expansion_requested": False,
                            "expansion_attempt_count": 0,
                            "expansion_result_chars": None,
                            "expansion_adopted": False,
                            "expansion_outcome": "not_needed",
                        }
                        for chapter, chars in zip(opening["chapters"], chapter_chars)
                    ],
                    "attempts": attempts,
                    "retry_count": 0,
                    "usage": {"calls": 19},
                    "context_audit": {
                        "calls": 19,
                        "estimate_sources": {
                            "fallback": 1,
                            "provider_usage_anchor": 18,
                        },
                    },
                    **protocol,
                },
            )
        root = results.parents[1]
        try:
            score_input_hash = load_submission(
                root, "reform-era", model_id
            ).input_hash
        except (ScoreError, OSError, ValueError):
            score_input_hash = "work-hash"
        active_judges = judges if judges is not None else tuple(JUDGE_IDS)
        for judge_id in active_judges:
            self._write_score(
                model, judge_id, score=score, input_hash=score_input_hash
            )
        if aggregate is not None:
            aggregate_complete = aggregate and set(active_judges) == set(JUDGE_IDS)
            aggregate_judges = {
                judge_id: {
                    "dimensions": json.loads(
                        (model / "scores" / f"{judge_id}.json").read_text(
                            encoding="utf-8"
                        )
                    )["dimensions"]
                }
                for judge_id in active_judges
            }
            aggregate_dimensions = (
                aggregate_dimension_scores(
                    {
                        judge_id: aggregate_judges[judge_id]["dimensions"]
                        for judge_id in JUDGE_IDS
                    }
                )
                if aggregate_complete
                else {}
            )
            self._write_json(
                model / "scores" / "aggregate.json",
                {
                    "schema": AGGREGATE_SCHEMA_VERSION,
                    "benchmark": "reform-era",
                    "candidate": model_id,
                    "input_hash": score_input_hash,
                    "expected_judges": list(JUDGE_IDS),
                    "completed_judges": list(active_judges),
                    "status": "complete" if aggregate_complete else "incomplete",
                    "eligible_for_ranking": aggregate_complete,
                    "judges": aggregate_judges,
                    "dimensions": aggregate_dimensions,
                    "overall_score": (
                        overall_score_from_medians(aggregate_dimensions)
                        if aggregate_complete
                        else None
                    ),
                },
            )
        return model

    def _refresh_manifest_artifacts(self, model: Path) -> dict:
        manifest_path = model / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifact_sha256"] = {
            name: hashlib.sha256(
                (model / name)
                .read_bytes()
                .decode("utf-8-sig")
                .replace("\r\n", "\n")
                .replace("\r", "\n")
                .encode("utf-8")
            ).hexdigest()
            for name in manifest["artifact_sha256"]
        }
        self._write_json(manifest_path, manifest)
        return manifest

    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        config = root / "config.yaml"
        names = {"model-a": "Zulu <script>", "model-b": "Alpha"}
        config.write_text(
            "providers:\n"
            "  new-api:\n"
            "    request_defaults: {}\n"
            "models:\n"
            + "".join(
                f"  - id: {model_id}\n"
                f"    name: {names.get(model_id, model_id.upper())}\n"
                f"    model: {model_id}\n"
                for model_id in MODEL_IDS
            )
            + "judges:\n"
            + "".join(
                f"  - id: {judge_id}\n"
                f"    name: {judge_id.title()}\n"
                f"    model: {judge_id}-wire\n"
                f"    provider: new-api\n"
                for judge_id in (*JUDGE_IDS, "kimi")
            ),
            encoding="utf-8",
        )
        direction = root / "benchmark" / "reform-era" / "direction.md"
        direction.parent.mkdir(parents=True)
        direction.write_text("改革开放初期的中国现实主义长篇。\n", encoding="utf-8")
        prompt_dir = root / "runner" / "prompts" / "v2"
        prompt_dir.mkdir(parents=True)
        for name in (
            "system.md",
            "book.md",
            "macro_outline.md",
            "opening_outline.md",
            "chapter.md",
            "expand_chapter.md",
            "repair_json.md",
            "repair_chapter.md",
        ):
            (prompt_dir / name).write_text(f"fixture {name}\n", encoding="utf-8")
        (prompt_dir / "judge_system.md").write_text(
            "fixture judge rubric\n{{DIMENSION_SPECS}}\n", encoding="utf-8"
        )

        novels = root / "novels"
        legacy = novels / "legacy-one"
        legacy.mkdir(parents=True)
        (legacy / "prompt.md").write_text(
            "# 旧题材\n\n## 题材\n年代\n\n## 世界观设定\n旧版测试。\n",
            encoding="utf-8",
        )
        (legacy / "retired-model.md").write_text(
            "# 旧作\n\n[思考过程]\nPRIVATE_LEGACY\n## 第99章 假章节\n"
            "[/思考过程]\n\n## 大纲\n不属于正文。\n\n"
            "## 第1章 开端\n\n真正正文。\n",
            encoding="utf-8",
        )
        (legacy / "model-a.md").write_text(
            "# 旧作 A\n\n## 第1章 开端\n\n另一个正文。\n", encoding="utf-8"
        )

        results = root / "results" / "reform-era"
        # A and B deliberately tie. Config order, not display name, must win.
        self._write_result(results, "model-a", score=82.0, aggregate=True)
        self._write_result(results, "model-b", score=82.0, aggregate=True)
        # An explicitly incomplete aggregate must block ranking.
        self._write_result(results, "model-c", score=70.0, aggregate=False)
        # Missing one judge blocks ranking, but not the detail page.
        self._write_result(results, "model-d", judges=("sol",), aggregate=None)
        # Status and tracked-artifact failures both block detail publication.
        self._write_result(results, "model-e", status="in_progress")
        self._write_result(results, "model-f", omit="opening_outline.json")
        # Complete source votes without their aggregate record stay unranked.
        self._write_result(results, "model-h", score=77.0, aggregate=None)
        return config, novels, results

    @staticmethod
    def _row(home: str, model_id: str) -> str:
        match = re.search(
            rf'<tr data-model-id="{re.escape(model_id)}".*?</tr>', home, re.DOTALL
        )
        if not match:
            raise AssertionError(f"missing row for {model_id}")
        return match.group(0)

    def test_reasoning_and_headings_are_excluded_from_public_prose(self) -> None:
        text = (
            "# 标题\n\n[思考过程]\nPRIVATE <img src=x>\n## 第99章 假\n"
            "[/思考过程]\n\n## 大纲\n大纲文字\n\n## 第1章 真\n\n正文A1。"
        )
        rendered = md_to_html(text)
        self.assertNotIn("PRIVATE", rendered)
        self.assertNotIn("img src", rendered)
        self.assertNotIn("思考过程", rendered)
        self.assertIn("正文A1", rendered)
        self.assertEqual(count_chapters(text), 1)
        self.assertEqual(count_chinese_chars(text), len("正文A1"))

        unclosed = md_to_html("# 标题\n\n正文。\n\n[思考过程]\nPRIVATE")
        self.assertIn("正文", unclosed)
        self.assertNotIn("PRIVATE", unclosed)

    def test_active_judge_median_uses_middle_vote(self) -> None:
        def dimensions(score: float, ai_flavor: float) -> dict[str, dict]:
            return {
                spec.key: {
                    "score": ai_flavor if spec.key == "ai_flavor" else score,
                    "comment": "测试",
                }
                for spec in DIMENSION_SPECS
            }

        scores = (70.0, 82.1, 95.0, 60.0, 85.0)
        ai_scores = (5.0, 10.3, 30.0, 8.0, 20.0)
        aggregate = aggregate_dimension_scores(
            {
                judge_id: dimensions(score, ai)
                for judge_id, score, ai in zip(JUDGE_IDS, scores, ai_scores)
            }
        )
        self.assertEqual(aggregate["theme_fulfillment"]["median"], 82.1)
        self.assertEqual(aggregate["theme_fulfillment"]["min"], 60.0)
        self.assertEqual(aggregate["theme_fulfillment"]["max"], 95.0)
        self.assertEqual(aggregate["ai_flavor"]["median"], 10.3)

    def test_build_lists_all_models_and_strictly_gates_rank_and_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, novels, results = self._fixture(root)
            output = root / "public"
            summary = build_site(
                config_path=config,
                novels_dir=novels,
                results_dir=results,
                assets_dir=REPO_ROOT / "site" / "assets",
                output_dir=output,
            )

            self.assertEqual(
                summary,
                {
                    "results": 15,
                    "opening_novels": 0,
                    "legacy_stories": 1,
                    "legacy_versions": 2,
                },
            )
            home = (output / "index.html").read_text(encoding="utf-8")
            opening_index = (output / "opening" / "index.html").read_text(encoding="utf-8")
            self.assertIn("开局", home)
            self.assertIn("尚无文风评分", opening_index)
            self.assertEqual(home.count("data-model-id="), 15)
            self.assertIn("全部 15", home)
            self.assertIn(f"评委 {len(JUDGE_IDS)}", home)
            self.assertIn("活动评委票的中位数", home)
            self.assertIn("V3 重聚合", home)
            self.assertIn("非新评", home)
            self.assertIn('data-sort="tscore"', home)
            self.assertIn("评分怎么算", home)
            self.assertIn("Content-Security-Policy", home)
            self.assertNotIn("Zulu <script>", home)
            self.assertIn("Zulu &lt;script&gt;", home)
            self.assertNotIn("data-grok=", home)
            self.assertNotIn('data-sort="grok"', home)
            self.assertIn("data-theme-fulfillment=", home)
            self.assertIn('data-sort="theme-fulfillment"', home)
            self.assertIn("文风管理", home)
            self.assertIn("AI味（越低越好）", home)
            self.assertIn(">82.0</td>", home)
            self.assertIn('data-metric="overall"', home)
            self.assertIn("Claude Opus 5", home)
            self.assertIn("Kimi K3", home)
            self.assertIn("DeepSeek V4 Pro", home)
            self.assertNotIn("Fable", home)

            row_a = self._row(home, "model-a")
            row_b = self._row(home, "model-b")
            row_c = self._row(home, "model-c")
            row_d = self._row(home, "model-d")
            row_e = self._row(home, "model-e")
            row_f = self._row(home, "model-f")
            row_g = self._row(home, "model-g")
            row_h = self._row(home, "model-h")
            self.assertIn('data-rankable="true"', row_a)
            self.assertIn('data-rankable="true"', row_b)
            self.assertIn('data-rankable="false"', row_c)
            self.assertIn("data-rank>01", row_a)
            self.assertIn("data-rank>02", row_b)
            self.assertIn('data-tie-next="true"', row_a)
            self.assertIn('data-tie-next="false"', row_b)
            self.assertLess(home.index(row_a), home.index(row_b))
            for row in (row_c, row_d, row_e, row_f, row_g, row_h):
                self.assertIn('data-rankable="false"', row)
                self.assertIn('data-rank></td>', row)

            # A completed manuscript gets a detail page even if scoring is pending.
            for model_id in ("model-a", "model-b", "model-c", "model-d", "model-h"):
                self.assertIn(f"results/reform-era/{model_id}.html", home)
                self.assertTrue(
                    (output / "results" / "reform-era" / f"{model_id}.html").is_file()
                )
            for model_id in ("model-e", "model-f", "model-g"):
                self.assertNotIn(f"results/reform-era/{model_id}.html", home)
                self.assertFalse(
                    (output / "results" / "reform-era" / f"{model_id}.html").exists()
                )

            detail = (output / "results" / "reform-era" / "model-a.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", detail)
            self.assertIn("&lt;svg onload=alert(2)&gt;", detail)
            self.assertIn("&lt;script&gt;alert(3)&lt;/script&gt;", detail)
            self.assertNotIn("PRIVATE_REASONING", detail)
            self.assertNotIn("第99章", detail)
            self.assertNotIn("<script>alert", detail)
            self.assertIn("Grok 4.6", detail)
            self.assertIn("Claude Opus 5", detail)
            self.assertIn("Kimi K3", detail)
            self.assertIn("DeepSeek V4 Pro", detail)
            self.assertNotIn("Fable", detail)
            self.assertIn("活动评委维度中位数", detail)
            self.assertIn("活动评委逐维记录", detail)
            self.assertIn('href="#novel-title"', detail)
            self.assertLess(detail.index('id="novel-title"'), detail.index('id="scores"'))
            self.assertIn("相对本书均值", detail)
            self.assertIn('data-series-kind="residual-p"', detail)
            self.assertIn("history-radar", detail)
            self.assertIn('class="judge-drawer"', detail)
            self.assertIn("<summary>Sol</summary>", detail)
            self.assertIn("<summary>Grok 4.6</summary>", detail)
            self.assertIn("<summary>Claude Opus 5</summary>", detail)
            radar_count = 2 + 1 + len(JUDGE_IDS)
            self.assertEqual(detail.count('data-radar-chart="'), radar_count)
            self.assertEqual(detail.count('class="radar-chart"'), radar_count)
            self.assertEqual(
                detail.count("radar-axis-label-full"), 8 * radar_count
            )
            self.assertEqual(
                detail.count("radar-axis-label-short"), 8 * radar_count
            )
            self.assertEqual(
                detail.count('class="dimension-comment"'), 8 * len(JUDGE_IDS)
            )
            self.assertEqual(detail.count('class="radar-axis"'), 8 * radar_count)
            self.assertEqual(detail.count("<title id="), radar_count)
            self.assertEqual(detail.count("<desc id="), radar_count)
            self.assertIn("AI味（越低越好）", detail)
            self.assertIn("雷达按控制度89.8绘制", detail)
            self.assertNotIn("NaN", detail)
            self.assertIn("&lt;b data-test=1&gt;", detail)
            self.assertNotIn("<b data-test=1>", detail)

    def test_archived_manuscript_keeps_reviews_is_browsable_and_never_ranks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, novels, results = self._fixture(root)
            current = results / "model-a"
            archived_copy = root / "archived-copy"
            shutil.copytree(current, archived_copy)
            archived_manifest_path = archived_copy / "manifest.json"
            archived_manifest = json.loads(archived_manifest_path.read_text(encoding="utf-8"))
            archived_manifest["manuscript_completed_at"] = "2026-07-01T16:30:00+00:00"
            archived_manifest["completed_at"] = "2026-07-01T16:30:00+00:00"
            self._write_json(archived_manifest_path, archived_manifest)
            archive_id = "20260701163000-oldrun"
            archived = current / "archive" / archive_id
            archived.parent.mkdir()
            archived_copy.replace(archived)
            self._write_json(
                archived / "archive.json",
                {
                    "schema": "novel-benchmark-archive.v1",
                    "archive_id": archive_id,
                    "ranking_status": "archived",
                    "eligible_for_ranking": False,
                },
            )
            historical_ids = ("sol", "grok", "fable")
            opus_score = json.loads(
                (archived / "scores" / "opus.json").read_text(encoding="utf-8")
            )
            opus_score["judge"] = "fable"
            opus_score["requested_model"] = "claude-fable-5"
            opus_score["response_model"] = "claude-fable-5"
            opus_score["cache_key"] = "historical-fable"
            self._write_json(archived / "scores" / "fable.json", opus_score)
            archived_aggregate_path = archived / "scores" / "aggregate.json"
            archived_aggregate = json.loads(
                archived_aggregate_path.read_text(encoding="utf-8")
            )
            archived_aggregate["expected_judges"] = list(historical_ids)
            archived_aggregate["completed_judges"] = list(historical_ids)
            self._write_json(archived_aggregate_path, archived_aggregate)

            output = root / "public"
            build_site(
                config_path=config,
                novels_dir=novels,
                results_dir=results,
                assets_dir=REPO_ROOT / "site" / "assets",
                output_dir=output,
            )

            home = (output / "index.html").read_text(encoding="utf-8")
            self.assertEqual(home.count('data-model-id="model-a"'), 1)
            row = self._row(home, "model-a")
            self.assertIn("成稿 2026-08-13", row)
            self.assertIn("历史稿 1", row)

            current_page = (
                output / "results" / "reform-era" / "model-a.html"
            ).read_text(encoding="utf-8")
            self.assertIn("历史成稿", current_page)
            self.assertIn(f"archive/model-a/{archive_id}.html", current_page)
            self.assertIn("成稿 2026-07-02", current_page)

            archive_page_path = (
                output
                / "results"
                / "reform-era"
                / "archive"
                / "model-a"
                / f"{archive_id}.html"
            )
            self.assertTrue(archive_page_path.is_file())
            archive_page = archive_page_path.read_text(encoding="utf-8")
            self.assertIn("这是历史成稿及其原评审快照", archive_page)
            self.assertIn("已退出当前排名", archive_page)
            self.assertIn("成稿 2026-07-02", archive_page)
            self.assertIn("Sol", archive_page)
            self.assertIn("Fable", archive_page)
            self.assertIn("sol 对题材与主题兑现的简评", archive_page)

    def test_legacy_keeps_retired_model_routes_and_never_publishes_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, novels, results = self._fixture(root)
            output = root / "public"
            build_site(
                config_path=config,
                novels_dir=novels,
                results_dir=results,
                assets_dir=REPO_ROOT / "site" / "assets",
                output_dir=output,
            )

            index = (output / "novels" / "legacy-one" / "index.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("retired-model.html", index)
            self.assertIn("retired-model", index)
            legacy = (
                output / "novels" / "legacy-one" / "retired-model.html"
            ).read_text(encoding="utf-8")
            self.assertIn("真正正文", legacy)
            self.assertNotIn("PRIVATE_LEGACY", legacy)
            self.assertNotIn("思考过程", legacy)
            self.assertNotIn("第99章", legacy)
            self.assertNotIn("不属于正文", legacy)
            self.assertIn("1 章", legacy)

    def test_public_score_with_private_extra_field_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, novels, results = self._fixture(root)
            score_path = results / "model-a" / "scores" / "sol.json"
            score = json.loads(score_path.read_text(encoding="utf-8"))
            score["reasoning_content"] = "PRIVATE_JUDGE_REASONING"
            self._write_json(score_path, score)

            output = root / "public"
            build_site(
                config_path=config,
                novels_dir=novels,
                results_dir=results,
                assets_dir=REPO_ROOT / "site" / "assets",
                output_dir=output,
            )

            home = (output / "index.html").read_text(encoding="utf-8")
            row = self._row(home, "model-a")
            self.assertIn('data-rankable="false"', row)
            detail = (output / "results" / "reform-era" / "model-a.html").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("PRIVATE_JUDGE_REASONING", detail)

    def test_manifest_content_hash_blocks_tampered_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, novels, results = self._fixture(root)
            novel = results / "model-a" / "novel.md"
            novel.write_text(novel.read_text(encoding="utf-8") + "\n被改动。", encoding="utf-8")
            output = root / "public"
            build_site(
                config_path=config,
                novels_dir=novels,
                results_dir=results,
                assets_dir=REPO_ROOT / "site" / "assets",
                output_dir=output,
            )

            home = (output / "index.html").read_text(encoding="utf-8")
            row = self._row(home, "model-a")
            self.assertIn('data-rankable="false"', row)
            self.assertNotIn("results/reform-era/model-a.html", row)
            self.assertFalse(
                (output / "results" / "reform-era" / "model-a.html").exists()
            )

    def test_protocol_hash_change_hides_stale_completed_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, novels, results = self._fixture(root)
            (root / "benchmark" / "reform-era" / "direction.md").write_text(
                "已变化的方向。\n", encoding="utf-8"
            )
            output = root / "public"
            build_site(
                config_path=config,
                novels_dir=novels,
                results_dir=results,
                assets_dir=REPO_ROOT / "site" / "assets",
                output_dir=output,
            )

            home = (output / "index.html").read_text(encoding="utf-8")
            row = self._row(home, "model-a")
            self.assertIn('data-rankable="false"', row)
            self.assertNotIn("results/reform-era/model-a.html", row)

    def test_protocol_and_score_hashes_ignore_bom_and_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path, _novels, _results = self._fixture(root)
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            model_by_id = {item["id"]: item for item in config["models"]}
            protocol_before = build_protocol_expectations(config_path, model_by_id)
            scores_before = build_score_expectations(config_path, config)
            self.assertEqual(set(scores_before), set(JUDGE_IDS))
            self.assertNotIn("kimi", scores_before)

            text_paths = [
                root / "benchmark" / "reform-era" / "direction.md",
                *(root / "runner" / "prompts" / "v2" / name for name in (
                    "system.md",
                    "book.md",
                    "macro_outline.md",
                    "opening_outline.md",
                    "chapter.md",
                    "expand_chapter.md",
                    "repair_json.md",
                    "repair_chapter.md",
                    "judge_system.md",
                )),
            ]
            for path in text_paths:
                normalized = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
                path.write_bytes(
                    ("\ufeff" + normalized.replace("\n", "\r\n")).encode("utf-8")
                )

            self.assertEqual(
                build_protocol_expectations(config_path, model_by_id), protocol_before
            )
            self.assertEqual(build_score_expectations(config_path, config), scores_before)

            config["providers"]["new-api"]["request_defaults"] = {"top_p": 0.72}
            config_path.write_text(
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            protocol_after_defaults = build_protocol_expectations(
                config_path, model_by_id
            )
            scores_after_defaults = build_score_expectations(config_path, config)
            self.assertNotEqual(
                protocol_after_defaults["model-a"]["run_id"],
                protocol_before["model-a"]["run_id"],
            )
            self.assertNotEqual(
                scores_after_defaults["sol"]["judge_config_sha256"],
                scores_before["sol"]["judge_config_sha256"],
            )

    def test_artifact_hashes_treat_crlf_as_the_same_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, novels, results = self._fixture(root)
            novel = results / "model-a" / "novel.md"
            lf = novel.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            novel.write_bytes(lf.replace(b"\n", b"\r\n"))
            output = root / "public"
            build_site(
                config_path=config,
                novels_dir=novels,
                results_dir=results,
                assets_dir=REPO_ROOT / "site" / "assets",
                output_dir=output,
            )

            home = (output / "index.html").read_text(encoding="utf-8")
            row = self._row(home, "model-a")
            self.assertIn('data-rankable="true"', row)
            self.assertTrue(
                (output / "results" / "reform-era" / "model-a.html").exists()
            )

    def test_scores_for_an_older_work_hash_cannot_rank_modified_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, novels, results = self._fixture(root)
            model = results / "model-a"
            book_path = model / "book.json"
            book = json.loads(book_path.read_text(encoding="utf-8"))
            book["blurb"] = "改" * 320
            self._write_json(book_path, book)
            self._refresh_manifest_artifacts(model)

            output = root / "public"
            build_site(
                config_path=config,
                novels_dir=novels,
                results_dir=results,
                assets_dir=REPO_ROOT / "site" / "assets",
                output_dir=output,
            )
            home = (output / "index.html").read_text(encoding="utf-8")
            row = self._row(home, "model-a")
            self.assertIn('data-rankable="false"', row)
            self.assertIn("results/reform-era/model-a.html", row)

    def test_self_consistent_hashes_do_not_publish_short_v2_manuscript(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, novels, results = self._fixture(root)
            model = results / "model-a"
            book = json.loads((model / "book.json").read_text(encoding="utf-8"))
            opening = json.loads(
                (model / "opening_outline.json").read_text(encoding="utf-8")
            )
            chapters: list[str] = []
            chapter_counts: list[int] = []
            for chapter in opening["chapters"]:
                text = f"## 第{chapter['number']}章 {chapter['title']}\n\n" + "短" * 100
                chapters.append(text)
                chapter_counts.append(count_protocol_chars(text))
                (model / "chapters" / f"{chapter['number']:02d}.md").write_text(
                    text + "\n", encoding="utf-8"
                )
            (model / "novel.md").write_text(
                f"# {book['title']}\n\n" + "\n\n".join(chapters) + "\n",
                encoding="utf-8",
            )
            manifest = json.loads(
                (model / "manifest.json").read_text(encoding="utf-8")
            )
            manifest["body_chars"] = sum(chapter_counts)
            for entry, chars in zip(manifest["chapters"], chapter_counts):
                entry["chars"] = chars
            self._write_json(model / "manifest.json", manifest)
            self._refresh_manifest_artifacts(model)

            output = root / "public"
            build_site(
                config_path=config,
                novels_dir=novels,
                results_dir=results,
                assets_dir=REPO_ROOT / "site" / "assets",
                output_dir=output,
            )
            home = (output / "index.html").read_text(encoding="utf-8")
            row = self._row(home, "model-a")
            self.assertIn('data-rankable="false"', row)
            self.assertNotIn("results/reform-era/model-a.html", row)

    def test_leaderboard_script_keeps_unranked_visible_and_uses_config_ties(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")

        script_path = REPO_ROOT / "site" / "assets" / "leaderboard.js"
        with tempfile.TemporaryDirectory() as temp:
            harness = Path(temp) / "harness.js"
            harness.write_text(
                f"""
const fs = require("fs");
const vm = require("vm");
const rankCell = () => ({{ textContent: "" }});
const makeRow = (id, order, rankable, overall, themeFulfillment, aiFlavor) => {{
  const cell = rankCell();
  return {{ id, hidden: false, dataset: {{ configOrder: String(order), rankable: String(rankable), overall: String(overall), themeFulfillment: String(themeFulfillment), aiFlavor: String(aiFlavor) }}, querySelector: () => cell, cell }};
}};
const rowA = makeRow("a", 0, true, 80.2, 70.5, 5.5);
const rowB = makeRow("b", 1, true, 80.2, 90.5, 20.5);
const pending = makeRow("pending", 2, false, 99.9, 99.9, 0.1);
const initialRows = [pending, rowB, rowA];
const domOrder = [...initialRows];
const body = {{ querySelectorAll: () => initialRows, appendChild: (row) => {{ const i = domOrder.indexOf(row); if (i >= 0) domOrder.splice(i, 1); domOrder.push(row); }} }};
const makeControl = (sort, direction) => ({{ dataset: {{ sort, direction }}, attrs: {{}}, setAttribute(k, v) {{ this.attrs[k] = v; }}, addEventListener(_, cb) {{ this.cb = cb; }} }});
const buttons = [
  makeControl("overall", "desc"),
  makeControl("theme-fulfillment", "desc"),
  makeControl("ai-flavor", "asc"),
];
const slider = {{ value: "2", max: "2", disabled: false, addEventListener(_, cb) {{ this.cb = cb; }} }};
const output = {{ textContent: "" }};
global.document = {{
  querySelector: (selector) => selector === "#leaderboard-body" ? body : selector === "#rank-limit" ? slider : output,
  querySelectorAll: () => buttons,
}};
vm.runInThisContext(fs.readFileSync({json.dumps(str(script_path))}, "utf8"));
slider.value = "1";
slider.cb();
const initialState = {{
  order: domOrder.map((row) => row.id),
  ranks: domOrder.map((row) => row.cell.textContent),
  hidden: Object.fromEntries(domOrder.map((row) => [row.id, row.hidden])),
}};
buttons[1].cb();
const dimensionState = {{
  order: domOrder.map((row) => row.id),
  ranks: domOrder.map((row) => row.cell.textContent),
  hidden: Object.fromEntries(domOrder.map((row) => [row.id, row.hidden])),
}};
buttons[2].cb();
console.log(JSON.stringify({{
  initialState,
  dimensionState,
  order: domOrder.map((row) => row.id),
  ranks: domOrder.map((row) => row.cell.textContent),
  hidden: Object.fromEntries(domOrder.map((row) => [row.id, row.hidden])),
  output: output.textContent,
}}));
""",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [node, str(harness)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
        state = json.loads(completed.stdout)
        self.assertEqual(state["initialState"]["order"], ["a", "b", "pending"])
        self.assertEqual(state["initialState"]["ranks"], ["01", "02", ""])
        self.assertEqual(
            state["initialState"]["hidden"],
            {"a": False, "b": True, "pending": False},
        )
        self.assertEqual(state["dimensionState"]["order"], ["b", "a", "pending"])
        self.assertEqual(state["dimensionState"]["ranks"], ["01", "02", ""])
        self.assertEqual(
            state["dimensionState"]["hidden"],
            {"a": True, "b": False, "pending": False},
        )
        self.assertEqual(state["order"], ["a", "b", "pending"])
        self.assertEqual(state["ranks"], ["01", "02", ""])
        self.assertEqual(state["hidden"], {"a": False, "b": True, "pending": False})
        self.assertEqual(state["output"], "前 1")


if __name__ == "__main__":
    unittest.main()
