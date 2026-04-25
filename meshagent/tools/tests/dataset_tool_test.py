from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from meshagent.api.messaging import EmptyContent, JsonContent
from meshagent.api.room_server_client import (
    DateDataType,
    IntDataType,
    TextDataType,
    TimestampDataType,
)
from meshagent.tools import ToolContext
from meshagent.tools.dataset import DatasetToolkit, make_dataset_toolkit


class _FakeDatasetsClient:
    def __init__(self) -> None:
        self.insert_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.inspect_calls: list[dict] = []

    async def insert(self, *, table: str, records: list[dict], namespace=None) -> None:
        self.insert_calls.append(
            {
                "table": table,
                "records": records,
                "namespace": namespace,
            }
        )

    async def search(self, *, table: str, where=None, namespace=None, **kwargs):
        self.search_calls.append(
            {
                "table": table,
                "where": where,
                "namespace": namespace,
                **kwargs,
            }
        )
        return [{"id": 1, "name": "Alice"}]

    async def inspect(self, *, table: str, namespace=None):
        self.inspect_calls.append({"table": table, "namespace": namespace})
        return {
            "id": IntDataType(),
            "name": TextDataType(),
        }


class _FakeRoom:
    def __init__(self) -> None:
        self.datasets = _FakeDatasetsClient()


def _tool_context(room: _FakeRoom) -> ToolContext:
    del room
    return ToolContext(caller=object())


@pytest.mark.asyncio
async def test_dataset_toolkit_insert_rows_uses_room_dataset_insert() -> None:
    room = _FakeRoom()
    toolkit = DatasetToolkit(
        tables={
            "users": {
                "id": IntDataType(),
                "name": TextDataType(),
            }
        },
        namespace=["prod"],
        room=room,
    )

    result = await toolkit.execute(
        context=_tool_context(room),
        name="insert_users_rows",
        input=JsonContent(json={"rows": [{"id": 1, "name": "Alice"}]}),
    )

    assert isinstance(result, EmptyContent)
    assert room.datasets.insert_calls == [
        {
            "table": "users",
            "records": [{"id": 1, "name": "Alice"}],
            "namespace": ["prod"],
        }
    ]


@pytest.mark.asyncio
async def test_dataset_toolkit_accepts_encoded_dates_and_timestamps() -> None:
    room = _FakeRoom()
    toolkit = DatasetToolkit(
        tables={
            "events": {
                "event_date": DateDataType(),
                "created_at": TimestampDataType(),
            }
        },
        namespace=["prod"],
        room=room,
    )

    result = await toolkit.execute(
        context=_tool_context(room),
        name="insert_events_rows",
        input=JsonContent(
            json={
                "rows": [
                    {
                        "event_date": {"date": "2026-04-09"},
                        "created_at": {"timestamp": "2026-04-09T12:30:45Z"},
                    }
                ]
            }
        ),
    )

    assert isinstance(result, EmptyContent)
    assert room.datasets.insert_calls == [
        {
            "table": "events",
            "records": [
                {
                    "event_date": date(2026, 4, 9),
                    "created_at": datetime(2026, 4, 9, 12, 30, 45, tzinfo=timezone.utc),
                }
            ],
            "namespace": ["prod"],
        }
    ]


@pytest.mark.asyncio
async def test_dataset_toolkit_advanced_search_uses_room_dataset_search() -> None:
    room = _FakeRoom()
    toolkit = DatasetToolkit(
        tables={
            "users": {
                "id": IntDataType(),
                "name": TextDataType(),
            }
        },
        namespace=["prod"],
        room=room,
    )

    result = await toolkit.execute(
        context=_tool_context(room),
        name="advanced_search_users",
        input=JsonContent(json={"where": "id = 1"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"rows": [{"id": 1, "name": "Alice"}]}
    assert room.datasets.search_calls == [
        {
            "table": "users",
            "where": "id = 1",
            "namespace": ["prod"],
        }
    ]


@pytest.mark.asyncio
async def test_make_dataset_toolkit_uses_room_dataset_inspect() -> None:
    room = _FakeRoom()

    toolkit = await make_dataset_toolkit(
        room=room,
        tables=["users"],
        namespace=["prod"],
        read_only=False,
    )

    assert isinstance(toolkit, DatasetToolkit)
    assert room.datasets.inspect_calls == [
        {
            "table": "users",
            "namespace": ["prod"],
        }
    ]
