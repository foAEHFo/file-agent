"""Streaming OpenAI Responses transport and internal model events."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast

from openai import AsyncOpenAI

JsonObject = dict[str, Any]


class ModelEventKind(StrEnum):
    REASONING_DELTA = "reasoning.delta"
    ANSWER_DELTA = "answer.delta"
    OUTPUT_ITEM = "output.item"
    USAGE = "usage"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ModelEvent:
    kind: ModelEventKind
    delta: str | None = None
    item: JsonObject | None = None
    usage: JsonObject | None = None


class ModelStreamError(RuntimeError):
    """Raised when a streamed response does not complete safely."""


class ResponsesClient(Protocol):
    def stream(
        self, input_items: Sequence[Mapping[str, Any]]
    ) -> AsyncIterator[ModelEvent]: ...


class ResponsesAPI(Protocol):
    async def create(self, **request: Any) -> AsyncIterator[Any]: ...


class OpenAIResponsesClient:
    """Adapt the official async SDK stream to stable internal events."""

    def __init__(
        self,
        *,
        model: str,
        instructions: str,
        tools: Sequence[Mapping[str, Any]],
        responses_api: ResponsesAPI | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if responses_api is None:
            if not api_key:
                raise ValueError("api_key is required when responses_api is omitted")
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                max_retries=0,
            )
            responses_api = cast(ResponsesAPI, client.responses)
        self._responses_api = responses_api
        self._model = model
        self._instructions = instructions
        self._tools = [dict(tool) for tool in tools]

    async def stream(
        self, input_items: Sequence[Mapping[str, Any]]
    ) -> AsyncIterator[ModelEvent]:
        raw_stream = await self._responses_api.create(
            model=self._model,
            instructions=self._instructions,
            input=[dict(item) for item in input_items],
            tools=self._tools,
            store=False,
            stream=True,
            reasoning={"summary": "auto"},
            include=["reasoning.encrypted_content"],
        )
        completed = False
        try:
            async for raw_event in raw_stream:
                event_type = getattr(raw_event, "type", "")
                if event_type == "response.reasoning_summary_text.delta":
                    yield ModelEvent(
                        kind=ModelEventKind.REASONING_DELTA,
                        delta=str(raw_event.delta),
                    )
                elif event_type == "response.output_text.delta":
                    yield ModelEvent(
                        kind=ModelEventKind.ANSWER_DELTA,
                        delta=str(raw_event.delta),
                    )
                elif event_type == "response.output_item.done":
                    yield ModelEvent(
                        kind=ModelEventKind.OUTPUT_ITEM,
                        item=_model_dump(raw_event.item),
                    )
                elif event_type == "response.completed":
                    usage = getattr(raw_event.response, "usage", None)
                    if usage is not None:
                        yield ModelEvent(
                            kind=ModelEventKind.USAGE,
                            usage=_model_dump(usage),
                        )
                    completed = True
                    yield ModelEvent(kind=ModelEventKind.COMPLETED)
                    return
                elif event_type in {
                    "error",
                    "response.failed",
                    "response.incomplete",
                }:
                    raise ModelStreamError(f"Responses stream ended with {event_type}")
        except ModelStreamError:
            raise
        except Exception as error:
            raise ModelStreamError("Responses stream was interrupted") from error

        if not completed:
            raise ModelStreamError("Responses stream ended before response.completed")


def _model_dump(value: Any) -> JsonObject:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        raise ModelStreamError("Responses event payload is not serializable")
    dumped = model_dump(mode="json", exclude_none=True)
    if not isinstance(dumped, dict):
        raise ModelStreamError("Responses event payload is not an object")
    return cast(JsonObject, dumped)
