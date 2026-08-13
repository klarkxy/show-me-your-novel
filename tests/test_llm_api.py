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
    ANTHROPIC_MESSAGES,
    OPENAI_CHAT_COMPLETIONS,
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


class FakeStreamResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [f"{line}\n".encode("utf-8") for line in lines]

    def __enter__(self) -> "FakeStreamResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def __iter__(self):
        return iter(self._lines)


class QueueOpener:
    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        if not self.responses:
            raise AssertionError("unexpected request")
        return FakeResponse(self.responses.pop(0))


class QueueStreamOpener:
    def __init__(self, *responses: list[str]) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        if not self.responses:
            raise AssertionError("unexpected request")
        return FakeStreamResponse(self.responses.pop(0))


def openai_response(
    content: str,
    *,
    finish_reason: str | None = "stop",
    response_id: str = "chatcmpl-test",
    model: str = "openai-wire-model",
) -> dict:
    return {
        "id": response_id,
        "model": model,
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        },
    }


def anthropic_response(
    *,
    stop_reason: str | None = "end_turn",
    content: list[dict] | None = None,
    response_id: str = "msg-test",
    model: str = "anthropic-wire-model",
) -> dict:
    return {
        "id": response_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": (
            [{"type": "text", "text": "正文"}]
            if content is None
            else content
        ),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 11,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 2,
            "output_tokens": 7,
        },
    }


def provider_config() -> dict:
    return {
        "providers": {
            "new-api": {
                "base_url_env": "TEST_API_URL",
                "api_key_env": "TEST_API_KEY",
                "timeout": 42,
                "request_defaults": {},
            }
        }
    }


