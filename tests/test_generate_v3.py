from __future__ import annotations

import unittest

from runner.generate_v3 import (
    OpeningError,
    SKIP_FROM_ALL,
    assert_design_prompt,
    assemble_chapter,
    beat_previous_tail,
    beat_user_prompt,
    design_user_prompt,
    load_frozen_pack,
    load_v3_prompts,
    main as generate_v3_main,
    packs_compatible,
    parse_v3_stop_after,
    run_prose,
    try_load_stage,
    validate_beat,
    validate_characters,
    validate_outline,
    validate_world,
)
from runner.generate import repo_root, canonical_text
from runner.score_design import DEFAULT_JUDGES, parse_design_score, pick_winners


def _world() -> dict:
    return {
        "name": "西门缝",
        "premise": "白昼裂缝不服从宫廷法术。",
        "rules": ["魔法必须见血或见光作为代价", "裂缝不能被当场封闭", "徽记灼伤施术者"],
        "institutions": [
            {"name": "裁决庭", "wants": "先控制徽记", "can": "封街", "cannot": "灭缝"},
            {"name": "骑士团", "wants": "带走少年", "can": "举盾隔离", "cannot": "擅自宣判异端"},
            {"name": "灰手", "wants": "买下目击者", "can": "散谣", "cannot": "公开对峙骑士"},
        ],
        "opening_constraints": ["缝只掉人和铁", "少年不会本地语"],
        "taboos": ["技能面板"],
        "unresolved": ["缝从哪来"],
    }


def _characters() -> dict:
    return {
        "viewpoint": "莱恩",
        "cast": [
            {
                "name": "莱恩",
                "role_in_incident": "第一个碰到少年的门卫",
                "desire": "别让市民涌上来",
                "cannot_accept": "再死一个孩子在自己班上",
                "knows": "不知道徽记是什么，以为是邪物",
                "can_decide": "先拦谁、放谁靠近",
                "how_refuses": "把戟横在路上",
                "attention": "先看血和手",
                "entry_state": "夜班没睡够，更容易吼人",
            },
            {
                "name": "伊蕾",
                "role_in_incident": "教会见习书记",
                "desire": "先记录再裁决",
                "cannot_accept": "让骑士团把人直接拖走",
                "knows": "误以为少年是伪神使",
                "can_decide": "能不能触摸徽记",
                "how_refuses": "要求当众诵条令拖延",
                "attention": "先听语言不像王都口音",
                "entry_state": "刚被上司斥过，不敢再错判",
            },
            {
                "name": "少年",
                "role_in_incident": "从缝里掉下来的人",
                "desire": "搞清自己在哪",
                "cannot_accept": "被当成祭品",
                "knows": "听不懂周围的话",
                "can_decide": "跟谁走",
                "how_refuses": "抓紧铁块后退",
                "attention": "先看谁伸手",
                "entry_state": "落地摔懵，听力过载",
            },
        ],
    }


def _outline() -> dict:
    return {
        "incident_one_liner": "西门白昼裂开，掉出不会说话的少年和烫手徽记。",
        "first_irreversible": "莱恩把少年交给教会而不是骑士团。",
        "not_resolved": ["缝的来历"],
        "chapters": [
            {
                "number": n,
                "title": f"第{n}场",
                "function": "莱恩多了一笔说不清的责任",
                "spine": "裂缝出现 → 三方赶到 → 有人伸手 → 少年被带走 → 市民看见",
                "pressures": ["莱恩｜守门｜不知徽记｜戟"],
                "must_keep": ["裂缝在白昼"],
                "must_not_lock": ["神代真相"],
                "prose_free": "台词、动作、闲笔",
                "beats": ["缝裂开", "少年落地", "有人伸手"],
            }
            for n in range(1, 6)
        ],
    }


