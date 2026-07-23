from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_GENERATORS = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "mimo-v2.5",
    "mimo-v2.5-pro",
    "minimax-m3",
    "glm-5.2",
    "gpt-5.6-luna",
    "claude-haiku-4-5",
    "claude-sonnet-5",
    "gemini-3.1-pro",
    "gemini-3.5-flash",
    "kimi-k3",
    "grok-4.5",
    "claude-opus-4-8",
    "agnes-2.0-flash",
)
EXPECTED_JUDGES = {
    "sol": "gpt-5.6-sol",
    "fable": "claude-fable-5",
    "kimi": "kimi-k3",
}


def test_v2_protocol_inventory_and_direction_are_locked() -> None:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    provider = config["providers"]["new-api"]
    assert provider["base_url_env"] == "API_URL"
    assert provider["api_key_env"] == "API_KEY"
    assert provider.get("request_defaults") == {}

    models = config["models"]
    assert tuple(model["id"] for model in models) == EXPECTED_GENERATORS
    assert tuple(model["model"] for model in models) == EXPECTED_GENERATORS
    assert all(model["provider"] == "new-api" for model in models)
    assert "gpt-5.6-terra" not in {model["id"] for model in models}
    assert all(model.get("request") == {} for model in models)
    assert all(model.get("stages") == {} for model in models)

    judges = config["judges"]
    assert {judge["id"]: judge["model"] for judge in judges} == EXPECTED_JUDGES
    assert all(judge["provider"] == "new-api" for judge in judges)

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
