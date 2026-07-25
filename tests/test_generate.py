from __future__ import annotations

import json
import os
from pathlib import Path
import re

import pytest
import yaml

import runner.generate as generate_module
from runner.generate import (
    EXPECTED_GENERATOR_IDS,
    EXPECTED_JUDGES,
    GenerationRun,
    calculate_run_id,
    count_content_chars,
    estimate_prompt_tokens,
    estimate_tokens,
    generation_request_parameters,
    parse_json_object,
    parse_stop_after,
    result_is_complete,
    retry_delay_seconds,
    validate_book,
    validate_chapter,
    validate_macro_outline,
    validate_opening_outline,
    validate_fixed_registries,
)
from runner.llm_api import ChatResult, LLMAPIError, with_provider_request_defaults


def make_book() -> dict:
    return {
        "title": "潮汐线",
        "blurb": "改" * 320,
        "protagonist": "林川，县城工人，想改变家境但容易替人担责",
        "setting": "改革开放初期的中国南方",
        "core_theme": "人在制度与机会之间的选择",
        "ending_direction": "主人公接受成功的代价",
    }


def make_macro() -> dict:
    volumes = []
    for number in range(1, 11):
        volumes.append(
            {
                "number": number,
                "title": f"第{number}卷",
                "target_chars": 200_000,
                "period": f"阶段{number}",
                "start_state": "起点",
                "end_state": "变化",
                "main_conflict": "利益与关系冲突",
                "arcs": [
                    {"title": f"弧{i}", "summary": "人物采取行动并承担后果"}
                    for i in range(1, 4)
                ],
            }
        )
    return {
        "target_total_chars": 2_000_000,
        "volumes": volumes,
        "character_arcs": ["主人公长期变化"],
        "foreshadowing": ["第一卷埋设，末卷回收"],
        "ending": "完成主要人物弧线",
    }


def make_opening(*, chapter_count: int = 16, target_chars: int = 3_130) -> dict:
    chapters = []
    for number in range(1, chapter_count + 1):
        chapters.append(
            {
                "number": number,
                "title": f"潮声{number}",
                "target_chars": target_chars,
                "summary": "主人公面对一个具体选择",
                "beats": ["进入场景", "冲突发生", "做出选择"],
                "continuity_in": [f"进入本章时账面状态为{number}"],
                "continuity_out": [f"本章结束后账面状态变为{number + 1}"],
                "foreshadowing": ["旧收据"],
            }
        )
    return {
        "target_total_chars": chapter_count * target_chars,
        "macro_scope": "第一卷开端",
        "chapters": chapters,
    }


