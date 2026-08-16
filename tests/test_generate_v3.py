from __future__ import annotations

import unittest

from runner.generate_v3 import (
    OpeningError,
    assert_design_prompt,
    assemble_chapter,
    design_user_prompt,
    load_v3_prompts,
    main as generate_v3_main,
    packs_compatible,
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
