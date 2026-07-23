from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runner.llm_api import (  # noqa: E402
    ChatClient,
    LLMAPIError,
    ModelPreflightError,
    OpenAIChatClient,
    get_model_config,
    normalize_api_url,
    parse_retry_after,
    with_provider_request_defaults,
)


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class QueueOpener:
    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        if not self.responses:
            raise AssertionError("unexpected request")
        return FakeResponse(self.responses.pop(0))


class LLMAPITests(unittest.TestCase):
    def test_normalize_api_url(self) -> None:
        self.assertEqual(normalize_api_url("https://example.test"), "https://example.test/v1")
        self.assertEqual(normalize_api_url("https://example.test/api/v1/"), "https://example.test/api/v1")
        with self.assertRaises(ValueError):
            normalize_api_url("  ")

    def test_provider_request_defaults_are_part_of_tracked_model_config(self) -> None:
        config = {
            "providers": {
                "new-api": {"request_defaults": {"temperature": 0.35}}
            }
        }
        tracked = with_provider_request_defaults(
            config, {"id": "m", "model": "m", "provider": "new-api"}
        )
        self.assertEqual(
            tracked["provider_request_defaults"], {"temperature": 0.35}
        )

    def test_chat_result_and_explicit_payload(self) -> None:
        opener = QueueOpener(
            {
                "id": "chatcmpl-test",
                "model": "server-routed-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "正文",
                            "reasoning_content": "思考",
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                    "prompt_tokens_details": {"cached_tokens": 3},
                },
            }
        )
        client = OpenAIChatClient(
            "https://example.test/gateway",
            "fake-test-credential",
            urlopen=opener,
        )
        result = client.chat_completion(
            model="requested-model",
            messages=[{"role": "user", "content": "写一段"}],
            request_params={
                "temperature": 0.8,
                "response_format": {"type": "json_object"},
            },
        )

        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "https://example.test/gateway/v1/chat/completions")
        self.assertEqual(timeout, 180)
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            set(payload), {"model", "messages", "temperature", "response_format"}
        )
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(result.content, "正文")
        self.assertEqual(result.reasoning_content, "思考")
        self.assertEqual(result.requested_model, "requested-model")
        self.assertEqual(result.response_model, "server-routed-model")
        self.assertEqual(result.response_id, "chatcmpl-test")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.usage["prompt_tokens_details"]["cached_tokens"], 3)

    def test_content_and_reasoning_blocks_are_normalized(self) -> None:
        opener = QueueOpener(
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "thinking", "thinking": "内嵌私有推理"},
                                {"type": "text", "text": "第一段"},
                                {"type": "text", "text": "第二段"},
                            ],
                            "reasoning": [{"type": "thinking", "thinking": "推理"}],
                        }
                    }
                ]
            }
        )
        client = OpenAIChatClient("https://example.test/v1", "fake", urlopen=opener)
        result = client.chat_completion(
            model="m", messages=[{"role": "user", "content": "x"}]
        )
        self.assertEqual(result.content, "第一段第二段")
        self.assertNotIn("私有推理", result.content)
        self.assertEqual(result.reasoning_content, "推理\n内嵌私有推理")
        self.assertEqual(result.usage, {})

    def test_empty_content_error_exposes_only_safe_completion_metadata(self) -> None:
        opener = QueueOpener(
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "", "reasoning_content": "private trace"},
                    }
                ]
            }
        )
        client = OpenAIChatClient("https://example.test/v1", "fake", urlopen=opener)
        with self.assertRaises(LLMAPIError) as raised:
            client.chat_completion(model="m", messages=[{"role": "user", "content": "x"}])
        message = str(raised.exception)
        self.assertIn("finish_reason=length", message)
        self.assertIn("reasoning=present", message)
        self.assertNotIn("private trace", message)

    def test_request_params_cannot_override_or_smuggle_sensitive_fields(self) -> None:
        client = OpenAIChatClient(
            "https://example.test/v1", "fake", urlopen=QueueOpener()
        )
        base = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
        with self.assertRaisesRegex(ValueError, "不得覆盖"):
            client.chat_completion(**base, request_params={"model": "other"})
        with self.assertRaisesRegex(ValueError, "敏感字段"):
            client.chat_completion(
                **base,
                request_params={"metadata": {"authorization": "not-allowed"}},
            )
        with self.assertRaisesRegex(ValueError, "非流式"):
            client.chat_completion(**base, request_params={"stream": True})

    def test_model_preflight_uses_exact_wire_ids(self) -> None:
        opener = QueueOpener(
            {
                "object": "list",
                "data": [
                    {"id": "model-a", "object": "model"},
                    {"id": "model-b", "object": "model"},
                ],
            }
        )
        client = OpenAIChatClient("https://example.test", "fake", urlopen=opener)
        result = client.preflight_models(["model-a", "model-a", "model-c"])

        request, _timeout = opener.requests[0]
        self.assertEqual(request.full_url, "https://example.test/v1/models")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(result.required, ("model-a", "model-c"))
        self.assertEqual(result.missing, ("model-c",))
        self.assertFalse(result.ok)
        with self.assertRaises(ModelPreflightError) as raised:
            result.require_available()
        self.assertEqual(raised.exception.missing, ("model-c",))

    def test_http_error_does_not_echo_upstream_body(self) -> None:
        def failing_opener(request, *, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                hdrs={"Retry-After": "3.5"},
                fp=BytesIO(b'{"error":{"message":"credential=fake-secret"}}'),
            )

        client = OpenAIChatClient(
            "https://example.test", "fake-secret", urlopen=failing_opener
        )
        with self.assertRaises(LLMAPIError) as raised:
            client.list_models()
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.retry_after_seconds, 3.5)
        self.assertNotIn("fake-secret", str(raised.exception))
        self.assertNotIn("credential", str(raised.exception))

    def test_connection_reset_is_wrapped_as_retryable_safe_error(self) -> None:
        def reset_opener(request, *, timeout):
            raise ConnectionResetError("upstream reset with private transport detail")

        client = OpenAIChatClient("https://example.test", "fake", urlopen=reset_opener)
        with self.assertRaises(LLMAPIError) as raised:
            client.list_models()
        self.assertIsNone(raised.exception.status_code)
        self.assertNotIn("private transport detail", str(raised.exception))

    def test_retry_after_parser_rejects_untrusted_text(self) -> None:
        self.assertEqual(parse_retry_after("2"), 2.0)
        self.assertIsNone(parse_retry_after("not-a-delay"))

    def test_from_env_reports_names_only(self) -> None:
        with self.assertRaises(LLMAPIError) as raised:
            OpenAIChatClient.from_env(environ={})
        self.assertIn("API_URL", str(raised.exception))
        self.assertIn("API_KEY", str(raised.exception))

    def test_config_client_merges_only_explicit_layers(self) -> None:
        opener = QueueOpener(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )
        config = {
            "providers": {
                "new-api": {
                    "base_url_env": "TEST_API_URL",
                    "api_key_env": "TEST_API_KEY",
                    "timeout": 42,
                    "request_defaults": {"temperature": 0.4, "max_tokens": 100},
                }
            }
        }
        client = ChatClient.from_config(
            config,
            {"TEST_API_URL": "https://example.test", "TEST_API_KEY": "fake"},
            urlopen=opener,
        )
        client.complete(
            {
                "id": "local-id",
                "model": "wire-id",
                "request": {"temperature": 0.7, "top_p": 0.9},
                "stages": {"chapter": {"max_tokens": 200}},
            },
            [{"role": "user", "content": "x"}],
            stage="chapter",
            request_overrides={"temperature": 0.8},
        )

        request, timeout = opener.requests[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(timeout, 42)
        self.assertEqual(payload["model"], "wire-id")
        self.assertEqual(payload["temperature"], 0.8)
        self.assertEqual(payload["max_tokens"], 200)
        self.assertEqual(payload["top_p"], 0.9)
        self.assertNotIn("stage", payload)

    def test_empty_generation_layers_send_only_model_and_messages(self) -> None:
        opener = QueueOpener(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )
        config = {
            "providers": {
                "new-api": {
                    "base_url_env": "TEST_API_URL",
                    "api_key_env": "TEST_API_KEY",
                    "request_defaults": {},
                }
            }
        }
        client = ChatClient.from_config(
            config,
            {"TEST_API_URL": "https://example.test", "TEST_API_KEY": "fake"},
            urlopen=opener,
        )
        client.complete(
            {"id": "local-id", "model": "wire-id", "request": {}, "stages": {}},
            [{"role": "user", "content": "x"}],
            stage="chapter",
        )
        payload = json.loads(opener.requests[0][0].data.decode("utf-8"))
        self.assertEqual(set(payload), {"model", "messages"})

    def test_model_registry_resolves_exact_ids_and_declared_aliases(self) -> None:
        config = {
            "models": [
                {"id": "stable-a", "model": "wire-a", "aliases": ["short-a"]},
                {"id": "stable-b", "model": "wire-b"},
            ]
        }
        self.assertEqual(get_model_config(config, "stable-a")["model"], "wire-a")
        self.assertEqual(get_model_config(config, "short-a")["id"], "stable-a")
        with self.assertRaises(ValueError):
            get_model_config(config, "wire")


if __name__ == "__main__":
    unittest.main()
