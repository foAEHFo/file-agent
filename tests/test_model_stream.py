from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest


class Dumpable:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return self.value


class FakeResponsesAPI:
    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> AsyncIterator[Any]:
        self.requests.append(request)

        async def stream() -> AsyncIterator[Any]:
            for event in self.events:
                yield event

        return stream()


def test_responses_client_streams_typed_events_and_preserves_output_items() -> None:
    from file_agent.model import ModelEventKind, OpenAIResponsesClient

    async def scenario() -> tuple[list[Any], FakeResponsesAPI]:
        reasoning_item = Dumpable(
            {
                "id": "rs_1",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Inspecting files"}],
                "encrypted_content": "encrypted-reasoning",
            }
        )
        message_item = Dumpable(
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done."}],
            }
        )
        usage = Dumpable(
            {
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
                "output_tokens_details": {"reasoning_tokens": 3},
            }
        )
        api = FakeResponsesAPI(
            [
                SimpleNamespace(
                    type="response.reasoning_summary_text.delta",
                    delta="Inspecting",
                ),
                SimpleNamespace(type="response.output_text.delta", delta="Done."),
                SimpleNamespace(type="response.output_item.done", item=reasoning_item),
                SimpleNamespace(type="response.output_item.done", item=message_item),
                SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(usage=usage),
                ),
            ]
        )
        client = OpenAIResponsesClient(
            responses_api=api,
            model="gpt-test",
            instructions="fixed instructions",
            tools=[{"type": "function", "name": "read_file", "parameters": {}}],
        )
        events = [
            event
            async for event in client.stream(
                [{"role": "user", "content": "Inspect the workspace"}]
            )
        ]
        return events, api

    events, api = asyncio.run(scenario())

    assert [event.kind for event in events] == [
        ModelEventKind.REASONING_DELTA,
        ModelEventKind.ANSWER_DELTA,
        ModelEventKind.OUTPUT_ITEM,
        ModelEventKind.OUTPUT_ITEM,
        ModelEventKind.USAGE,
        ModelEventKind.COMPLETED,
    ]
    assert events[2].item == {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "Inspecting files"}],
        "encrypted_content": "encrypted-reasoning",
    }
    assert events[2].item["encrypted_content"] == "encrypted-reasoning"
    assert events[4].usage == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "output_tokens_details": {"reasoning_tokens": 3},
    }
    assert api.requests == [
        {
            "model": "gpt-test",
            "instructions": "fixed instructions",
            "input": [{"role": "user", "content": "Inspect the workspace"}],
            "tools": [{"type": "function", "name": "read_file", "parameters": {}}],
            "store": False,
            "stream": True,
            "reasoning": {"summary": "auto"},
            "include": ["reasoning.encrypted_content"],
        }
    ]


def test_responses_client_rejects_an_incomplete_stream() -> None:
    from file_agent.model import ModelStreamError, OpenAIResponsesClient

    async def scenario() -> None:
        client = OpenAIResponsesClient(
            responses_api=FakeResponsesAPI(
                [SimpleNamespace(type="response.output_text.delta", delta="partial")]
            ),
            model="gpt-test",
            instructions="fixed",
            tools=[],
        )
        async for _ in client.stream([{"role": "user", "content": "test"}]):
            pass

    with pytest.raises(ModelStreamError, match="before response.completed"):
        asyncio.run(scenario())


def test_tool_schemas_are_strict_and_prompts_are_chinese_and_safe() -> None:
    from file_agent.prompts import SYSTEM_INSTRUCTIONS, TOOL_DEFINITIONS

    assert {tool["name"] for tool in TOOL_DEFINITIONS} == {
        "list_directory",
        "search_files",
        "read_file",
        "make_directory",
        "write_file",
        "move_file",
        "get_workspace_changes",
    }
    for tool in TOOL_DEFINITIONS:
        schema = tool["parameters"]
        assert tool["strict"] is True
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert "title" not in schema
        assert all("title" not in field for field in schema["properties"].values())
        assert any(
            "\u4e00" <= character <= "\u9fff" for character in tool["description"]
        )
    assert "不可信数据" in SYSTEM_INSTRUCTIONS
    assert "先搜索" in SYSTEM_INSTRUCTIONS
    assert "正文中的日期" in SYSTEM_INSTRUCTIONS
    assert "expected_sha256" in SYSTEM_INSTRUCTIONS
    assert "Project Falcon" not in SYSTEM_INSTRUCTIONS