class FakeClient:
    def __init__(
        self,
        *,
        invalid_once: str | None = None,
        api_error_once: str | None = None,
        finish_reason_once: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, list[dict[str, str]]]] = []
        self.invalid_once = invalid_once
        self.api_error_once = api_error_once
        self.finish_reason_once = finish_reason_once
        self._invalid_sent = False
        self._api_error_sent = False
        self._finish_reason_sent = False

    def complete(self, model_cfg, messages, *, stage):
        self.calls.append((stage, messages))
        if self.api_error_once == stage and not self._api_error_sent:
            self._api_error_sent = True
            raise LLMAPIError(
                "LLM API 返回空内容（finish_reason=length，reasoning=present）",
                raw_response={
                    "id": "failed-response",
                    "model": model_cfg["model"],
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "content": "",
                                "reasoning_content": "PRIVATE_REASONING",
                            },
                        }
                    ],
                    "usage": {"completion_tokens": 8192, "total_tokens": 8200},
                },
            )
        if self.invalid_once == stage and not self._invalid_sent:
            self._invalid_sent = True
            content = "{}" if stage != "chapter" else "不是正文"
            return ChatResult(
                content=content,
                usage={"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
                requested_model=model_cfg["model"],
                response_model=model_cfg["model"],
                finish_reason="stop",
                response_id=f"r-{len(self.calls)}",
                latency_ms=1,
                raw_response={"id": f"r-{len(self.calls)}"},
            )
        if stage == "book":
            content = json.dumps(make_book(), ensure_ascii=False)
        elif stage == "macro_outline":
            content = json.dumps(make_macro(), ensure_ascii=False)
        elif stage == "opening_outline":
            content = json.dumps(make_opening(), ensure_ascii=False)
        else:
            matches = re.findall(r"第\s*(\d+)\s*章", messages[-1]["content"])
            number = int(matches[-1])
            target = make_opening()["chapters"][number - 1]["target_chars"]
            content = f"## 第{number}章 潮声{number}\n\n" + "汉" * target
        finish_reason = "stop"
        if self.finish_reason_once == stage and not self._finish_reason_sent:
            self._finish_reason_sent = True
            finish_reason = "length"
        return ChatResult(
            content=content,
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            requested_model=model_cfg["model"],
            response_model=model_cfg["model"],
            finish_reason=finish_reason,
            response_id=f"r-{len(self.calls)}",
            latency_ms=1,
            raw_response={"id": f"r-{len(self.calls)}"},
        )


PROMPTS = {
    "system.md": "你是长篇小说作家。",
    "book.md": "根据{direction}立项并输出JSON。",
    "macro_outline.md": "根据{direction}输出全纲JSON。",
    "opening_outline.md": "根据{direction}输出细纲JSON。",
    "chapter.md": "写第{chapter_number}章《{chapter_title}》：{chapter_summary}；{chapter_beats}；{target_chars}字。",
    "expand_chapter.md": "扩写第{chapter_number}章《{chapter_title}》，当前{current_chars}字。",
    "repair_json.md": "修复{stage}：{error}",
    "repair_chapter.md": "修复第{chapter_number}章：{error}；目标{target_chars}字。",
}


MODEL = {
    "id": "fake-model",
    "name": "Fake Model",
    "model": "fake-model",
    "provider": "new-api",
    "context_window": 1_000_000,
    "request": {},
    "stages": {},
}


def make_run(
    tmp_path: Path,
    client: FakeClient,
    *,
    prompts: dict | None = None,
    sleep_fn=None,
) -> GenerationRun:
    return GenerationRun(
        root=tmp_path,
        benchmark="reform-era",
        direction="改革开放初期的中国现实主义长篇。",
        prompts=prompts or PROMPTS,
        model_cfg=MODEL,
        client=client,
        new_run=False,
        sleep_fn=sleep_fn or (lambda _delay: None),
    )


def make_fixed_config() -> dict:
    model_defaults = {
        "provider": "new-api",
        "context_window": 131_072,
        "request": {},
        "stages": {},
    }
    return {
        "providers": {"new-api": {"base_url_env": "API_URL", "api_key_env": "API_KEY"}},
        "models": [
            {**model_defaults, "id": model_id, "model": model_id, "name": model_id}
            for model_id in EXPECTED_GENERATOR_IDS
        ],
        "judges": [
            {"id": judge_id, "model": model_id, "provider": "new-api"}
            for judge_id, model_id in EXPECTED_JUDGES.items()
        ],
    }


def write_cli_workspace(root: Path, config: dict) -> None:
    direction_dir = root / "benchmark" / "reform-era"
    direction_dir.mkdir(parents=True)
    (direction_dir / "direction.md").write_text(
        "改革开放初期的中国现实主义长篇。\n", encoding="utf-8"
    )
    prompt_dir = root / "runner" / "prompts" / "v2"
    prompt_dir.mkdir(parents=True)
    for name, content in PROMPTS.items():
        (prompt_dir / name).write_text(content + "\n", encoding="utf-8")
    (root / "config.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def test_parse_and_validators() -> None:
    assert parse_json_object("```json\n{\"a\": 1}\n```") == {"a": 1}
    assert parse_json_object('{"a":"末字段漏闭引号}') == {"a": "末字段漏闭引号"}
    assert validate_book(make_book())["title"] == "潮汐线"
    assert len(validate_macro_outline(make_macro())["volumes"]) == 10
    assert len(validate_opening_outline(make_opening())["chapters"]) == 16
    chapter = make_opening()["chapters"][0]
    text = "## 第1章 潮声1\n\n" + "汉" * 3_130
    assert count_content_chars(validate_chapter(text, chapter)) == 3_130
    with pytest.raises(ValueError, match="48000"):
        validate_opening_outline(make_opening(target_chars=2_900))
    assert validate_opening_outline(make_opening(target_chars=3_000))[
        "target_total_chars"
    ] == 48_000
    assert len(validate_opening_outline(make_opening(chapter_count=18, target_chars=2_800))["chapters"]) == 18
    assert (
        validate_opening_outline(make_opening(chapter_count=18, target_chars=4_000))[
            "target_total_chars"
        ]
        == 72_000
    )
    declared_below_minimum = make_opening()
    declared_below_minimum["target_total_chars"] = 47_999
    with pytest.raises(ValueError, match="target_total_chars.*48000"):
        validate_opening_outline(declared_below_minimum)
    with pytest.raises(ValueError, match="16–18"):
        validate_opening_outline(make_opening(chapter_count=19, target_chars=2_800))
    assert count_content_chars(
        validate_chapter("## 第1章 潮声1\n\n" + "汉" * 100, chapter)
    ) == 100
    assert count_content_chars(
        validate_chapter("## 第1章 潮声1\n\n" + "汉" * 5_000, chapter)
    ) == 5_000
    with pytest.raises(ValueError, match="正文为空"):
        validate_chapter("## 第1章 潮声1", chapter)
    with pytest.raises(ValueError, match="reasoning 标记"):
        validate_chapter(
            "## 第1章 潮声1\n\n<think>私有推理</think>" + "汉" * 3_130,
            chapter,
        )
    compact_book = make_book()
    compact_book["title"] = "潮"
    compact_book["blurb"] = "改" * 100
    assert validate_book(compact_book)["blurb"] == "改" * 100
    long_book = make_book()
    long_book["title"] = "潮" * 100
    long_book["blurb"] = "改" * 2_000
    assert validate_book(long_book)["title"] == "潮" * 100
    assert parse_stop_after("chapter:7") == ("chapter", 7)
    with pytest.raises(ValueError):
        parse_stop_after("chapter:19")


def test_generation_request_parameters_must_stay_at_server_defaults() -> None:
    assert generation_request_parameters(MODEL, "chapter") == {}
    assert generation_request_parameters(MODEL, "chapter_expansion") == {}
    configured = {
        **MODEL,
        "request": {"temperature": 0.7},
        "stages": {"chapter": {"max_tokens": 4096}},
        generate_module.PROVIDER_DEFAULTS_TRACKING_KEY: {"top_p": 0.9},
    }
    assert generation_request_parameters(configured, "chapter") == {
        "top_p": 0.9,
        "temperature": 0.7,
        "max_tokens": 4096,
    }
    expansion_configured = {
        **MODEL,
        "stages": {"chapter_expansion": {"max_tokens": 8192}},
    }
    assert generation_request_parameters(
        expansion_configured, "chapter_expansion"
    ) == {"max_tokens": 8192}


def test_end_to_end_and_resume(tmp_path: Path) -> None:
    client = FakeClient()
    run = make_run(tmp_path, client)
    assert run.execute()
    assert len(client.calls) == 19
    result_dir = tmp_path / "results" / "reform-era" / "fake-model"
    manifest = json.loads((result_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "novel-benchmark.v2.1"
    assert manifest["run_input_sha256"].startswith(run.run_id)
    assert manifest["protocol_policy"] == generate_module.PROTOCOL_POLICY
    assert manifest["protocol_policy_sha256"] == generate_module.protocol_policy_sha256()
    assert manifest["body_chars"] == 50_080
    assert manifest["retry_count"] == 0
    assert len(manifest["code_sha256"]) == 64
    assert len(manifest["artifact_sha256"]) == 20
    assert len(list((result_dir / "chapters").glob("*.md"))) == 16
    assert len(list(run.usage_events_dir.glob("*.json"))) == 19
    usage_records = run._usage_records()
    assert all(
        record["context_audit"]["configured_max_tokens"] is None
        and record["context_audit"]["max_tokens_sent"] is False
        and record["context_audit"]["output_reserve_tokens"] == 0
        and record["context_audit"]["api_optional_parameters"] == []
        for record in usage_records
    )
    assert manifest["context_audit"]["calls"] == 19
    assert manifest["context_audit"]["estimate_sources"] == {
        "fallback": 1,
        "provider_usage_anchor": 18,
    }
    assert len(
        [
            json.loads(line)
            for line in run.usage_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    ) == 19
    assert result_is_complete(result_dir, run.run_id)
    session = json.loads(run.session_path.read_text(encoding="utf-8"))["messages"]
    assert session[0]["role"] == "system"
    assert [item["role"] for item in session[1:5]] == ["user", "assistant", "user", "assistant"]

    resumed_client = FakeClient()
    resumed = make_run(tmp_path, resumed_client)
    assert resumed.execute()
    assert resumed_client.calls == []

    novel_path = result_dir / "novel.md"
    os.utime(novel_path, None)
    assert result_is_complete(result_dir, run.run_id)

    # Git and editors may materialize committed text with CRLF on Windows.
    # Content hashes and completion checks must treat that as the same work.
    for name in manifest["artifact_sha256"]:
        artifact = result_dir / name
        lf = artifact.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        artifact.write_bytes(lf.replace(b"\n", b"\r\n"))
    assert result_is_complete(result_dir, run.run_id)

    chapter_path = result_dir / "chapters" / "01.md"
    chapter_path.write_text(
        chapter_path.read_text(encoding="utf-8").replace("汉", "文", 1),
        encoding="utf-8",
    )
    assert not result_is_complete(result_dir, run.run_id)


def test_uneven_chapters_publish_when_only_final_total_is_valid(
    tmp_path: Path,
) -> None:
    counts = [100, 5_000, *([3_213] * 13), 3_211]
    assert sum(counts) == 50_080

    class UnevenClient(FakeClient):
        def complete(self, model_cfg, messages, *, stage):
            if stage != "chapter":
                return super().complete(model_cfg, messages, stage=stage)
            self.calls.append((stage, messages))
            matches = re.findall(r"第\s*(\d+)\s*章", messages[-1]["content"])
            number = int(matches[-1])
            content = f"## 第{number}章 潮声{number}\n\n" + "汉" * counts[number - 1]
            return ChatResult(
                content=content,
                usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                requested_model=model_cfg["model"],
                response_model=model_cfg["model"],
                finish_reason="stop",
                response_id=f"r-{len(self.calls)}",
                latency_ms=1,
                raw_response={"id": f"r-{len(self.calls)}"},
            )

    client = UnevenClient()
    run = make_run(tmp_path, client)
    assert run.execute()
    assert result_is_complete(run.result_dir, run.run_id)
    manifest = json.loads((run.result_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["body_chars"] == 53_110
    assert manifest["chapters"][0]["chars"] == 3_130
    assert manifest["chapters"][0]["initial_chars"] == 100
    assert manifest["chapters"][0]["expansion_requested"] is True
    assert manifest["chapters"][0]["expansion_attempt_count"] == 1
    assert manifest["chapters"][0]["expansion_result_chars"] == 3_130
    assert manifest["chapters"][0]["expansion_adopted"] is True
    assert manifest["chapters"][0]["expansion_outcome"] == "adopted"
    assert manifest["chapters"][1]["chars"] == 5_000
    assert manifest["chapters"][1]["expansion_requested"] is False
    assert manifest["attempts"]["chapter_01"] == 1
    assert manifest["attempts"]["chapter_expansion_01"] == 1
    assert manifest["retry_count"] == 0
    assert manifest["usage"]["calls"] == 20
    assert len(client.calls) == 20


@pytest.mark.parametrize(
    ("total_chars", "should_publish"),
    [(47_999, False), (48_000, True), (55_001, True), (100_000, True)],
)
def test_final_total_has_a_minimum_but_no_upper_gate(
    tmp_path: Path, total_chars: int, should_publish: bool
) -> None:
    base, remainder = divmod(total_chars, 16)
    counts = [base + (1 if index < remainder else 0) for index in range(16)]

    class TotalClient(FakeClient):
        def complete(self, model_cfg, messages, *, stage):
            if stage not in ("chapter", "chapter_expansion"):
                return super().complete(model_cfg, messages, stage=stage)
            self.calls.append((stage, messages))
            matches = re.findall(r"第\s*(\d+)\s*章", messages[-1]["content"])
            number = int(matches[-1])
            content = f"## 第{number}章 潮声{number}\n\n" + "汉" * counts[number - 1]
            return ChatResult(
                content=content,
                usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                requested_model=model_cfg["model"],
                response_model=model_cfg["model"],
                finish_reason="stop",
                response_id=f"r-{len(self.calls)}",
                latency_ms=1,
                raw_response={"id": f"r-{len(self.calls)}"},
            )

    run = make_run(tmp_path, TotalClient())
    if should_publish:
        assert run.execute()
        assert result_is_complete(run.result_dir, run.run_id)
        manifest = json.loads(
            (run.result_dir / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["body_chars"] == total_chars
    else:
        with pytest.raises(RuntimeError, match="最低完成线"):
            run.execute()
        assert not (run.result_dir / "manifest.json").exists()


def test_v21_chapter_prompt_is_minimal_and_uses_structural_repair(
    tmp_path: Path,
) -> None:
    prompts = generate_module.load_prompts(
        Path(__file__).resolve().parents[1] / "runner" / "prompts" / "v2.1"
    )
    client = FakeClient(invalid_once="chapter")
    run = make_run(tmp_path, client, prompts=prompts)
    assert run.execute("chapter:1") is False
    chapter_prompts = [
        messages[-1]["content"]
        for stage, messages in client.calls
        if stage == "chapter"
    ]
    assert len(prompts["system.md"]) < 120
    assert len(prompts["book.md"]) < 350
    assert len(prompts["chapter.md"]) < 300
    assert len(prompts["expand_chapter.md"]) < 180
    assert len(prompts["opening_outline.md"]) < 700
    assert "{direction}" not in prompts["opening_outline.md"]
    assert "300–500" not in prompts["book.md"]
    assert "API" not in prompts["opening_outline.md"]
    assert "3–5" in prompts["opening_outline.md"]
    assert "3000–4000" in prompts["expand_chapter.md"]
    assert "{current_chars}" in prompts["expand_chapter.md"]
    assert "3000–4000" in chapter_prompts[0]
    assert "完整展开关键场景" in chapter_prompts[0]
    assert "主人公面对一个具体选择" not in chapter_prompts[0]
    assert "只作创作参考" not in chapter_prompts[0]
    assert "单章实际字数不会作为拒稿或重写条件" not in chapter_prompts[0]
    assert "不重新介绍读者已经知道" not in chapter_prompts[0]
    assert "无效对话" not in chapter_prompts[0]
    assert "必须完成的场景节拍" not in chapter_prompts[0]
    assert "硬性长度验收" not in chapter_prompts[0]
    assert "承接状态" in chapter_prompts[0]
    assert "伏笔" in chapter_prompts[0]
    assert "只修复该错误" in chapter_prompts[1]
    assert "不重写或调整篇幅" in chapter_prompts[1]
    assert "硬性合法范围" not in chapter_prompts[1]
    chapter_calls = [messages for stage, messages in client.calls if stage == "chapter"]
    assert sum(
        message["role"] == "assistant" and message["content"] == "不是正文"
        for message in chapter_calls[1]
    ) == 1
    session = json.loads(run.session_path.read_text(encoding="utf-8"))["messages"]
    assert len(session) == 1 + 2 * 4
    assert all(message["content"] != "不是正文" for message in session)


@pytest.mark.parametrize(
    ("initial_chars", "expanded_chars", "expected_chars", "expansion_calls"),
    [
        (2_999, 2_600, 2_999, 1),
        (2_000, 2_600, 2_600, 1),
        (2_000, 9_999, 9_999, 1),
        (3_000, 9_999, 3_000, 0),
        (5_000, 9_999, 5_000, 0),
    ],
)
def test_short_length_triggers_only_one_best_effort_expansion(
    tmp_path: Path,
    initial_chars: int,
    expanded_chars: int,
    expected_chars: int,
    expansion_calls: int,
) -> None:
    class LengthVariantClient(FakeClient):
        def complete(self, model_cfg, messages, *, stage):
            if stage not in ("chapter", "chapter_expansion"):
                return super().complete(model_cfg, messages, stage=stage)
            self.calls.append((stage, messages))
            body_chars = (
                initial_chars if stage == "chapter" else expanded_chars
            )
            chapter_text = "## 第1章 潮声1\n\n" + "汉" * body_chars
            return ChatResult(
                content=chapter_text,
                usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                requested_model=model_cfg["model"],
                response_model=model_cfg["model"],
                finish_reason="stop",
                response_id=f"r-{len(self.calls)}",
                latency_ms=1,
                raw_response={"id": f"r-{len(self.calls)}"},
            )

    client = LengthVariantClient()
    prompts = generate_module.load_prompts(
        Path(__file__).resolve().parents[1] / "runner" / "prompts" / "v2.1"
    )
    run = make_run(tmp_path, client, prompts=prompts)
    assert run.execute("chapter:1") is False
    chapter_calls = [messages for stage, messages in client.calls if stage == "chapter"]
    assert len(chapter_calls) == 1
    expansion_messages = [
        messages for stage, messages in client.calls if stage == "chapter_expansion"
    ]
    assert len(expansion_messages) == expansion_calls
    assert count_content_chars(
        run.accepted_dir.joinpath("chapters", "01.md").read_text(encoding="utf-8")
    ) == expected_chars
    session = json.loads(run.session_path.read_text(encoding="utf-8"))["messages"]
    final_text = "## 第1章 潮声1\n\n" + "汉" * expected_chars
    assert final_text in [message["content"] for message in session]
    assert len(
        [stage for stage, _messages in client.calls if stage == "chapter_expansion"]
    ) == expansion_calls
    assert list(run.failures_dir.glob("chapter_01_attempt_*.json")) == []


def test_short_chapter_expansion_is_isolated_and_resume_safe(
    tmp_path: Path,
) -> None:
    short_text = "## 第1章 潮声1\n\n" + "短" * 2_000
    expanded_text = "## 第1章 潮声1\n\n" + "长" * 3_200

    class ExpandingClient(FakeClient):
        def complete(self, model_cfg, messages, *, stage):
            if stage not in ("chapter", "chapter_expansion"):
                return super().complete(model_cfg, messages, stage=stage)
            self.calls.append((stage, messages))
            content = short_text if stage == "chapter" else expanded_text
            return ChatResult(
                content=content,
                usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                requested_model=model_cfg["model"],
                response_model=model_cfg["model"],
                finish_reason="stop",
                response_id=f"r-{len(self.calls)}",
                latency_ms=1,
                raw_response={"id": f"r-{len(self.calls)}"},
            )

    prompts = generate_module.load_prompts(
        Path(__file__).resolve().parents[1] / "runner" / "prompts" / "v2.1"
    )
    client = ExpandingClient()
    run = make_run(tmp_path, client, prompts=prompts)
    assert run.execute("chapter:1") is False
    assert [stage for stage, _messages in client.calls] == [
        "book",
        "macro_outline",
        "opening_outline",
        "chapter",
        "chapter_expansion",
    ]
    expansion_messages = client.calls[-1][1]
    assert [message["role"] for message in expansion_messages[-3:]] == [
        "user",
        "assistant",
        "user",
    ]
    assert expansion_messages[-2]["content"] == short_text
    assert "目前约 2000" in expansion_messages[-1]["content"]

    accepted = run.accepted_dir / "chapters" / "01.md"
    assert count_content_chars(accepted.read_text(encoding="utf-8")) == 3_200
    session = json.loads(run.session_path.read_text(encoding="utf-8"))["messages"]
    assert expanded_text in [message["content"] for message in session]
    assert short_text not in [message["content"] for message in session]
    assert not any("目前约 2000" in message["content"] for message in session)
    assert len(session) == 1 + 2 * 4

    state = json.loads(run.state_path.read_text(encoding="utf-8"))
    assert state["chapter_expansions"]["01"] == {
        "requested": True,
        "threshold_chars": 3_000,
        "initial_chars": 2_000,
        "result_chars": 3_200,
        "adopted": True,
        "outcome": "adopted",
    }
    expansion_usage = [
        record
        for record in run._usage_records()
        if record["stage"] == "chapter_expansion"
    ]
    assert len(expansion_usage) == 1
    assert expansion_usage[0]["attempt"] == 1
    assert expansion_usage[0]["context_audit"]["api_optional_parameters"] == []
    assert expansion_usage[0]["context_audit"]["max_tokens_sent"] is False

    usage_before = run.usage_path.read_bytes()
    resumed_client = ExpandingClient()
    resumed = make_run(tmp_path, resumed_client, prompts=prompts)
    assert resumed.execute("chapter:1") is False
    assert resumed_client.calls == []
    assert resumed.usage_path.read_bytes() == usage_before


@pytest.mark.parametrize(
    ("expansion_mode", "expected_chars", "expected_outcome"),
    [
        ("success", 3_200, "adopted"),
        ("api_error", 2_000, "kept_source_api_error"),
    ],
)
def test_usage_journal_recovers_expansion_crash_without_repeating_api(
    tmp_path: Path,
    expansion_mode: str,
    expected_chars: int,
    expected_outcome: str,
) -> None:
    short_text = "## 第1章 潮声1\n\n" + "短" * 2_000
    expanded_text = "## 第1章 潮声1\n\n" + "长" * 3_200

    class CrashWindowClient(FakeClient):
        def complete(self, model_cfg, messages, *, stage):
            if stage not in ("chapter", "chapter_expansion"):
                return super().complete(model_cfg, messages, stage=stage)
            self.calls.append((stage, messages))
            if stage == "chapter_expansion" and expansion_mode == "api_error":
                raise LLMAPIError("上游 504", status_code=504)
            content = short_text if stage == "chapter" else expanded_text
            return ChatResult(
                content=content,
                usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                requested_model=model_cfg["model"],
                response_model=model_cfg["model"],
                finish_reason="stop",
                response_id=f"r-{len(self.calls)}",
                latency_ms=1,
                raw_response={"id": f"r-{len(self.calls)}"},
            )

    prompts = generate_module.load_prompts(
        Path(__file__).resolve().parents[1] / "runner" / "prompts" / "v2.1"
    )
    client = CrashWindowClient()
    run = make_run(tmp_path, client, prompts=prompts)
    assert run.execute("opening-outline") is False
    opening = json.loads(
        (run.accepted_dir / "opening_outline.json").read_text(encoding="utf-8")
    )
    chapter = opening["chapters"][0]
    original_prompt = prompts["chapter.md"].format(
        chapter_number=1,
        chapter_title=chapter["title"],
        chapter_summary=chapter["summary"],
        chapter_beats=json.dumps(chapter["beats"], ensure_ascii=False),
        continuity_in=json.dumps(chapter["continuity_in"], ensure_ascii=False),
        continuity_out=json.dumps(chapter["continuity_out"], ensure_ascii=False),
        foreshadowing=json.dumps(chapter["foreshadowing"], ensure_ascii=False),
        target_chars=chapter["target_chars"],
    )
    base_history = list(run.session["messages"])
    source_response = run._call(
        original_prompt,
        stage="chapter",
        attempt=1,
        chapter=1,
        history=base_history,
        persist=False,
    )
    expansion_prompt = prompts["expand_chapter.md"].format(
        chapter_number=1,
        chapter_title=chapter["title"],
        current_chars=count_content_chars(validate_chapter(source_response, chapter)),
    )
    expansion_history = [
        *base_history,
        {"role": "user", "content": original_prompt},
        {"role": "assistant", "content": source_response},
    ]
    if expansion_mode == "api_error":
        with pytest.raises(LLMAPIError, match="504"):
            run._call(
                expansion_prompt,
                stage="chapter_expansion",
                attempt=1,
                chapter=1,
                history=expansion_history,
                persist=False,
            )
    else:
        assert run._call(
            expansion_prompt,
            stage="chapter_expansion",
            attempt=1,
            chapter=1,
            history=expansion_history,
            persist=False,
        ) == expanded_text

    usage_before = run.usage_path.read_bytes()
    assert generate_module.work_checkpoint_is_resumable(run.work_dir, run.run_id)
    resumed_client = FakeClient()
    resumed = make_run(tmp_path, resumed_client, prompts=prompts)
    assert resumed.execute("chapter:1") is False
    assert resumed_client.calls == []
    assert resumed.usage_path.read_bytes() == usage_before
    expansion_records = [
        record
        for record in resumed._usage_records()
        if record["stage"] == "chapter_expansion" and record["chapter"] == 1
    ]
    assert len(expansion_records) == 1
    accepted = resumed.accepted_dir / "chapters" / "01.md"
    assert count_content_chars(accepted.read_text(encoding="utf-8")) == expected_chars
    state = json.loads(resumed.state_path.read_text(encoding="utf-8"))
    assert state["chapter_expansions"]["01"]["outcome"] == expected_outcome
    session = json.loads(resumed.session_path.read_text(encoding="utf-8"))["messages"]
    assert (expanded_text in [message["content"] for message in session]) == (
        expansion_mode == "success"
    )
    assert (short_text in [message["content"] for message in session]) == (
        expansion_mode == "api_error"
    )


@pytest.mark.parametrize(
    ("failure_mode", "expected_outcome"),
    [
        ("api", "kept_source_api_error"),
        ("length", "kept_source_incomplete"),
        ("invalid", "kept_source_invalid"),
    ],
)
def test_failed_expansion_is_audited_once_and_keeps_valid_source(
    tmp_path: Path,
    failure_mode: str,
    expected_outcome: str,
) -> None:
    source_text = "## 第1章 潮声1\n\n" + "源" * 2_000

    class FailedExpansionClient(FakeClient):
        def complete(self, model_cfg, messages, *, stage):
            if stage == "chapter":
                self.calls.append((stage, messages))
                return ChatResult(
                    content=source_text,
                    usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                    requested_model=model_cfg["model"],
                    response_model=model_cfg["model"],
                    finish_reason="stop",
                    response_id=f"r-{len(self.calls)}",
                    latency_ms=1,
                    raw_response={"id": f"r-{len(self.calls)}"},
                )
            if stage != "chapter_expansion":
                return super().complete(model_cfg, messages, stage=stage)
            self.calls.append((stage, messages))
            if failure_mode == "api":
                raise LLMAPIError("上游 504", status_code=504)
            return ChatResult(
                content=(
                    "不是正文"
                    if failure_mode == "invalid"
                    else "## 第1章 潮声1\n\n" + "扩" * 3_200
                ),
                usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                requested_model=model_cfg["model"],
                response_model=model_cfg["model"],
                finish_reason="length" if failure_mode == "length" else "stop",
                response_id=f"r-{len(self.calls)}",
                latency_ms=1,
                raw_response={"id": f"r-{len(self.calls)}"},
            )

    prompts = generate_module.load_prompts(
        Path(__file__).resolve().parents[1] / "runner" / "prompts" / "v2.1"
    )
    client = FailedExpansionClient()
    run = make_run(tmp_path, client, prompts=prompts)
    assert run.execute("chapter:1") is False
    assert [stage for stage, _messages in client.calls].count("chapter_expansion") == 1
    accepted = run.accepted_dir / "chapters" / "01.md"
    assert count_content_chars(accepted.read_text(encoding="utf-8")) == 2_000
    state = json.loads(run.state_path.read_text(encoding="utf-8"))
    assert state["chapter_expansions"]["01"]["outcome"] == expected_outcome
    assert list(run.failures_dir.glob("chapter_expansion_01_attempt_01.json"))
    assert list(run.failures_dir.glob("chapter_01_attempt_*.json")) == []
    session = json.loads(run.session_path.read_text(encoding="utf-8"))["messages"]
    assert source_text in [message["content"] for message in session]


@pytest.mark.parametrize(
    ("checkpoint", "first_calls"),
    [
        ("book", 1),
        ("macro-outline", 2),
        ("opening-outline", 3),
        ("chapter:1", 4),
        ("chapter:7", 10),
        ("chapter:16", 19),
    ],
)
def test_stop_after_resumes_without_repeating_accepted_calls(
    tmp_path: Path,
    checkpoint: str,
    first_calls: int,
) -> None:
    first_client = FakeClient()
    first = make_run(tmp_path, first_client)
    assert first.execute(checkpoint) is False
    assert len(first_client.calls) == first_calls
    assert not (first.result_dir / "manifest.json").exists()

    resumed_client = FakeClient()
    resumed = make_run(tmp_path, resumed_client)
    assert resumed.execute() is True
    assert first_calls + len(resumed_client.calls) == 19
    assert result_is_complete(resumed.result_dir, resumed.run_id)
    messages = json.loads(resumed.session_path.read_text(encoding="utf-8"))["messages"]
    assert len(messages) == 1 + 2 * 19


def test_reconciles_committed_files_if_state_write_was_interrupted(tmp_path: Path) -> None:
    first_client = FakeClient()
    first = make_run(tmp_path, first_client)
    assert first.execute("chapter:7") is False

    state = json.loads(first.state_path.read_text(encoding="utf-8"))
    state["next_chapter"] = 7
    state["completed_chapters"].remove(7)
    first.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    resumed_client = FakeClient()
    resumed = make_run(tmp_path, resumed_client)
    assert resumed.execute("chapter:7") is False
    assert resumed_client.calls == []
    repaired_state = json.loads(resumed.state_path.read_text(encoding="utf-8"))
    assert repaired_state["next_chapter"] == 8
    assert 7 in repaired_state["completed_chapters"]


def test_validation_failures_are_audited_and_retries_are_manifested(tmp_path: Path) -> None:
    client = FakeClient(invalid_once="book")
    run = make_run(tmp_path, client)
    assert run.execute()
    failures = list(run.failures_dir.glob("book_attempt_*.json"))
    assert len(failures) == 1
    failure = json.loads(failures[0].read_text(encoding="utf-8"))
    assert failure["stage"] == "book"
    assert failure["response_sha256"]
    manifest = json.loads((run.result_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["attempts"]["book"] == 2
    assert manifest["retry_count"] == 1
    public_manifest = (run.result_dir / "manifest.json").read_text(encoding="utf-8").lower()
    assert "reasoning_content" not in public_manifest
    assert "raw_response" not in public_manifest
    session = json.loads(run.session_path.read_text(encoding="utf-8"))["messages"]
    assert len(session) == 1 + 2 * 19
    assert all(message["content"] != "{}" for message in session)


def test_empty_api_content_is_audited_counted_and_retried(tmp_path: Path) -> None:
    client = FakeClient(api_error_once="book")
    delays: list[float] = []
    run = make_run(tmp_path, client, sleep_fn=delays.append)
    assert run.execute()
    assert delays == [1.0]
    assert len(client.calls) == 20
    manifest = json.loads((run.result_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["attempts"]["book"] == 2
    assert manifest["retry_count"] == 1
    assert manifest["usage"]["calls"] == 20
    assert list(run.raw_dir.glob("*_book_error.json"))
    assert list(run.failures_dir.glob("book_attempt_01.json"))
    public_manifest = (run.result_dir / "manifest.json").read_text(encoding="utf-8")
    assert "PRIVATE_REASONING" not in public_manifest


def test_nonempty_length_completion_is_repaired_not_accepted(tmp_path: Path) -> None:
    client = FakeClient(finish_reason_once="chapter")
    run = make_run(tmp_path, client)
    assert run.execute()
    manifest = json.loads((run.result_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["attempts"]["chapter_01"] == 2
    assert len(client.calls) == 20
    failure = json.loads(
        next(run.failures_dir.glob("chapter_01_attempt_01.json")).read_text(
            encoding="utf-8"
        )
    )
    assert "finish_reason=length" in failure["error"]
    messages = json.loads(run.session_path.read_text(encoding="utf-8"))["messages"]
    assert len(messages) == 1 + 2 * 19
    assert not any("finish_reason=length" in message["content"] for message in messages)
    chapter_calls = [messages for stage, messages in client.calls if stage == "chapter"]
    assert len(chapter_calls[0]) == len(chapter_calls[1])
    assert "finish_reason=length" not in chapter_calls[1][-1]["content"]


def test_retry_delay_uses_exponential_backoff_and_retry_after() -> None:
    assert retry_delay_seconds(LLMAPIError("transient"), 1) == 1.0
    assert retry_delay_seconds(LLMAPIError("transient"), 4) == 8.0
    assert retry_delay_seconds(
        LLMAPIError("limited", status_code=429, retry_after_seconds=12.5), 1
    ) == 12.5
    assert retry_delay_seconds(
        LLMAPIError("limited", status_code=429, retry_after_seconds=300), 1
    ) == 30.0


def test_exhausted_transient_attempts_can_resume_in_a_later_process(
    tmp_path: Path,
) -> None:
    class AlwaysLimitedClient(FakeClient):
        def complete(self, model_cfg, messages, *, stage):
            self.calls.append((stage, messages))
            raise LLMAPIError("limited", status_code=429)

    delays: list[float] = []
    first = make_run(tmp_path, AlwaysLimitedClient(), sleep_fn=delays.append)
    with pytest.raises(RuntimeError, match="本次运行连续 5 次"):
        first.execute("book")
    assert delays == [1.0, 2.0, 4.0, 8.0]
    assert json.loads(first.state_path.read_text(encoding="utf-8"))["attempts"]["book"] == 5

    second_delays: list[float] = []
    second = GenerationRun(
        root=tmp_path,
        benchmark="reform-era",
        direction="改革开放初期的中国现实主义长篇。",
        prompts=PROMPTS,
        model_cfg=MODEL,
        client=AlwaysLimitedClient(),
        new_run=True,
        sleep_fn=second_delays.append,
    )
    with pytest.raises(RuntimeError, match="本次运行连续 5 次"):
        second.execute("book")
    assert second_delays == [1.0, 2.0, 4.0, 8.0]

    healthy = FakeClient()
    resumed = GenerationRun(
        root=tmp_path,
        benchmark="reform-era",
        direction="改革开放初期的中国现实主义长篇。",
        prompts=PROMPTS,
        model_cfg=MODEL,
        client=healthy,
        new_run=True,
        sleep_fn=lambda _delay: None,
    )
    assert resumed.execute("book") is False
    assert len(healthy.calls) == 1
    assert json.loads(resumed.state_path.read_text(encoding="utf-8"))["attempts"]["book"] == 11


def test_new_run_clears_a_corrupt_completed_checkpoint(tmp_path: Path) -> None:
    completed = make_run(tmp_path, FakeClient())
    assert completed.execute()
    (completed.accepted_dir / "chapters" / "01.md").unlink()

    client = FakeClient()
    restarted = GenerationRun(
        root=tmp_path,
        benchmark="reform-era",
        direction="改革开放初期的中国现实主义长篇。",
        prompts=PROMPTS,
        model_cfg=MODEL,
        client=client,
        new_run=True,
        sleep_fn=lambda _delay: None,
    )
    assert restarted.execute("book") is False
    assert len(client.calls) == 1
    state = json.loads(restarted.state_path.read_text(encoding="utf-8"))
    assert state["stage"] == "macro_outline"
    assert state["attempts"] == {"book": 1}


def test_same_run_rejects_a_second_concurrent_executor(tmp_path: Path) -> None:
    first = make_run(tmp_path, FakeClient())
    second_client = FakeClient()
    second = make_run(tmp_path, second_client)

    with generate_module.WorkDirLock(first.model_work_root / ".run.lock"):
        with pytest.raises(RuntimeError, match="另一个生成进程占用"):
            second.execute("book")
    assert second_client.calls == []


def test_partial_legacy_usage_migration_keeps_every_record(tmp_path: Path) -> None:
    run = make_run(tmp_path, FakeClient())
    assert run.execute("macro-outline") is False
    records = [
        json.loads(line)
        for line in run.usage_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 2
    (run.usage_events_dir / "000002.json").unlink()

    reconciled = run._usage_records()
    assert reconciled == records
    assert [path.name for path in sorted(run.usage_events_dir.glob("*.json"))] == [
        "000001.json",
        "000002.json",
    ]


def test_context_guard_stops_before_client_call(tmp_path: Path) -> None:
    client = FakeClient()
    tiny_model = {**MODEL, "context_window": 50}
    run = GenerationRun(
        root=tmp_path,
        benchmark="reform-era",
        direction="改革开放初期的中国现实主义长篇。",
        prompts=PROMPTS,
        model_cfg=tiny_model,
        client=client,
        new_run=False,
    )
    with pytest.raises(RuntimeError, match="85%"):
        run.execute("book")
    assert client.calls == []


def test_generation_run_rejects_optional_api_controls_before_call(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    controlled_model = {**MODEL, "request": {"temperature": 0.7}}
    run = GenerationRun(
        root=tmp_path,
        benchmark="reform-era",
        direction="改革开放初期的中国现实主义长篇。",
        prompts=PROMPTS,
        model_cfg=controlled_model,
        client=client,
        new_run=False,
    )
    run._initialize_work()
    with pytest.raises(RuntimeError, match="服务端默认参数"):
        run._call("立项", stage="book", attempt=1)
    assert client.calls == []

    expansion_controlled = {
        **MODEL,
        "stages": {"chapter_expansion": {"max_tokens": 8192}},
    }
    expansion_run = GenerationRun(
        root=tmp_path / "expansion",
        benchmark="reform-era",
        direction="改革开放初期的中国现实主义长篇。",
        prompts=PROMPTS,
        model_cfg=expansion_controlled,
        client=client,
        new_run=False,
    )
    expansion_run._initialize_work()
    with pytest.raises(RuntimeError, match="服务端默认参数"):
        expansion_run._call(
            "扩写",
            stage="chapter_expansion",
            attempt=1,
            chapter=1,
        )
    assert client.calls == []


def test_context_guard_uses_matching_provider_usage_anchor(tmp_path: Path) -> None:
    client = FakeClient()
    anchored_model = {
        **MODEL,
        "context_window": 450,
    }
    run = GenerationRun(
        root=tmp_path,
        benchmark="reform-era",
        direction="改革开放初期的中国现实主义长篇。",
        prompts=PROMPTS,
        model_cfg=anchored_model,
        client=client,
        new_run=False,
    )
    run._initialize_work()
    run.session["messages"].extend(
        [
            {"role": "user", "content": "汉" * 1_000},
            {"role": "assistant", "content": "修订完成"},
        ]
    )
    run._save_session()
    prior_prompt = run.session["messages"][:-1]
    run._append_usage_record(
        {
            "stage": "chapter",
            "chapter": 1,
            "attempt": 1,
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 100_000,
                "total_tokens": 100_050,
            },
            "context_audit": {
                "prompt_message_count": len(prior_prompt),
                "prompt_sha256": generate_module.sha256_text(
                    generate_module.canonical_json(prior_prompt)
                ),
                "assistant_content_sha256": generate_module.sha256_text("修订完成"),
            },
        }
    )
    pending = [*run.session["messages"], {"role": "user", "content": "请写第 1 章"}]
    assert estimate_tokens(pending) > 382
    assert estimate_prompt_tokens(pending, run._usage_records()) < 382

    run._call("请写第 1 章", stage="chapter", attempt=2, chapter=1)
    assert len(client.calls) == 1


def test_context_anchor_mismatch_falls_back(tmp_path: Path) -> None:
    run = make_run(tmp_path, FakeClient())
    run._initialize_work()
    run.session["messages"].extend(
        [
            {"role": "user", "content": "汉" * 1_000},
            {"role": "assistant", "content": "修订完成"},
        ]
    )
    pending = [*run.session["messages"], {"role": "user", "content": "下一章"}]
    records = [
        {
            "event_index": 1,
            "usage": {"prompt_tokens": 50, "completion_tokens": 100_000},
            "context_audit": {
                "prompt_message_count": 2,
                "prompt_sha256": "0" * 64,
                "assistant_content_sha256": generate_module.sha256_text("修订完成"),
            },
        }
    ]
    assert estimate_prompt_tokens(pending, records) == estimate_tokens(pending)


def test_v21_run_uses_isolated_work_root_and_rejects_schema_drift(
    tmp_path: Path,
) -> None:
    run = make_run(tmp_path, FakeClient())
    run._initialize_work()
    assert "v2.1" in run.work_dir.parts
    state = json.loads(run.state_path.read_text(encoding="utf-8"))
    state["schema"] = "novel-benchmark.v2"
    run.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    assert not generate_module.work_checkpoint_is_resumable(run.work_dir, run.run_id)


def test_fixed_registry_rejects_substitution() -> None:
    models = [
        {"id": model_id, "model": model_id}
        for model_id in EXPECTED_GENERATOR_IDS
    ]
    judges = [
        {"id": judge_id, "model": model_id}
        for judge_id, model_id in EXPECTED_JUDGES.items()
    ]
    validated_models, validated_judges = validate_fixed_registries(
        {"models": models, "judges": judges}
    )
    assert len(validated_models) == 19
    assert len(validated_judges) == len(EXPECTED_JUDGES)
    substituted = [dict(item) for item in models]
    substituted[0]["model"] = "silent-fallback"
    with pytest.raises(ValueError, match="静默替换"):
        validate_fixed_registries({"models": substituted, "judges": judges})


def test_cli_preflight_checks_every_generator_and_judge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_fixed_config()
    write_cli_workspace(tmp_path, config)
    all_wire_ids = {
        *(item["model"] for item in config["models"]),
        *(item["model"] for item in config["judges"]),
    }

    class MissingUnselectedClient:
        @classmethod
        def from_config(cls, *args, **kwargs):
            return cls()

        def list_models(self):
            return all_wire_ids - {"agnes-2.0-flash"}

    monkeypatch.setattr(generate_module, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(generate_module, "ChatClient", MissingUnselectedClient)
    assert generate_module.main(["--model", "deepseek-v4-flash", "--dry-run"]) == 1
    assert "agnes-2.0-flash" in capsys.readouterr().err

    class CompleteClient(MissingUnselectedClient):
        def list_models(self):
            return all_wire_ids

    monkeypatch.setattr(generate_module, "ChatClient", CompleteClient)
    assert generate_module.main(["--model", "deepseek-v4-flash", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert f"全部 19 个生成模型、{len(EXPECTED_JUDGES)} 个评委" in output
    assert "85%安全线" in output
    assert "api_optional_params=none（服务端默认）" in output
    assert "基础调用=19–21" in output
    assert "最多追加16–18次扩写" in output
    assert "总计35–39" in output
    assert "未发 completion 请求" in output


def test_cli_skips_valid_completed_result_before_reading_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_fixed_config()
    write_cli_workspace(tmp_path, config)
    prompts = generate_module.load_prompts(tmp_path / "runner" / "prompts" / "v2")
    run = GenerationRun(
        root=tmp_path,
        benchmark="reform-era",
        direction="改革开放初期的中国现实主义长篇。",
        prompts=prompts,
        model_cfg=config["models"][0],
        client=FakeClient(),
        new_run=False,
    )
    assert run.execute()
    backup = run.result_dir.with_name(f".{run.result_dir.name}.backup-test")
    backup.mkdir()
    (backup / "old.txt").write_text("old", encoding="utf-8")
    monkeypatch.setattr(generate_module, "repo_root", lambda: tmp_path)

    def key_read_is_a_failure(path: Path) -> dict[str, str]:
        raise AssertionError("valid completed result should skip before .env/API key read")

    monkeypatch.setattr(generate_module, "load_env_file", key_read_is_a_failure)
    assert generate_module.main(["--model", "deepseek-v4-flash"]) == 0
    assert not backup.exists()


def test_new_run_with_stale_public_result_can_resume_without_restarting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_fixed_config()
    write_cli_workspace(tmp_path, config)
    current_prompts = generate_module.load_prompts(
        tmp_path / "runner" / "prompts" / "v2"
    )
    old_prompts = {**current_prompts, "system.md": current_prompts["system.md"] + "旧协议"}
    old = GenerationRun(
        root=tmp_path,
        benchmark="reform-era",
        direction="改革开放初期的中国现实主义长篇。",
        prompts=old_prompts,
        model_cfg=config["models"][0],
        client=FakeClient(),
        new_run=False,
    )
    assert old.execute()

    available = {
        *(item["model"] for item in config["models"]),
        *(item["model"] for item in config["judges"]),
    }

    class PreflightClient(FakeClient):
        def list_models(self):
            return available

    class ClientFactory:
        active: PreflightClient

        @classmethod
        def from_config(cls, *args, **kwargs):
            return cls.active

    monkeypatch.setattr(generate_module, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(generate_module, "ChatClient", ClientFactory)

    first = PreflightClient()
    ClientFactory.active = first
    assert generate_module.main(
        [
            "--model",
            "deepseek-v4-flash",
            "--new-run",
            "--stop-after",
            "chapter:1",
        ]
    ) == 0
    assert len(first.calls) == 4

    resumed = PreflightClient()
    ClientFactory.active = resumed
    assert generate_module.main(
        ["--model", "deepseek-v4-flash", "--stop-after", "chapter:2"]
    ) == 0
    assert [stage for stage, _messages in resumed.calls] == ["chapter"]


def test_prompt_change_with_only_partial_old_work_requires_new_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_fixed_config()
    write_cli_workspace(tmp_path, config)
    current_prompts = generate_module.load_prompts(
        tmp_path / "runner" / "prompts" / "v2"
    )
    old_prompts = {**current_prompts, "system.md": current_prompts["system.md"] + "旧协议"}
    old = GenerationRun(
        root=tmp_path,
        benchmark="reform-era",
        direction="改革开放初期的中国现实主义长篇。",
        prompts=old_prompts,
        model_cfg=config["models"][0],
        client=FakeClient(),
        new_run=False,
        sleep_fn=lambda _delay: None,
    )
    assert old.execute("book") is False
    assert not old.result_dir.exists()

    available = {
        *(item["model"] for item in config["models"]),
        *(item["model"] for item in config["judges"]),
    }

    class PreflightClient(FakeClient):
        @classmethod
        def from_config(cls, *args, **kwargs):
            return cls()

        def list_models(self):
            return available

    monkeypatch.setattr(generate_module, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(generate_module, "ChatClient", PreflightClient)
    assert generate_module.main(["--model", "deepseek-v4-flash"]) == 1
    assert "请使用 --new-run" in capsys.readouterr().err

    assert generate_module.main(
        ["--model", "deepseek-v4-flash", "--new-run", "--stop-after", "book"]
    ) == 0


def test_run_id_changes_with_prompt() -> None:
    first = calculate_run_id("reform-era", "方向", PROMPTS, MODEL)
    changed = dict(PROMPTS)
    changed["system.md"] += "改"
    second = calculate_run_id("reform-era", "方向", changed, MODEL)
    assert first != second
    changed_expansion = dict(PROMPTS)
    changed_expansion["expand_chapter.md"] += "扩"
    assert first != calculate_run_id(
        "reform-era", "方向", changed_expansion, MODEL
    )
    changed_model = {**MODEL, "context_window": MODEL["context_window"] - 1}
    assert first != calculate_run_id("reform-era", "方向", PROMPTS, changed_model)
    assert first != calculate_run_id("reform-era", "另一个方向", PROMPTS, MODEL)


def test_run_id_changes_with_protocol_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    first = calculate_run_id("reform-era", "方向", PROMPTS, MODEL)
    monkeypatch.setattr(
        generate_module,
        "PROTOCOL_POLICY",
        {**generate_module.PROTOCOL_POLICY, "test_policy_revision": 1},
    )
    assert first != calculate_run_id("reform-era", "方向", PROMPTS, MODEL)


def test_run_id_normalizes_bom_and_line_endings() -> None:
    multiline = {name: value + "\n第二行" for name, value in PROMPTS.items()}
    noisy = {
        name: "\ufeff" + value.replace("\n", "\r\n")
        for name, value in multiline.items()
    }
    assert calculate_run_id("reform-era", "方向\n补充", multiline, MODEL) == calculate_run_id(
        "reform-era", "\ufeff方向\r\n补充", noisy, MODEL
    )


def test_run_id_changes_with_provider_request_defaults() -> None:
    config = {
        "providers": {
            "new-api": {"request_defaults": {"top_p": 0.8}}
        }
    }
    tracked = with_provider_request_defaults(config, MODEL)
    assert calculate_run_id("reform-era", "方向", PROMPTS, MODEL) != calculate_run_id(
        "reform-era", "方向", PROMPTS, tracked
    )


def test_generation_identities_are_guarded_by_exact_registry_only_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        generate_module._generation_compatibility_source_hash()
        == generate_module.GENERATION_COMPATIBILITY_SOURCE_SHA256
    )
    legacy_run_id = calculate_run_id("reform-era", "方向", PROMPTS, MODEL)
    anthropic_model = {**MODEL, "protocol": "anthropic-messages"}
    legacy_anthropic_run_id = calculate_run_id(
        "reform-era", "方向", PROMPTS, anthropic_model
    )
    current_hash = generate_module._current_source_code_hash()
    assert generate_module.calculate_code_hash(MODEL) == (
        generate_module.LEGACY_OPENAI_CODE_SHA256
    )
    assert generate_module.calculate_code_hash(anthropic_model) == (
        generate_module.LEGACY_ANTHROPIC_CODE_SHA256
    )

    monkeypatch.setattr(
        generate_module,
        "_generation_compatibility_source_hash",
        lambda: "changed-source",
    )
    assert generate_module.calculate_code_hash(MODEL) == current_hash
    assert generate_module.calculate_code_hash(anthropic_model) == current_hash
    assert calculate_run_id("reform-era", "方向", PROMPTS, MODEL) != legacy_run_id
    assert (
        calculate_run_id("reform-era", "方向", PROMPTS, anthropic_model)
        != legacy_anthropic_run_id
    )


def test_anthropic_required_max_tokens_are_audited_as_protocol_metadata(
    tmp_path: Path,
) -> None:
    class AnthropicClient:
        def complete(self, model_cfg, messages, *, stage):
            return ChatResult(
                content="正文",
                usage={
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
                requested_model=str(model_cfg["model"]),
                response_model="MiniMax-M3",
                finish_reason="stop",
                native_finish_reason="end_turn",
                protocol="anthropic-messages",
                endpoint_path="/v1/messages",
                raw_response={
                    "id": "msg-audit",
                    "model": "MiniMax-M3",
                    "content": [{"type": "text", "text": "正文"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                },
            )

    model = {
        **MODEL,
        "protocol": "anthropic-messages",
        "protocol_required": {"max_tokens": 204_800},
    }
    run = GenerationRun(
        root=tmp_path,
        benchmark="reform-era",
        direction="改革开放初期的中国现实主义长篇。",
        prompts=PROMPTS,
        model_cfg=model,
        client=AnthropicClient(),
        new_run=False,
        sleep_fn=lambda _delay: None,
    )
    run._initialize_work()
    assert run._call("继续写", stage="chapter", attempt=1, persist=False) == "正文"
    record = run._usage_records()[0]
    audit = record["context_audit"]
    assert audit["wire_protocol"] == "anthropic-messages"
    assert audit["endpoint_path"] == "/v1/messages"
    assert audit["protocol_required_parameters"] == ["max_tokens"]
    assert audit["configured_max_tokens"] == 204_800
    assert audit["max_tokens_sent"] is True
    assert audit["api_optional_parameters"] == []
    assert record["protocol"] == "anthropic-messages"
    assert record["endpoint_path"] == "/v1/messages"
    assert record["native_finish_reason"] == "end_turn"


def test_anthropic_empty_response_failure_usage_is_normalized(
    tmp_path: Path,
) -> None:
    model = {
        **MODEL,
        "protocol": "anthropic-messages",
        "protocol_required": {"max_tokens": 65_536},
    }
    run = GenerationRun(
        root=tmp_path,
        benchmark="reform-era",
        direction="改革开放初期的中国现实主义长篇。",
        prompts=PROMPTS,
        model_cfg=model,
        client=FakeClient(),
        new_run=False,
        sleep_fn=lambda _delay: None,
    )
    run._initialize_work()
    error = LLMAPIError(
        "LLM API 返回空内容",
        raw_response={
            "id": "msg-empty",
            "model": "claude-test",
            "content": [],
            "stop_reason": "max_tokens",
            "usage": {
                "input_tokens": 11,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 2,
                "output_tokens": 7,
            },
        },
    )
    run._append_failed_usage(
        "chapter",
        error,
        attempt=1,
        chapter=1,
        context_audit={"wire_protocol": "anthropic-messages"},
    )
    record = run._usage_records()[0]
    assert record["finish_reason"] == "length"
    assert record["native_finish_reason"] == "max_tokens"
    assert record["usage"]["prompt_tokens"] == 16
    assert record["usage"]["completion_tokens"] == 7
    assert record["usage"]["total_tokens"] == 23
