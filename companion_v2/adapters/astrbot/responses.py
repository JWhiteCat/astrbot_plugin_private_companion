"""Preserve Responses API completion evidence at the native adapter boundary."""

from types import SimpleNamespace

from .memory_output import explicit_refusal
from .streaming import StreamTranscript


def completion_evidence(response):
    """Project a Responses result onto the existing strict completion contract.

    Args:
        response: The original SDK Responses result, before native parsing.

    Returns:
        Completion evidence retaining raw tool arguments, status, and usage.
    """

    def read(value, name, default=None):
        return (
            value.get(name, default)
            if isinstance(value, dict)
            else getattr(value, name, default)
        )

    text, refusals, calls = [], [], []
    invalid = False
    output = read(response, "output", ())
    if not isinstance(output, (list, tuple)):
        output, invalid = (), True
    for item in output:
        kind = read(item, "type")
        if read(item, "status") not in (None, "completed"):
            invalid = True
        if kind == "message":
            content = read(item, "content", ())
            if not isinstance(content, (list, tuple)):
                invalid = True
                continue
            for part in content:
                part_type = read(part, "type")
                value = read(part, "text" if part_type == "output_text" else "refusal")
                if part_type not in ("output_text", "refusal") or not isinstance(
                    value, str
                ):
                    invalid = True
                    continue
                text.append(value)
                if part_type == "refusal":
                    refusals.append(value)
        elif kind == "function_call":
            calls.append(
                SimpleNamespace(
                    id=read(item, "call_id"),
                    type="function",
                    function=SimpleNamespace(
                        name=read(item, "name"), arguments=read(item, "arguments")
                    ),
                )
            )
        elif kind != "reasoning":
            invalid = True
    status = read(response, "status")
    reason = read(read(response, "incomplete_details"), "reason")
    finish = "invalid"
    if status == "completed" and not invalid:
        finish = "tool_calls" if calls else "stop"
    elif status == "incomplete" and reason == "max_output_tokens":
        finish = "length"
    elif status == "incomplete" and reason == "content_filter":
        finish = "content_filter"
    return SimpleNamespace(
        id=read(response, "id"),
        usage=read(response, "usage"),
        choices=[
            SimpleNamespace(
                finish_reason=finish,
                message=SimpleNamespace(
                    content="".join(text),
                    tool_calls=calls,
                    refusal="".join(refusals) or None,
                ),
            )
        ],
    )


class ResponsesTranscript(StreamTranscript):
    """Audit native Responses streaming without using the Chat accumulator."""

    def __init__(self, client, on_content=None):
        super().__init__(client, on_content)
        self.terminal = None
        self.text_seen = False

    async def create(self, **kwargs):
        """Wrap one Responses stream and retain its authoritative terminal event.

        Args:
            **kwargs: The native SDK request parameters.

        Returns:
            An async iterator forwarding the original SDK events.

        Raises:
            ValueError: If the stream changes identity or exceeds its bounds.
        """
        if self.stream is not None or kwargs.get("stream") is not True:
            raise ValueError("stream transport must make exactly one streaming request")
        self.stream = await self.client.responses.create(**kwargs)

        async def events():
            async for event in self.stream:
                self.chunks += 1
                if self.chunks > 262144 or self.ended:
                    raise ValueError(
                        "Responses stream exceeded its completion boundary"
                    )
                kind = getattr(event, "type", "")
                response = getattr(event, "response", None)
                if response is not None:
                    identifier = getattr(response, "id", None)
                    if not isinstance(identifier, str) or not identifier:
                        raise ValueError("Responses stream has no response identity")
                    if self.response_id is not None and identifier != self.response_id:
                        raise ValueError("Responses stream changed response identity")
                    self.response_id = identifier
                fragment = self._fragment(getattr(event, "delta", None))
                if kind == "response.output_text.delta":
                    self.text_seen = True
                    self.content.append(fragment)
                    if (
                        self.on_content is not None
                        and not self.calls
                        and not self.refusal
                    ):
                        await self.on_content(fragment)
                elif kind == "response.refusal.delta":
                    self.refusal = self.refusal or bool(fragment)
                elif kind == "response.function_call_arguments.delta" or (
                    kind == "response.output_item.added"
                    and getattr(getattr(event, "item", None), "type", None)
                    == "function_call"
                ):
                    self.calls[0] = True
                if kind in (
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                ):
                    if response is None or getattr(
                        response, "status", None
                    ) != kind.removeprefix("response."):
                        raise ValueError(
                            "Responses stream has an inconsistent terminal event"
                        )
                    self.terminal = response
                    self.ended = True
                    self.usage_receipt = response
                    evidence = completion_evidence(response)
                    self.finish = evidence.choices[0].finish_reason
                    self.refusal = self.refusal or explicit_refusal(evidence)
                yield event

        return events()

    def client_view(self):
        """Return a client view that intercepts only the Responses endpoint.

        Returns:
            A native-compatible client forwarding all other attributes.
        """
        transcript = self

        class ClientView:
            responses = SimpleNamespace(create=transcript.create)

            def __getattr__(self, name):
                return getattr(transcript.client, name)

        return ClientView()

    def verify(self, raw):
        """Verify that native parsing consumes the exact terminal response.

        Args:
            raw: The response passed to the native parser.

        Raises:
            ValueError: If the terminal response is missing or changes text.
        """
        if not self.ended or raw is not self.terminal:
            raise ValueError("Responses stream never completed")
        evidence = completion_evidence(raw)
        if (
            self.text_seen
            and not self.refusal
            and evidence.choices[0].message.content != "".join(self.content)
        ):
            raise ValueError("Responses terminal text differs from streamed content")
