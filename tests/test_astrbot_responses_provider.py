"""Exercise the installed Responses transport using a network-free SDK client."""

import tests.astrbot_data_root  # noqa: F401

from dataclasses import replace
import inspect
import json
from types import SimpleNamespace as NS
import unittest

from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider.sources.openai_responses_source import (
    ProviderOpenAIResponses,
)
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from openai.resources.responses import AsyncResponses
from openai.types.responses import Response

from companion_v2.adapters.astrbot.model_wire import tool_alias
from companion_v2.adapters.astrbot.provider import AstrBotTextProvider, UsageTariff
from companion_v2.model_gateway.models import Amount, ToolSpec
from tests.native_adapter_support import NativeFixture, final_text


def response_result(
    text=None, *, status="completed", reason=None, calls=(), refusal=None
):
    """Construct a real SDK response without creating a client.

    Args:
        text: Visible output text.
        status: Responses completion status.
        reason: Optional incomplete reason.
        calls: Raw function call dictionaries.
        refusal: Optional refusal content.

    Returns:
        A validated SDK response.
    """
    content = (
        [{"type": "refusal", "refusal": refusal}]
        if refusal is not None
        else [
            {
                "type": "output_text",
                "text": final_text() if text is None else text,
                "annotations": [],
            }
        ]
    )
    return Response.model_validate(
        {
            "id": "response-1",
            "created_at": 1,
            "model": "fixture",
            "object": "response",
            "status": status,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "output": [
                {
                    "type": "message",
                    "id": "message-1",
                    "role": "assistant",
                    "status": "completed",
                    "content": content,
                },
                *calls,
            ],
            "incomplete_details": {"reason": reason} if reason else None,
            "usage": {
                "input_tokens": 30,
                "output_tokens": 10,
                "total_tokens": 40,
                "input_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
    )


class FakeResponsesStream:
    def __init__(self, events):
        self.events, self.closed = events, False

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for event in self.events:
            yield event

    async def close(self):
        self.closed = True


class FakeResponsesClient:
    def __init__(self, state=None, *, max_retries=2):
        self.state = state or NS(
            calls=[], streams=[], result=response_result(), events=None
        )
        self.max_retries = max_retries
        self.base_url = NS(host="fixture.invalid")
        self.responses = NS(create=self.create)

    def with_options(self, *, max_retries):
        return FakeResponsesClient(self.state, max_retries=max_retries)

    async def create(self, **kwargs):
        assert self.max_retries == 0
        self.state.calls.append(kwargs)
        if not kwargs["stream"]:
            return self.state.result
        events = self.state.events
        if events is None:
            events = [
                NS(type="response.output_text.delta", delta=final_text()),
                NS(type="response.completed", response=self.state.result),
            ]
        stream = FakeResponsesStream(events)
        self.state.streams.append(stream)
        return stream


class ResponsesProviderTests(NativeFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        provider = ProviderOpenAIResponses.__new__(ProviderOpenAIResponses)
        provider.provider_config = {
            "id": "native-model",
            "type": "openai_responses",
            "model": "fixture",
            "custom_extra_body": {},
        }
        provider.model_name = "fixture"
        provider.default_params = inspect.signature(
            AsyncResponses.create
        ).parameters.keys()
        provider.client = FakeResponsesClient()
        self.native.provider = provider
        self.state = provider.client.state
        self.native_model = self.responses_model()

    def responses_model(self, **kwargs):
        return AstrBotTextProvider(
            self.native,
            bridge=self.bridge,
            binding=self.providers[0].binding,
            provider_id="native-model",
            version="4.28.2",
            provider_type=ProviderOpenAIOfficial,
            function_tool_type=FunctionTool,
            tool_set_type=ToolSet,
            tariff=UsageTariff(2000000, 1000000, 4000000),
            maximum_output_tokens=100,
            **kwargs,
        )

    async def test_native_responses_text_payload_and_usage_with_base_provider_type(
        self,
    ):
        result = await self.native_model.generate(await self.envelope())
        self.assertEqual(result.status, "generated")
        self.assertEqual(result.draft.text, "我在这里。")
        self.assertEqual(
            result.usage,
            Amount(40, 90, input_tokens=30, cached_input_tokens=10, output_tokens=10),
        )
        self.assertEqual(len(self.state.calls), 1)
        payload = self.state.calls[0]
        self.assertIn("input", payload)
        self.assertEqual(payload["max_output_tokens"], 100)
        self.assertFalse(payload["store"])
        self.assertFalse(payload["stream"])
        self.assertNotIn("messages", payload)
        self.assertNotIn("max_tokens", payload["extra_body"])
        self.assertEqual(self.native.provider.client.max_retries, 2)

    async def test_background_json_modes_use_responses_text_format(self):
        for mode in ("text", "json_object", "json_schema"):
            with self.subTest(mode=mode):
                envelope = await self.envelope()
                envelope = replace(
                    envelope,
                    original=replace(
                        envelope.original,
                        output_format="memory_reflection",
                        response_format=mode,
                        response_schema_json='{"type":"object","properties":{},"additionalProperties":false}',
                    ),
                )
                self.state.result = response_result("{unfinished")
                result = await self.native_model.generate(envelope)
                self.assertEqual(result.captured_output.text, "{unfinished")
                self.assertEqual(result.captured_output.finish_reason, "stop")
                payload = self.state.calls[-1]
                self.assertNotIn("response_format", payload)
                if mode == "text":
                    self.assertNotIn("text", payload)
                else:
                    self.assertEqual(payload["text"]["format"]["type"], mode)
                    if mode == "json_schema":
                        self.assertEqual(
                            payload["text"]["format"]["name"], "memory_reflection"
                        )
                        self.assertTrue(payload["text"]["format"]["strict"])
                        self.assertIn("schema", payload["text"]["format"])

    async def test_role_state_background_response_uses_native_parser(self):
        envelope = await self.envelope()
        envelope = replace(
            envelope, original=replace(envelope.original, output_format="role_state")
        )
        self.state.result = response_result('{"energy":50}')
        result = await self.native_model.generate(envelope)
        self.assertEqual(result.status, "generated")
        self.assertEqual(result.draft.text, '{"energy":50}')

    async def test_tool_arguments_are_not_repaired_into_valid_operations(self):
        for arguments, expected in (("{}", "generated"), ("{broken", "unknown")):
            with self.subTest(arguments=arguments):
                self.state.result = response_result(
                    "",
                    calls=[
                        {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": tool_alias("memo.list"),
                            "arguments": arguments,
                            "status": "completed",
                            "id": "function-1",
                        }
                    ],
                )
                envelope = await self.envelope(
                    tools=(ToolSpec("memo.list", "List memos", '{"type":"object"}'),)
                )
                result = await self.native_model.generate(envelope)
                self.assertEqual(result.status, expected)
                if expected == "generated":
                    self.assertEqual(result.tool_calls[0].action, "memo.list")
                    self.assertEqual(result.tool_calls[0].call_id, "call-1")
                else:
                    self.assertFalse(result.tool_calls)
                self.assertEqual(self.state.calls[-1]["tools"][0]["type"], "function")

    async def test_truncation_failure_and_refusal_preserve_usage(self):
        cases = (
            ("incomplete", "max_output_tokens", None, "unknown"),
            ("failed", None, None, "unknown"),
            ("completed", None, "Refused", "generated"),
            ("incomplete", "content_filter", None, "generated"),
        )
        for status, reason, refusal, expected in cases:
            with self.subTest(status=status, reason=reason, refusal=refusal):
                self.state.result = response_result(
                    status=status, reason=reason, refusal=refusal
                )
                result = await self.native_model.generate(await self.envelope())
                self.assertEqual(result.status, expected)
                self.assertEqual(result.usage.tokens, 40)
                if expected == "generated":
                    self.assertEqual(result.captured_output.finish_reason, "refusal")

    async def test_reflection_keeps_truncation_status(self):
        self.state.result = response_result(
            "{partial", status="incomplete", reason="max_output_tokens"
        )
        envelope = await self.envelope()
        envelope = replace(
            envelope,
            original=replace(envelope.original, output_format="memory_reflection"),
        )
        result = await self.native_model.generate(envelope)
        self.assertEqual(result.captured_output.finish_reason, "length")
        self.assertEqual(result.captured_output.text, "{partial")

    async def test_native_responses_stream_completes_and_closes_once(self):
        model = self.responses_model(streaming=True)
        result = await model.generate(await self.envelope())
        self.assertEqual(result.status, "generated")
        self.assertEqual(result.draft.text, "我在这里。")
        self.assertEqual(result.usage.tokens, 40)
        self.assertEqual(len(self.state.calls), 1)
        self.assertTrue(self.state.calls[0]["stream"])
        self.assertTrue(self.state.streams[0].closed)

    async def test_missing_or_changed_stream_terminal_is_not_a_valid_reply(self):
        for terminal in (
            [],
            [NS(type="response.completed", response=self.state.result)],
        ):
            with self.subTest(terminal=bool(terminal)):
                self.state.events = [
                    NS(type="response.output_text.delta", delta="changed"),
                    *terminal,
                ]
                result = await self.responses_model(streaming=True).generate(
                    await self.envelope()
                )
                self.assertEqual(result.status, "unknown")
                self.assertIsNone(result.draft)
                self.assertTrue(self.state.streams[-1].closed)

    async def test_probe_uses_native_responses_input_without_retries(self):
        await self.native_model.probe()
        self.assertEqual(len(self.state.calls), 1)
        self.assertEqual(
            self.state.calls[0]["input"],
            [{"type": "message", "role": "user", "content": "Reply OK."}],
        )
        self.assertEqual(self.state.calls[0]["max_output_tokens"], 8)

    async def test_incremental_reply_prefix_reaches_existing_callback(self):
        text = "Hello."
        body = json.dumps(
            {
                "action": "text",
                "claims": [
                    {
                        "id": "c1",
                        "kind": "subjective",
                        "statement": text,
                        "evidence": None,
                    }
                ],
                "text": text,
            }
        )
        self.state.result = response_result(body)
        self.state.events = [
            NS(type="response.output_text.delta", delta=body),
            NS(type="response.completed", response=self.state.result),
        ]
        prefixes = []

        async def receive_prefix(draft):
            self.assertFalse(self.state.streams[0].closed)
            prefixes.append(draft.text)

        envelope = await self.envelope(on_reply_prefix=receive_prefix)
        envelope = replace(
            envelope, original=replace(envelope.original, streaming_mode="incremental")
        )
        result = await self.responses_model(streaming=True).generate(envelope)
        self.assertEqual(result.status, "generated")
        self.assertEqual(prefixes, [text])
        self.assertEqual(result.draft.text, text)

    async def test_stream_tools_use_terminal_arguments_and_close(self):
        self.state.result = response_result(
            "",
            calls=[
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": tool_alias("memo.list"),
                    "arguments": '{"limit":2}',
                    "status": "completed",
                    "id": "function-1",
                }
            ],
        )
        self.state.events = [
            NS(type="response.function_call_arguments.delta", delta='{"limit":2}'),
            NS(type="response.completed", response=self.state.result),
        ]
        envelope = await self.envelope(
            tools=(ToolSpec("memo.list", "List memos", '{"type":"object"}'),)
        )
        result = await self.responses_model(streaming=True).generate(envelope)
        self.assertEqual(result.status, "generated")
        self.assertEqual(result.tool_calls[0].arguments_json, '{"limit":2}')
        self.assertTrue(self.state.streams[0].closed)

    def test_responses_extra_parameters_cannot_replace_owned_request_fields(self):
        for field in (
            "input",
            "text",
            "instructions",
            "previous_response_id",
            "conversation",
            "store",
        ):
            with self.subTest(field=field):
                self.native.provider.provider_config["custom_extra_body"] = {
                    field: "override"
                }
                with self.assertRaises(ValueError):
                    self.responses_model()


if __name__ == "__main__":
    unittest.main()
