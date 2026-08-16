from __future__ import annotations

from pathlib import Path

import yaml

from runner.llm_api import ANTHROPIC_MESSAGES, OPENAI_CHAT_COMPLETIONS


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_GENERATORS = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "mimo-v2.5",
    "mimo-v2.5-pro",
    "minimax-m3",
    "glm-5.3",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "claude-haiku-4-5",
    "claude-fable-5",
    "claude-sonnet-4-6",
    "claude-sonnet-5",
    "gemini-2.5-pro",
    "gemini-3.1-pro",
    "gemini-3.7-flash",
    "kimi-k2.7-code",
    "kimi-k3",
    "grok-4.6",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "agnes-2.5-flash",
)
EXPECTED_JUDGES = {
    "sol": "gpt-5.6-sol",
    "grok": "grok-4.6",
    "opus": "claude-opus-5",
    "k3": "kimi-k3",
    "ds-v4-pro": "deepseek-v4-pro",
}
ANTHROPIC_GENERATORS = {
    "minimax-m3",
    "claude-haiku-4-5",
    "claude-fable-5",
    "claude-sonnet-4-6",
    "claude-sonnet-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
}
ANTHROPIC_JUDGES = {"opus"}


def assert_anthropic_protocol_required(
    entry: dict,
    *,
    require_more_than_8192: bool,
) -> None:
    assert entry.get("protocol") == ANTHROPIC_MESSAGES
    required = entry.get("protocol_required")
    assert isinstance(required, dict)
    assert set(required) == {"max_tokens"}
    max_tokens = required["max_tokens"]
    assert type(max_tokens) is int
    assert max_tokens > (8_192 if require_more_than_8192 else 0)
    request = entry.get("request") or {}
    assert "max_tokens" not in request
    stages = entry.get("stages") or {}
    assert all(
        "max_tokens" not in (stage_request or {})
        for stage_request in stages.values()
    )


def test_v2_protocol_inventory_and_direction_are_locked() -> None:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    provider = config["providers"]["new-api"]
    assert provider["base_url_env"] == "API_URL"
    assert provider["api_key_env"] == "API_KEY"
    assert provider["stream"] is True
    assert provider.get("request_defaults") == {}

    models = config["models"]
    assert tuple(model["id"] for model in models) == EXPECTED_GENERATORS
    assert tuple(model["model"] for model in models) == EXPECTED_GENERATORS
    assert all(model["provider"] == "new-api" for model in models)
    assert all(model.get("request") == {} for model in models)
    assert all(model.get("stages") == {} for model in models)
    models_by_id = {model["id"]: model for model in models}
    assert models_by_id["deepseek-v4-pro"]["revision"] == "2026-08-13"
    assert models_by_id["grok-4.6"]["supersedes"] == ["grok-4.5"]
    assert models_by_id["glm-5.3"]["supersedes"] == ["glm-5.2"]
    assert models_by_id["gemini-3.7-flash"]["supersedes"] == [
        "gemini-3.5-flash",
        "gemini-3.6-flash",
    ]
    for model in models:
        if model["id"] in ANTHROPIC_GENERATORS:
            assert_anthropic_protocol_required(
                model, require_more_than_8192=True
            )
        else:
            assert model.get("protocol", OPENAI_CHAT_COMPLETIONS) == (
                OPENAI_CHAT_COMPLETIONS
            )
            assert "protocol_required" not in model

    judges = config["judges"]
    assert {judge["id"]: judge["model"] for judge in judges} == EXPECTED_JUDGES
    assert all(judge["provider"] == "new-api" for judge in judges)
    judges_by_id = {judge["id"]: judge for judge in judges}
    assert judges_by_id["sol"]["request"] == {"max_tokens": 4096}
    assert judges_by_id["sol"]["stages"]["judge"]["temperature"] == 0.2
    grok = judges_by_id["grok"]
    assert grok["name"] == "Grok 4.6"
    assert grok["request"] == {"max_tokens": 4096}
    grok_stage = grok["stages"]["judge"]
    assert grok_stage["temperature"] == 0.2
    assert grok_stage["tool_choice"] == "none"
    assert grok_stage["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "unused_judge_tool",
                "description": "Never call this tool.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }
    ]
    assert "response_format" not in grok_stage
    assert grok.get("protocol", OPENAI_CHAT_COMPLETIONS) == (
        OPENAI_CHAT_COMPLETIONS
    )
    assert "protocol_required" not in grok
    opus = judges_by_id["opus"]
    assert opus["request"] == {}
    assert opus["stages"]["judge"] == {"temperature": 0.2}
    assert opus["protocol"] == ANTHROPIC_MESSAGES
    assert opus["protocol_required"] == {"max_tokens": 16384}
    assert judges_by_id["k3"]["request"] == {"max_tokens": 8192}
    assert judges_by_id["k3"]["stages"]["judge"] == {
        "response_format": {"type": "json_object"}
    }
    assert judges_by_id["ds-v4-pro"]["request"] == {"max_tokens": 8192}
    for judge in judges:
        if judge["id"] in ANTHROPIC_JUDGES:
            assert_anthropic_protocol_required(
                judge, require_more_than_8192=True
            )
            assert "response_format" not in judge["stages"]["judge"]
        else:
            assert judge.get("protocol", OPENAI_CHAT_COMPLETIONS) == (
                OPENAI_CHAT_COMPLETIONS
            )
            assert "protocol_required" not in judge

    direction = (ROOT / "benchmark" / "reform-era" / "direction.md").read_text(
        encoding="utf-8"
    )
    assert direction.strip() == "改革开放初期的中国现实主义长篇。"

    from runner.generate import PROTOCOL_VERSION
    from scripts.generate_site import GENERATION_PROTOCOL

    assert PROTOCOL_VERSION == "novel-benchmark.v2.1"
    assert GENERATION_PROTOCOL == PROTOCOL_VERSION


def test_pages_configuration_runs_only_in_the_authorized_deploy_job() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "generate.yml").read_text(encoding="utf-8")
    )
    build_steps = workflow["jobs"]["build"]["steps"]
    deploy = workflow["jobs"]["deploy"]

    assert all(step.get("uses") != "actions/configure-pages@v5" for step in build_steps)
    assert deploy["permissions"]["pages"] == "write"
    assert deploy["permissions"]["id-token"] == "write"
    assert any(
        step.get("uses") == "actions/configure-pages@v5"
        for step in deploy["steps"]
    )