def model_config(
    model: str,
    *,
    protocol: str | None = None,
    protocol_required: dict | None = None,
    request: dict | None = None,
) -> dict:
    result = {
        "id": model,
        "model": model,
        "provider": "new-api",
        "request": dict(request or {}),
        "stages": {},
    }
    if protocol is not None:
        result["protocol"] = protocol
    if protocol_required is not None:
        result["protocol_required"] = dict(protocol_required)
    return result


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
        self.assertEqual(result.native_finish_reason, "stop")
        self.assertEqual(result.protocol, OPENAI_CHAT_COMPLETIONS)
        self.assertEqual(result.endpoint_path, "/v1/chat/completions")
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

    def test_anthropic_payload_and_response_are_normalized(self) -> None:
        response = anthropic_response(
            content=[
                {"type": "thinking", "thinking": "私有思考", "signature": "sig"},
                {"type": "text", "text": "第一段"},
                {"type": "redacted_thinking", "data": "sealed-private-data"},
                {"type": "text", "text": "第二段"},
            ],
            model="server-anthropic-model",
        )
        opener = QueueOpener(response)
        client = ChatClient.from_config(
            provider_config(),
            {
                "TEST_API_URL": "https://example.test/gateway",
                "TEST_API_KEY": "fake-anthropic-key",
            },
            urlopen=opener,
        )
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "先前回答"},
            {"role": "user", "content": "继续写"},
        ]
        original_messages = json.loads(json.dumps(messages, ensure_ascii=False))

        result = client.complete(
            model_config(
                "requested-anthropic-model",
                protocol=ANTHROPIC_MESSAGES,
                protocol_required={"max_tokens": 65_536},
            ),
            messages,
            stage="chapter",
        )

        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "https://example.test/gateway/v1/messages")
        self.assertEqual(timeout, 42)
        self.assertEqual(request.get_header("X-api-key"), "fake-anthropic-key")
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
        self.assertIsNone(request.get_header("Authorization"))
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            payload,
            {
                "model": "requested-anthropic-model",
                "max_tokens": 65_536,
                "system": "系统提示",
                "messages": [
                    {"role": "user", "content": "第一问"},
                    {"role": "assistant", "content": "先前回答"},
                    {"role": "user", "content": "继续写"},
                ],
            },
        )
        self.assertNotIn("stream", payload)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("thinking", payload)
        self.assertEqual(messages, original_messages)

        self.assertEqual(result.content, "第一段第二段")
        self.assertEqual(result.reasoning_content, "私有思考")
        self.assertNotIn("sealed-private-data", result.content)
        self.assertEqual(result.requested_model, "requested-anthropic-model")
        self.assertEqual(result.response_model, "server-anthropic-model")
        self.assertEqual(result.response_id, "msg-test")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.native_finish_reason, "end_turn")
        self.assertEqual(result.protocol, ANTHROPIC_MESSAGES)
        self.assertEqual(result.endpoint_path, "/v1/messages")
        self.assertEqual(result.usage["prompt_tokens"], 16)
        self.assertEqual(result.usage["completion_tokens"], 7)
        self.assertEqual(result.usage["total_tokens"], 23)
        self.assertEqual(result.usage["input_tokens"], 11)
        self.assertEqual(result.usage["cache_creation_input_tokens"], 3)
        self.assertEqual(result.usage["cache_read_input_tokens"], 2)
        self.assertEqual(result.usage["output_tokens"], 7)
        self.assertEqual(result.raw_response, response)

    def test_anthropic_native_structured_output_is_forwarded(self) -> None:
        opener = QueueOpener(anthropic_response(content=[{"type": "text", "text": "{\"ok\":true}"}]))
        client = OpenAIChatClient(
            "https://example.test/v1", "fake", urlopen=opener
        )
        output_config = {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            }
        }

        result = client.anthropic_message(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "return json"}],
            request_params={"max_tokens": 1024, "output_config": output_config},
        )

        payload = json.loads(opener.requests[0][0].data.decode("utf-8"))
        self.assertEqual(payload["output_config"], output_config)
        self.assertEqual(result.content, '{"ok":true}')

    def test_anthropic_stop_reasons_are_normalized_without_accepting_unknowns(
        self,
    ) -> None:
        cases = (
            ("end_turn", "stop"),
            ("stop_sequence", "stop"),
            ("max_tokens", "length"),
            ("tool_use", "tool_use"),
            ("pause_turn", "pause_turn"),
            ("refusal", "refusal"),
            (None, None),
        )
        for native_reason, expected_finish_reason in cases:
            with self.subTest(native_reason=native_reason):
                opener = QueueOpener(
                    anthropic_response(stop_reason=native_reason)
                )
                client = ChatClient.from_config(
                    provider_config(),
                    {
                        "TEST_API_URL": "https://example.test",
                        "TEST_API_KEY": "fake",
                    },
                    urlopen=opener,
                )
                result = client.complete(
                    model_config(
                        "a-model",
                        protocol=ANTHROPIC_MESSAGES,
                        protocol_required={"max_tokens": 32_768},
                    ),
                    [{"role": "user", "content": "x"}],
                    stage="chapter",
                )
                self.assertEqual(result.finish_reason, expected_finish_reason)
                self.assertEqual(result.native_finish_reason, native_reason)
                self.assertEqual(result.protocol, ANTHROPIC_MESSAGES)
                self.assertEqual(result.endpoint_path, "/v1/messages")

    def test_anthropic_protocol_required_is_strict_and_not_overridable(
        self,
    ) -> None:
        client = ChatClient.from_config(
            provider_config(),
            {
                "TEST_API_URL": "https://example.test",
                "TEST_API_KEY": "fake",
            },
            urlopen=QueueOpener(),
        )
        base = model_config("a-model", protocol=ANTHROPIC_MESSAGES)
        invalid_required = (
            None,
            {},
            {"max_tokens": True},
            {"max_tokens": 0},
            {"max_tokens": -1},
            {"max_tokens": "65536"},
            {"max_tokens": 65_536, "temperature": 0.2},
        )
        for required in invalid_required:
            with self.subTest(protocol_required=required):
                configured = dict(base)
                if required is not None:
                    configured["protocol_required"] = required
                with self.assertRaisesRegex(ValueError, "protocol_required|max_tokens"):
                    client.complete(
                        configured,
                        [{"role": "user", "content": "x"}],
                        stage="chapter",
                    )

        for forbidden_request in (
            {"max_tokens": 8_192},
            {"stream": True},
        ):
            with self.subTest(forbidden_request=forbidden_request):
                configured = model_config(
                    "a-model",
                    protocol=ANTHROPIC_MESSAGES,
                    protocol_required={"max_tokens": 65_536},
                    request=forbidden_request,
                )
                with self.assertRaisesRegex(ValueError, "max_tokens|stream|非流式"):
                    client.complete(
                        configured,
                        [{"role": "user", "content": "x"}],
                        stage="chapter",
                    )

    def test_protocol_routing_is_per_request_and_not_sticky(self) -> None:
        opener = QueueOpener(
            openai_response("O-1", response_id="o-1"),
            anthropic_response(
                content=[{"type": "text", "text": "A"}],
                response_id="a-1",
            ),
            openai_response("O-2", response_id="o-2"),
        )
        client = ChatClient.from_config(
            provider_config(),
            {
                "TEST_API_URL": "https://example.test",
                "TEST_API_KEY": "fake",
            },
            urlopen=opener,
        )
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "x"},
        ]

        first = client.complete(
            model_config("o-default"),
            messages,
            stage="chapter",
        )
        second = client.complete(
            model_config(
                "a-explicit",
                protocol=ANTHROPIC_MESSAGES,
                protocol_required={"max_tokens": 65_536},
            ),
            messages,
            stage="chapter",
        )
        third = client.complete(
            model_config("o-explicit", protocol=OPENAI_CHAT_COMPLETIONS),
            messages,
            stage="chapter",
        )

        self.assertEqual(
            [request.full_url for request, _timeout in opener.requests],
            [
                "https://example.test/v1/chat/completions",
                "https://example.test/v1/messages",
                "https://example.test/v1/chat/completions",
            ],
        )
        self.assertEqual(
            [first.protocol, second.protocol, third.protocol],
            [
                OPENAI_CHAT_COMPLETIONS,
                ANTHROPIC_MESSAGES,
                OPENAI_CHAT_COMPLETIONS,
            ],
        )
        self.assertEqual(
            [first.endpoint_path, second.endpoint_path, third.endpoint_path],
            [
                "/v1/chat/completions",
                "/v1/messages",
                "/v1/chat/completions",
            ],
        )
        first_payload, second_payload, third_payload = (
            json.loads(request.data.decode("utf-8"))
            for request, _timeout in opener.requests
        )
        self.assertEqual(set(first_payload), {"model", "messages"})
        self.assertEqual(
            set(second_payload), {"model", "messages", "system", "max_tokens"}
        )
        self.assertEqual(set(third_payload), {"model", "messages"})

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
        with self.assertRaisesRegex(ValueError, "传输选项"):
            client.chat_completion(**base, request_params={"stream": True})

    def test_openai_sse_stream_is_reconstructed_and_requires_terminal_event(self) -> None:
        opener = QueueStreamOpener(
            [
                'data: {"id":"chat-stream","model":"wire","choices":[{"delta":{"reasoning_content":"思考"},"finish_reason":null}]}',
                "",
                'data: {"id":"chat-stream","model":"wire","choices":[{"delta":{"content":"正文"},"finish_reason":null}]}',
                "",
                'data: {"id":"chat-stream","model":"wire","choices":[{"delta":{"content":"完成"},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}',
                "",
                "data: [DONE]",
                "",
            ],
            [
                'data: {"id":"truncated","choices":[{"delta":{"content":"半截"},"finish_reason":null}]}',
                "",
            ],
        )
        client = OpenAIChatClient(
            "https://example.test/v1", "fake", stream=True, urlopen=opener
        )
        result = client.chat_completion(
            model="m", messages=[{"role": "user", "content": "x"}]
        )
        payload = json.loads(opener.requests[0][0].data.decode("utf-8"))
        self.assertIs(payload["stream"], True)
        self.assertEqual(result.content, "正文完成")
        self.assertEqual(result.reasoning_content, "思考")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.usage["total_tokens"], 5)
        self.assertTrue(result.raw_response["_stream"]["terminal"])
        with self.assertRaisesRegex(LLMAPIError, "终止事件前中断"):
            client.chat_completion(
                model="m", messages=[{"role": "user", "content": "x"}]
            )

    def test_anthropic_native_sse_stream_is_reconstructed(self) -> None:
        def event(value: dict) -> str:
            return "data: " + json.dumps(value, ensure_ascii=False)

        opener = QueueStreamOpener(
            [
                event({"type": "message_start", "message": {"id": "msg-stream", "model": "claude-wire", "usage": {"input_tokens": 4}}}),
                "",
                event({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "私有"}}),
                "",
                event({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "思考"}}),
                "",
                event({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": "正"}}),
                "",
                event({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "文"}}),
                "",
                event({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
                "",
                event({"type": "message_stop"}),
                "",
            ],
            [
                event({"type": "message_start", "message": {"id": "msg-cut", "model": "claude-wire", "usage": {"input_tokens": 1}}}),
                "",
                event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "半截"}}),
                "",
                event({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
                "",
            ],
        )
        client = OpenAIChatClient(
            "https://example.test/v1", "fake", stream=True, urlopen=opener
        )
        result = client.anthropic_message(
            model="claude",
            messages=[{"role": "user", "content": "x"}],
            request_params={"max_tokens": 128},
        )
        payload = json.loads(opener.requests[0][0].data.decode("utf-8"))
        self.assertIs(payload["stream"], True)
        self.assertEqual(result.content, "正文")
        self.assertEqual(result.reasoning_content, "私有思考")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.usage["total_tokens"], 6)
        self.assertTrue(result.raw_response["_stream"]["terminal"])
        with self.assertRaisesRegex(LLMAPIError, "终止事件前中断"):
            client.anthropic_message(
                model="claude",
                messages=[{"role": "user", "content": "x"}],
                request_params={"max_tokens": 128},
            )

    def test_anthropic_forced_tool_input_becomes_streamed_json_content(self) -> None:
        def event(value: dict) -> str:
            return "data: " + json.dumps(value, ensure_ascii=False)

        opener = QueueStreamOpener(
            [
                event({"type": "message_start", "message": {"id": "msg-tool", "model": "claude-opus-5", "usage": {"input_tokens": 3}}}),
                "",
                event({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tool-1", "name": "submit_result", "input": {}}}),
                "",
                event({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"dimensions\":\"{\\\"ok\\\":"}}),
                "",
                event({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "true}\"}"}}),
                "",
                event({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 2}}),
                "",
                event({"type": "message_stop"}),
                "",
            ]
        )
        client = OpenAIChatClient(
            "https://example.test/v1", "fake", stream=True, urlopen=opener
        )
        result = client.anthropic_message(
            model="claude-opus-5",
            messages=[{"role": "user", "content": "submit"}],
            request_params={
                "max_tokens": 128,
                "tools": [
                    {
                        "name": "submit_result",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "dimensions": {
                                    "type": "object",
                                    "properties": {"ok": {"type": "boolean"}},
                                }
                            },
                        },
                    }
                ],
                "tool_choice": {"type": "tool", "name": "submit_result"},
            },
        )
        self.assertEqual(json.loads(result.content), {"dimensions": {"ok": True}})
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.native_finish_reason, "tool_use")
        self.assertEqual(
            result.raw_response["content"][0]["input"],
            {"dimensions": '{"ok":true}'},
        )

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
