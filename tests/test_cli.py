from __future__ import annotations

from types import SimpleNamespace

import novel


def test_help_and_forwarding_are_single_entrypoint(capsys, monkeypatch) -> None:
    assert novel.main(["--help"]) == 0
    assert "generate" in capsys.readouterr().out
    received = []
    monkeypatch.setitem(
        novel.FORWARDED,
        "generate",
        ("test", lambda argv: received.extend(argv or []) or 7),
    )
    assert novel.main(["generate", "--model", "m"]) == 7
    assert received == ["--model", "m"]


def test_models_reports_exact_missing_registry(monkeypatch, capsys) -> None:
    config = {
        "models": [{"id": "a", "model": "a"}, {"id": "b", "model": "b"}],
        "judges": [{"id": "j", "model": "judge"}],
    }
    client = SimpleNamespace(stream=True, list_models=lambda: frozenset({"a", "judge", "extra"}))
    monkeypatch.setattr(novel, "_load_runtime", lambda *_args: (config, client))
    assert novel.main(["models"]) == 1
    output = capsys.readouterr().out
    assert "transport=stream" in output
    assert "missing=b" in output
    assert "unconfigured=extra" in output


def test_probe_compares_stream_and_non_stream_without_printing_content(
    monkeypatch, capsys
) -> None:
    config = {
        "models": [
            {
                "id": "m",
                "model": "wire",
                "provider": "new-api",
                "request": {},
                "stages": {},
            }
        ]
    }
    calls = []

    class Client:
        stream = True

        def list_models(self):
            return frozenset({"wire"})

        def complete(self, model_cfg, messages, **kwargs):
            calls.append((model_cfg, messages, kwargs))
            return SimpleNamespace(
                content="private output",
                finish_reason="stop",
                latency_ms=12,
                response_model="wire",
            )

    monkeypatch.setattr(novel, "_load_runtime", lambda *_args: (config, Client()))
    assert novel.main(["probe", "--model", "m", "--mode", "both"]) == 0
    assert [call[2]["stream"] for call in calls] == [True, False]
    output = capsys.readouterr().out
    assert "mode=stream" in output
    assert "mode=non-stream" in output
    assert "private output" not in output