class GenerateV3Tests(unittest.TestCase):
    def test_validators_accept_minimal_legal_payloads(self) -> None:
        self.assertEqual(validate_world(_world())["name"], "西门缝")
        self.assertEqual(validate_characters(_characters())["viewpoint"], "莱恩")
        self.assertEqual(len(validate_outline(_outline())["chapters"]), 5)

    def test_world_allows_a_single_institution(self) -> None:
        data = _world()
        data["rules"] = ["一条规则"]
        data["institutions"] = [data["institutions"][0]]
        data["opening_constraints"] = ["一条约束"]
        self.assertEqual(len(validate_world(data)["institutions"]), 1)

    def test_viewpoint_must_be_in_cast(self) -> None:
        data = _characters()
        data["viewpoint"] = "幽灵"
        with self.assertRaises(OpeningError):
            validate_characters(data)

    def test_world_prompt_is_topic_only(self) -> None:
        root = repo_root()
        prompts = load_v3_prompts(root / "runner" / "prompts" / "v3")
        direction = canonical_text(
            (root / "benchmark" / "foundation-city" / "direction.md")
            .read_bytes()
            .decode("utf-8-sig")
        )
        user = design_user_prompt(prompts, "world", direction)
        assert_design_prompt(user, direction, "world")
        self.assertNotIn("book.json", user)
        self.assertNotIn("已定世界", user)
        self.assertNotIn("技能面板", user)

    def test_later_prompts_require_locked_priors(self) -> None:
        root = repo_root()
        prompts = load_v3_prompts(root / "runner" / "prompts" / "v3")
        direction = canonical_text(
            (root / "benchmark" / "foundation-city" / "direction.md")
            .read_bytes()
            .decode("utf-8-sig")
        )
        world = _world()
        characters = _characters()
        char_prompt = design_user_prompt(prompts, "characters", direction, world=world)
        assert_design_prompt(char_prompt, direction, "characters", world=world)
        self.assertIn(world["name"], char_prompt)
        outline_prompt = design_user_prompt(
            prompts, "outline", direction, world=world, characters=characters
        )
        assert_design_prompt(
            outline_prompt, direction, "outline", world=world, characters=characters
        )
        self.assertIn(characters["viewpoint"], outline_prompt)
        self.assertIn("5–10 章", outline_prompt)
        with self.assertRaises(OpeningError):
            assert_design_prompt(char_prompt, direction, "characters")

    def test_incompatible_pack_when_outline_drops_viewpoint(self) -> None:
        outline = _outline()
        outline["first_irreversible"] = "有人先伸了手。"
        outline["incident_one_liner"] = "白天有缝裂开。"
        for chapter in outline["chapters"]:
            chapter["pressures"] = ["路人｜看热闹｜无｜无"]
            chapter["spine"] = "有人围观"
            chapter["function"] = "围观结束"
        problems = packs_compatible(_world(), _characters(), outline)
        self.assertTrue(any("视角" in item for item in problems))

    def test_incompatible_pack_when_outline_has_no_city(self) -> None:
        problems = packs_compatible(_world(), _characters(), _outline())
        self.assertTrue(any("高楼" in item for item in problems))

    def test_compatible_pack_names_city_and_viewpoint(self) -> None:
        outline = _outline()
        outline["incident_one_liner"] = "莱恩翻过十万大山，看见高楼大厦。"
        self.assertEqual(packs_compatible(_world(), _characters(), outline), [])

    def test_beat_and_chapter_length_gates(self) -> None:
        short = "太短了。"
        with self.assertRaises(OpeningError):
            validate_beat(short)
        beat = "门卫把戟横过来。" + "城门的影子还在往外爬。" * 55
        cleaned = validate_beat(beat)
        chapter = assemble_chapter([cleaned, cleaned, cleaned, cleaned])
        self.assertGreater(len(chapter), 100)
        overshoot = "门卫把戟横过来。" + "城门的影子还在往外爬。" * 200
        self.assertGreater(len(validate_beat(overshoot)), 100)
        dump = "门卫把戟横过来。" + "城门的影子还在往外爬。" * 260
        with self.assertRaises(OpeningError):
            validate_beat(dump)

    def test_try_load_stage_rejects_broken_json(self) -> None:
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "world.json").write_text("{", encoding="utf-8")
            self.assertIsNone(try_load_stage(root, "world"))
            (root / "world.json").write_text(
                __import__("json").dumps(_world(), ensure_ascii=False), encoding="utf-8"
            )
            loaded = try_load_stage(root, "world")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded["name"], "西门缝")

    def test_parse_design_score_and_pick_winners(self) -> None:
        parsed = parse_design_score(
            '{"bands":{"constraint":3,"institutions":2,"focus":4},"comment":"规则能挡住灭缝。"}',
            "world",
        )
        self.assertEqual(parsed["score"], 75.0)
        winners = pick_winners(
            [
                {
                    "candidate": "aa",
                    "complete": True,
                    "overall": 70.0,
                    "tracks": {
                        "world": {"median": 80.0},
                        "characters": {"median": 60.0},
                        "outline": {"median": 70.0},
                    },
                },
                {
                    "candidate": "bb",
                    "complete": True,
                    "overall": 72.0,
                    "tracks": {
                        "world": {"median": 70.0},
                        "characters": {"median": 80.0},
                        "outline": {"median": 66.0},
                    },
                },
            ]
        )
        self.assertEqual(winners["mixed"]["world"], "aa")
        self.assertEqual(winners["mixed"]["characters"], "bb")
        self.assertEqual(winners["package"], "bb")

    def test_design_judges_are_the_five_active_seats(self) -> None:
        self.assertEqual(DEFAULT_JUDGES, ("sol", "grok", "opus", "k3", "ds-v4-pro"))

    def test_cli_dry_run_all_reports_jobs(self) -> None:
        self.assertEqual(
            generate_v3_main(["--all", "--dry-run", "--jobs", "6"]),
            0,
        )

    def test_cli_exclude_skips_claude_prefix(self) -> None:
        self.assertEqual(
            generate_v3_main(["--all", "--dry-run", "--exclude", "claude-"]),
            0,
        )

    def test_cli_refuses_characters_without_locked_world(self) -> None:
        self.assertEqual(
            generate_v3_main(["--all", "--dry-run", "--stop-after", "characters"]),
            2,
        )

    def test_cli_refuses_outline_without_locked_characters(self) -> None:
        self.assertEqual(
            generate_v3_main(
                [
                    "--all",
                    "--dry-run",
                    "--stop-after",
                    "outline",
                    "--from-world",
                    "glm-5.3",
                ]
            ),
            2,
        )

    def test_cli_dry_run_prose_sees_frozen_pack(self) -> None:
        self.assertEqual(
            generate_v3_main(
                ["--all", "--dry-run", "--phase", "prose", "--exclude", "claude-"]
            ),
            0,
        )

    def test_all_skips_luna_and_agnes(self) -> None:
        import io
        from contextlib import redirect_stdout

        self.assertIn("gpt-5.6-luna", SKIP_FROM_ALL)
        self.assertIn("agnes-2.5-flash", SKIP_FROM_ALL)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = generate_v3_main(
                ["--all", "--dry-run", "--phase", "prose", "--exclude", "claude-"]
            )
        self.assertEqual(code, 0)
        self.assertIn("models=14", buffer.getvalue())

    def test_cli_prose_rejects_design_stop_after(self) -> None:
        self.assertEqual(
            generate_v3_main(["--all", "--dry-run", "--phase", "prose", "--stop-after", "world"]),
            2,
        )

    def test_cli_design_rejects_chapter_stop_after(self) -> None:
        self.assertEqual(
            generate_v3_main(["--all", "--dry-run", "--stop-after", "chapter:1"]),
            2,
        )

    def test_parse_stop_after_defaults(self) -> None:
        self.assertEqual(parse_v3_stop_after(None, "design"), ("world", None))
        self.assertEqual(parse_v3_stop_after(None, "prose"), (None, None))
        self.assertEqual(parse_v3_stop_after("chapter:2", "prose"), (None, 2))

    def test_beat_prompt_survives_json_braces(self) -> None:
        root = repo_root()
        prompts = load_v3_prompts(root / "runner" / "prompts" / "v3")
        pack = {
            "world": _world(),
            "characters": _characters(),
            "outline": _outline(),
        }
        pack["outline"]["incident_one_liner"] = "莱恩翻过十万大山，看见高楼大厦。"
        user = beat_user_prompt(prompts, pack, pack["outline"]["chapters"][0], 1, "")
        self.assertIn("（本章第一节）", user)
        self.assertIn("西门缝", user)
        self.assertIn("缝裂开", user)
        self.assertIn("旁白", user)
        self.assertNotIn("{beat_materials}", user)
        self.assertNotIn("{previous_tail}", user)
        self.assertNotIn("第2场", user)
        self.assertNotIn("少年落地", user)
        self.assertNotIn("有人伸手", user)

    def test_real_frozen_beat_prompt_hides_later_outline(self) -> None:
        from pathlib import Path

        root = repo_root()
        prompts = load_v3_prompts(root / "runner" / "prompts" / "v3")
        pack = load_frozen_pack(root / "benchmark" / "foundation-city" / "frozen" / "pack.json")
        user = beat_user_prompt(prompts, pack, pack["outline"]["chapters"][0], 1, "")
        self.assertIn("哑叔", user)
        self.assertNotIn("远房表弟", user)
        self.assertNotIn("玉牌发烫", user)
        self.assertNotIn("半夜吐纳", user)
        self.assertNotIn("一束巡逻警灯扫上边坡", user)

    def test_short_beat_is_expanded_once(self) -> None:
        from types import SimpleNamespace

        root = repo_root()
        prompts = load_v3_prompts(root / "runner" / "prompts" / "v3")
        chapter = _outline()["chapters"][0]
        calls: list[str] = []

        class Client:
            def complete(self, model_cfg, messages, *, stage):
                calls.append(stage)
                user = messages[1]["content"]
                if "扩写" in user or stage.endswith("-expand"):
                    body = "扩写后莱恩把戟横过来。" + "城门的影子还在往外爬。" * 55
                    return SimpleNamespace(content=body)
                return SimpleNamespace(content="太短了，只有一句。")

        from runner.generate_v3 import _complete_beat

        text = _complete_beat(
            Client(),
            {"id": "glm-5.3", "model": "glm-5.3"},
            prompts["system.md"],
            beat_user_prompt(prompts, {"world": _world(), "characters": _characters(), "outline": _outline(), "style": "旁白。"}, chapter, 1, ""),
            stage="v3-beat-1-1",
            prompts=prompts,
            chapter=chapter,
            beat_index=1,
        )
        self.assertEqual(calls, ["v3-beat-1-1", "v3-beat-1-1-expand"])
        self.assertIn("扩写后", text)

    def test_previous_tail_keeps_the_ending(self) -> None:
        text = ("前段。" * 80) + "这是尾部标记。"
        tail = beat_previous_tail(text, limit=8)
        self.assertEqual(tail, text[-8:])

    def test_run_prose_writes_resumes_and_stops(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        root = repo_root()
        prompts = load_v3_prompts(root / "runner" / "prompts" / "v3")
        outline = _outline()
        outline["incident_one_liner"] = "莱恩翻过十万大山，看见高楼大厦。"
        pack = {
            "schema": "novel-benchmark.v3",
            "benchmark": "foundation-city",
            "world": _world(),
            "characters": _characters(),
            "outline": outline,
            "style": "叙述经过视角人物的经验。",
        }

        class BeatClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, list[dict[str, str]]]] = []

            def complete(self, model_cfg, messages, *, stage):
                self.calls.append((stage, list(messages)))
                user = messages[1]["content"]
                marker = "本章第一节" if "（本章第一节）" in user else "接上节"
                body = f"{marker}莱恩把戟横过来。" + "城门的影子还在往外爬。" * 55
                return SimpleNamespace(content=body)

        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            pack_path = work / "pack.json"
            pack_path.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
            loaded = load_frozen_pack(pack_path)
            out = work / "glm-5.3"
            client = BeatClient()
            name, status = run_prose(
                client=client,
                model_cfg={"id": "glm-5.3", "model": "glm-5.3"},
                prompts=prompts,
                pack=loaded,
                pack_path=pack_path,
                output_dir=out,
                model_id="glm-5.3",
                stop_after_chapter=1,
            )
            self.assertEqual(name, "glm-5.3")
            self.assertEqual(status, "partial:1")
            self.assertEqual(len(client.calls), 3)
            self.assertTrue((out / "beats" / "01-01.md").is_file())
            self.assertTrue((out / "chapters" / "01.md").is_file())
            self.assertFalse((out / "novel.md").is_file())
            self.assertIn("（本章第一节）", client.calls[0][1][1]["content"])
            self.assertIn("城门的影子还在往外爬。", client.calls[1][1][1]["content"])
            self.assertNotIn("（本章第一节）", client.calls[1][1][1]["content"])

            resumed = BeatClient()
            name, status = run_prose(
                client=resumed,
                model_cfg={"id": "glm-5.3", "model": "glm-5.3"},
                prompts=prompts,
                pack=loaded,
                pack_path=pack_path,
                output_dir=out,
                model_id="glm-5.3",
            )
            self.assertEqual(status, "complete")
            self.assertEqual(len(resumed.calls), 12)
            self.assertTrue((out / "novel.md").is_file())
            novel = (out / "novel.md").read_text(encoding="utf-8")
            self.assertIn("## 第1章 第1场", novel)
            self.assertIn("## 第5章 第5场", novel)
            manifest = json.loads((out / "prose.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(len(manifest["chapters"]), 5)

            cached = BeatClient()
            _, status = run_prose(
                client=cached,
                model_cfg={"id": "glm-5.3", "model": "glm-5.3"},
                prompts=prompts,
                pack=loaded,
                pack_path=pack_path,
                output_dir=out,
                model_id="glm-5.3",
            )
            self.assertEqual(status, "complete")
            self.assertEqual(cached.calls, [])

    def test_run_prose_refuses_stale_frozen_pack(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        root = repo_root()
        prompts = load_v3_prompts(root / "runner" / "prompts" / "v3")
        outline = _outline()
        outline["incident_one_liner"] = "莱恩翻过十万大山，看见高楼大厦。"
        pack = {
            "schema": "novel-benchmark.v3",
            "benchmark": "foundation-city",
            "world": _world(),
            "characters": _characters(),
            "outline": outline,
            "style": "叙述经过视角人物的经验。",
        }

        class BeatClient:
            def complete(self, model_cfg, messages, *, stage):
                body = "莱恩把戟横过来。" + "城门的影子还在往外爬。" * 55
                return SimpleNamespace(content=body)

        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            pack_path = work / "pack.json"
            pack_path.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
            out = work / "glm-5.3"
            run_prose(
                client=BeatClient(),
                model_cfg={"id": "glm-5.3", "model": "glm-5.3"},
                prompts=prompts,
                pack=load_frozen_pack(pack_path),
                pack_path=pack_path,
                output_dir=out,
                model_id="glm-5.3",
                stop_after_chapter=1,
            )
            pack["world"]["name"] = "东门缝"
            pack_path.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
            with self.assertRaises(OpeningError):
                run_prose(
                    client=BeatClient(),
                    model_cfg={"id": "glm-5.3", "model": "glm-5.3"},
                    prompts=prompts,
                    pack=load_frozen_pack(pack_path),
                    pack_path=pack_path,
                    output_dir=out,
                    model_id="glm-5.3",
                    stop_after_chapter=1,
                )
