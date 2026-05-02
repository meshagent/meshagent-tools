from __future__ import annotations

from datetime import date, datetime, timezone

import pyarrow as pa
import pytest

from meshagent.api.messaging import EmptyContent, JsonContent
from meshagent.api.room_server_client import DatasetSqlQuery, DatasetSqlStatement
from meshagent.tools import ToolContext
from meshagent.tools.dataset import DatasetToolkit, make_dataset_toolkit
from meshagent.tools.strict_schema import ensure_strict_json_schema


class _FakeDatasetsClient:
    def __init__(self) -> None:
        self.insert_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.inspect_calls: list[dict] = []
        self.execute_sql_calls: list[dict] = []
        self.read_sql_query_calls: list[dict] = []
        self.close_sql_query_calls: list[dict] = []
        self.sql_result = DatasetSqlStatement(rows_affected=0)
        self.sql_batches: list[pa.RecordBatch] = []

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
        return pa.table({"id": [1], "name": ["Alice"]})

    async def inspect(self, *, table: str, namespace=None):
        self.inspect_calls.append({"table": table, "namespace": namespace})
        return {
            "id": pa.int64(),
            "name": pa.string(),
        }

    async def execute_sql(self, *, query: str, params=None, namespace=None):
        self.execute_sql_calls.append(
            {
                "query": query,
                "params": params,
                "namespace": namespace,
            }
        )
        return self.sql_result

    async def read_sql_query(self, *, query_id: str):
        self.read_sql_query_calls.append({"query_id": query_id})
        for batch in self.sql_batches:
            yield batch

    async def close_sql_query(self, *, query_id: str):
        self.close_sql_query_calls.append({"query_id": query_id})


class _FakeRoom:
    def __init__(self) -> None:
        self.datasets = _FakeDatasetsClient()


def _tool_context(room: _FakeRoom) -> ToolContext:
    del room
    return ToolContext(caller=object())


def _assert_openai_function_schema_compatible(schema: object, *, path: str) -> None:
    if isinstance(schema, list):
        for index, item in enumerate(schema):
            _assert_openai_function_schema_compatible(
                item,
                path=f"{path}.{index}",
            )
        return

    if not isinstance(schema, dict):
        return

    if schema.get("type") == "array":
        assert "items" in schema, f"{path}: array schema missing items"

    if schema.get("type") == "object":
        additional_properties = schema.get("additionalProperties")
        assert additional_properties is False or isinstance(
            additional_properties,
            dict,
        ), f"{path}: object schema must set additionalProperties to false or a schema"

        properties = schema.get("properties")
        if isinstance(properties, dict):
            assert set(schema.get("required", [])) == set(properties.keys()), (
                f"{path}: object schema must require all properties"
            )

    for key in ("anyOf", "oneOf", "allOf"):
        value = schema.get(key)
        if isinstance(value, list):
            for index, item in enumerate(value):
                _assert_openai_function_schema_compatible(
                    item,
                    path=f"{path}.{key}.{index}",
                )

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, item in properties.items():
            _assert_openai_function_schema_compatible(
                item,
                path=f"{path}.properties.{name}",
            )

    additional_properties = schema.get("additionalProperties")
    if isinstance(additional_properties, dict):
        _assert_openai_function_schema_compatible(
            additional_properties,
            path=f"{path}.additionalProperties",
        )

    items = schema.get("items")
    if isinstance(items, dict):
        _assert_openai_function_schema_compatible(items, path=f"{path}.items")

    defs = schema.get("$defs")
    if isinstance(defs, dict):
        for name, item in defs.items():
            _assert_openai_function_schema_compatible(
                item,
                path=f"{path}.$defs.{name}",
            )


def test_dataset_toolkit_uses_openai_compatible_strict_input_schemas() -> None:
    room = _FakeRoom()
    toolkit = DatasetToolkit(
        tables={
            "users": {
                "id": pa.int64(),
                "name": pa.string(),
                "metadata": pa.json_() if hasattr(pa, "json_") else pa.string(),
                "tags": pa.list_(pa.string()),
                "profile": pa.struct([("age", pa.int64())]),
            }
        },
        namespace=["prod"],
        room=room,
    )

    for tool in toolkit.tools:
        assert tool.input_schema is not None
        assert tool.input_schema == ensure_strict_json_schema(tool.input_schema)
        _assert_openai_function_schema_compatible(
            tool.input_schema,
            path=tool.name,
        )


@pytest.mark.asyncio
async def test_dataset_toolkit_insert_rows_uses_room_dataset_insert() -> None:
    room = _FakeRoom()
    toolkit = DatasetToolkit(
        tables={
            "users": {
                "id": pa.int64(),
                "name": pa.string(),
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
                "event_date": pa.date32(),
                "created_at": pa.timestamp("us"),
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
                "id": pa.int64(),
                "name": pa.string(),
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
async def test_dataset_toolkit_execute_sql_returns_rows_and_closes_query() -> None:
    room = _FakeRoom()
    schema = pa.schema([("id", pa.int64()), ("name", pa.string())])
    room.datasets.sql_result = DatasetSqlQuery(schema=schema, query_id="q1")
    room.datasets.sql_batches = [
        pa.record_batch([[1], ["Alice"]], schema=schema),
    ]
    toolkit = DatasetToolkit(
        tables={
            "users": {
                "id": pa.int64(),
                "name": pa.string(),
            }
        },
        namespace=["prod"],
        room=room,
    )

    result = await toolkit.execute(
        context=_tool_context(room),
        name="execute_sql",
        input=JsonContent(json={"query": "select * from users", "params": {"id": 1}}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"kind": "query", "rows": [{"id": 1, "name": "Alice"}]}
    assert room.datasets.execute_sql_calls[0]["query"] == "select * from users"
    assert room.datasets.execute_sql_calls[0]["namespace"] == ["prod"]
    assert room.datasets.execute_sql_calls[0]["params"].to_pylist() == [{"id": 1}]
    assert room.datasets.read_sql_query_calls == [{"query_id": "q1"}]
    assert room.datasets.close_sql_query_calls == [{"query_id": "q1"}]


@pytest.mark.asyncio
async def test_dataset_toolkit_execute_sql_returns_statement_result() -> None:
    room = _FakeRoom()
    room.datasets.sql_result = DatasetSqlStatement(rows_affected=3)
    toolkit = DatasetToolkit(
        tables={
            "users": {
                "id": pa.int64(),
                "name": pa.string(),
            }
        },
        namespace=["prod"],
        room=room,
    )

    result = await toolkit.execute(
        context=_tool_context(room),
        name="execute_sql",
        input=JsonContent(json={"query": "delete from users where id = 1"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"kind": "statement", "rows_affected": 3}
    assert room.datasets.read_sql_query_calls == []
    assert room.datasets.close_sql_query_calls == []


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
