from __future__ import annotations

from datetime import date, datetime, timezone

import pyarrow as pa
import pytest

from meshagent.api.messaging import (
    EmptyContent,
    JsonContent,
)
from meshagent.api.room_server_client import DatasetSqlQuery, DatasetSqlStatement
from meshagent.tools import ToolContext
from meshagent.tools.dataset import (
    AdvancedDeleteRowsTool,
    CountTool,
    DatasetToolkit,
    SpawnTaskForEachRow,
    make_dataset_toolkit,
)
from meshagent.tools.strict_schema import ensure_strict_json_schema


class _FakeDatasetsClient:
    def __init__(self) -> None:
        self.insert_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.count_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.inspect_calls: list[dict] = []
        self.execute_sql_calls: list[dict] = []
        self.read_sql_query_calls: list[dict] = []
        self.close_sql_query_calls: list[dict] = []
        self.sql_result = DatasetSqlStatement(rows_affected=0)
        self.sql_batches: list[pa.RecordBatch | pa.Table] = []
        self.sql_read_error: Exception | None = None
        self.sql_close_error: Exception | None = None
        self.search_result = pa.table({"id": [1], "name": ["Alice"]})
        self.count_result = 3

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
        return self.search_result

    async def count(self, *, table: str, where=None, namespace=None):
        self.count_calls.append(
            {
                "table": table,
                "where": where,
                "namespace": namespace,
            }
        )
        return self.count_result

    async def delete(self, *, table: str, where: str, namespace=None) -> None:
        self.delete_calls.append(
            {
                "table": table,
                "where": where,
                "namespace": namespace,
            }
        )

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
        if self.sql_read_error is not None:
            raise self.sql_read_error
        for batch in self.sql_batches:
            yield batch

    async def close_sql_query(self, *, query_id: str):
        self.close_sql_query_calls.append({"query_id": query_id})
        if self.sql_close_error is not None:
            raise self.sql_close_error


class _FakeQueuesClient:
    def __init__(self) -> None:
        self.send_calls: list[dict] = []

    async def send(self, *, name: str, message: dict, create: bool = True) -> None:
        self.send_calls.append({"name": name, "message": message, "create": create})


class _FakeRoom:
    def __init__(self) -> None:
        self.datasets = _FakeDatasetsClient()
        self.queues = _FakeQueuesClient()


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


def test_dataset_toolkit_tool_order_and_metadata_match_python() -> None:
    room = _FakeRoom()
    schema = {
        "id": pa.int64(),
        "name": pa.string(),
    }
    toolkit = DatasetToolkit(
        tables={
            "users": schema,
            "events": schema,
        },
        namespace=["prod"],
        room=room,
    )

    assert toolkit.name == "dataset"
    assert toolkit.title == "dataset"
    assert toolkit.description == "tools for interacting with meshagent datasets"
    assert [tool.name for tool in toolkit.tools] == [
        "execute_sql",
        "insert_users_rows",
        "update_users_rows",
        "advanced_delete_users",
        "count_users",
        "advanced_search_users",
        "insert_events_rows",
        "update_events_rows",
        "advanced_delete_events",
        "count_events",
        "advanced_search_events",
    ]
    assert "list_tables" not in {tool.name for tool in toolkit.tools}
    assert "search_users" not in {tool.name for tool in toolkit.tools}
    assert "spawn_task_for_each_users_row" not in {tool.name for tool in toolkit.tools}

    read_only_toolkit = DatasetToolkit(
        tables={"users": schema},
        read_only=True,
        namespace=["prod"],
        room=room,
    )

    assert [tool.name for tool in read_only_toolkit.tools] == [
        "execute_sql",
        "count_users",
        "advanced_search_users",
    ]


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
async def test_dataset_toolkit_accepts_encoded_binary_rows() -> None:
    room = _FakeRoom()
    toolkit = DatasetToolkit(
        tables={
            "events": {
                "payload": pa.binary(),
            }
        },
        namespace=["prod"],
        room=room,
    )

    result = await toolkit.execute(
        context=_tool_context(room),
        name="insert_events_rows",
        input=JsonContent(json={"rows": [{"payload": {"binary": "AAEC+v8="}}]}),
    )

    assert isinstance(result, EmptyContent)
    assert room.datasets.insert_calls == [
        {
            "table": "events",
            "records": [{"payload": b"\x00\x01\x02\xfa\xff"}],
            "namespace": ["prod"],
        }
    ]


@pytest.mark.asyncio
async def test_spawn_task_for_each_row_searches_and_queues_messages() -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64(), "name": pa.string()},
        prompt="Process {id}: {name}",
        queue="jobs",
        namespace=["prod"],
    )

    result = await tool.execute(
        context=_tool_context(room),
        query={"name": "Alice", "id": None},
        limit=5,
        offset=1,
        select=["id", "name"],
    )

    assert result == "added 1 items to the queue jobs"
    assert room.datasets.search_calls == [
        {
            "table": "users",
            "where": {"name": "Alice"},
            "namespace": ["prod"],
            "select": ["id", "name"],
            "offset": 1,
            "limit": 5,
        }
    ]
    assert room.queues.send_calls == [
        {
            "name": "jobs",
            "message": {
                "prompt": "Process 1: Alice",
                "row": {"id": 1, "name": "Alice"},
            },
            "create": True,
        }
    ]


@pytest.mark.asyncio
async def test_spawn_task_for_each_row_all_null_query_returns_zero_without_queue() -> (
    None
):
    room = _FakeRoom()
    room.datasets.search_result = pa.table({"id": pa.array([], type=pa.int64())})
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64(), "name": pa.string()},
        prompt="Process {id}: {name}",
        queue="jobs",
        namespace=["prod"],
    )

    result = await tool.execute(
        context=_tool_context(room),
        query={"name": None, "id": None},
        limit=5,
        offset=1,
        select=["id", "name"],
    )

    assert result == "added 0 items to the queue jobs"
    assert room.datasets.search_calls == [
        {
            "table": "users",
            "where": None,
            "namespace": ["prod"],
            "select": ["id", "name"],
            "offset": 1,
            "limit": 5,
        }
    ]
    assert room.queues.send_calls == []


def test_spawn_task_for_each_row_custom_metadata_matches_python() -> None:
    tool = SpawnTaskForEachRow(
        room=_FakeRoom(),
        table="users",
        schema={"id": pa.int64(), "name": pa.string()},
        prompt="Process {id}",
        queue="jobs",
        name="custom_spawn",
        title="Custom Spawn",
        description="custom description",
    )

    assert tool.name == "custom_spawn"
    assert tool.title == "Custom Spawn"
    assert tool.description == "custom description"
    assert tool.input_schema["properties"]["select"]["description"] == (
        "the columns to return, available columns: id,name"
    )


@pytest.mark.asyncio
async def test_count_tool_matches_python_schema_normalization_and_room_call() -> None:
    room = _FakeRoom()
    tool = CountTool(
        room=room,
        table="users",
        schema={
            "name": pa.string(),
            "event_date": pa.date32(),
        },
        namespace=["prod"],
    )

    result = await tool.execute(
        context=_tool_context(room),
        query={
            "name": "Alice",
            "event_date": {"date": "2026-04-09"},
        },
    )

    assert tool.name == "count_users"
    assert tool.title == "count_users"
    assert tool.description == "count matching rows in the users table"
    assert tool.input_schema["required"] == ["query"]
    assert result == {"rows": 3}
    assert room.datasets.count_calls == [
        {
            "table": "users",
            "where": {
                "name": "Alice",
                "event_date": date(2026, 4, 9),
            },
            "namespace": ["prod"],
        }
    ]

    room.datasets.count_calls.clear()
    result = await tool.execute(
        context=_tool_context(room),
        query={"name": None, "event_date": None},
    )

    assert result == {"rows": 3}
    assert room.datasets.count_calls == [
        {
            "table": "users",
            "where": None,
            "namespace": ["prod"],
        }
    ]


@pytest.mark.asyncio
async def test_advanced_delete_tool_matches_python_schema_and_room_call() -> None:
    room = _FakeRoom()
    tool = AdvancedDeleteRowsTool(
        room=room,
        table="users",
        schema={
            "name": pa.field("name", pa.string(), nullable=False),
            "event_date": pa.field("event_date", pa.date32(), nullable=True),
        },
        namespace=["prod"],
    )

    result = await tool.execute(
        context=_tool_context(room),
        where="name = 'Alice'",
    )

    assert tool.name == "advanced_delete_users"
    assert tool.title == "advanced delete users"
    assert (
        tool.description
        == "advanced search users table with a lancedb compatible filter and delete the matching rows"
    )
    assert tool.input_schema["required"] == ["where"]
    assert tool.input_schema["properties"]["where"]["description"] == (
        "a lance db compatible filter, columns are: "
        "column name => pyarrow.Field<name: string not null>"
        "column event_date => pyarrow.Field<event_date: date32[day]>"
    )
    assert result == {"ok": True}
    assert room.datasets.delete_calls == [
        {
            "table": "users",
            "where": "name = 'Alice'",
            "namespace": ["prod"],
        }
    ]


def test_spawn_task_for_each_row_prompt_renders_simple_placeholders() -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64(), "name": pa.string()},
        prompt="Process {id}: {name} {{ok}}",
        queue="jobs",
        namespace=["prod"],
    )

    assert tool.make_message(
        context=_tool_context(room),
        row={"id": 7, "name": "Alice", "flag": True, "items": [1, 2]},
    ) == {
        "prompt": "Process 7: Alice {ok}",
        "row": {"id": 7, "name": "Alice", "flag": True, "items": [1, 2]},
    }


def test_spawn_task_for_each_row_prompt_renders_json_values() -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64()},
        prompt="{flag}|{none}|{items}|{user}",
        queue="jobs",
        namespace=["prod"],
    )

    assert tool.make_message(
        context=_tool_context(room),
        row={
            "flag": True,
            "none": None,
            "items": [1, "two"],
            "user": {"name": "Alice"},
        },
    ) == {
        "prompt": 'true|null|[1,"two"]|{"name":"Alice"}',
        "row": {
            "flag": True,
            "none": None,
            "items": [1, "two"],
            "user": {"name": "Alice"},
        },
    }


@pytest.mark.parametrize(
    ("prompt", "error_type", "message"),
    [
        ("{missing}", KeyError, "missing"),
        ("{id:04d}", ValueError, "unsupported dataset prompt placeholder"),
        ("{user[name]}", ValueError, "unsupported dataset prompt placeholder"),
        ("{name!r}", ValueError, "unsupported dataset prompt placeholder"),
        ("{name.__class__}", ValueError, "unsupported dataset prompt placeholder"),
        ("{", ValueError, r"unmatched '\{' in dataset prompt"),
        ("}", ValueError, r"unmatched '\}' in dataset prompt"),
    ],
)
def test_spawn_task_for_each_row_prompt_rejects_unsupported_placeholders(
    prompt: str, error_type: type[Exception], message: str
) -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64(), "name": pa.string()},
        prompt=prompt,
        queue="jobs",
        namespace=["prod"],
    )

    with pytest.raises(error_type, match=message):
        tool.make_message(context=_tool_context(room), row={"id": 7, "name": "Alice"})


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
    tool = toolkit.get_tool("advanced_search_users")

    assert tool.name == "advanced_search_users"
    assert tool.title == "advanced search users"
    assert (
        tool.description
        == "advanced search users table with a lancedb compatible filter"
    )
    assert tool.input_schema["required"] == ["where"]
    assert tool.input_schema["properties"]["where"]["description"] == (
        "a lance db compatible filter, columns are: "
        "column id => int64\n"
        "column name => string\n"
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
async def test_dataset_toolkit_execute_sql_empty_query_result_closes_query() -> None:
    room = _FakeRoom()
    schema = pa.schema([("id", pa.int64()), ("name", pa.string())])
    room.datasets.sql_result = DatasetSqlQuery(schema=schema, query_id="q1")
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
        input=JsonContent(json={"query": "select * from users"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"kind": "query", "rows": []}
    assert room.datasets.read_sql_query_calls == [{"query_id": "q1"}]
    assert room.datasets.close_sql_query_calls == [{"query_id": "q1"}]


@pytest.mark.asyncio
async def test_dataset_toolkit_execute_sql_concatenates_table_chunks() -> None:
    room = _FakeRoom()
    schema = pa.schema([("id", pa.int64()), ("name", pa.string())])
    room.datasets.sql_result = DatasetSqlQuery(schema=schema, query_id="q1")
    room.datasets.sql_batches = [
        pa.table({"id": [1], "name": ["Alice"]}, schema=schema),
        pa.table({"id": [2], "name": ["Bob"]}, schema=schema),
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
        input=JsonContent(json={"query": "select * from users"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "kind": "query",
        "rows": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}],
    }
    assert room.datasets.read_sql_query_calls == [{"query_id": "q1"}]
    assert room.datasets.close_sql_query_calls == [{"query_id": "q1"}]


@pytest.mark.asyncio
async def test_dataset_toolkit_execute_sql_closes_query_after_read_error() -> None:
    room = _FakeRoom()
    schema = pa.schema([("id", pa.int64()), ("name", pa.string())])
    room.datasets.sql_result = DatasetSqlQuery(schema=schema, query_id="q1")
    room.datasets.sql_read_error = RuntimeError("read failed")
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

    with pytest.raises(RuntimeError, match="read failed"):
        await toolkit.execute(
            context=_tool_context(room),
            name="execute_sql",
            input=JsonContent(json={"query": "select * from users"}),
        )

    assert room.datasets.read_sql_query_calls == [{"query_id": "q1"}]
    assert room.datasets.close_sql_query_calls == [{"query_id": "q1"}]


@pytest.mark.asyncio
async def test_dataset_toolkit_execute_sql_returns_close_error_after_read_success() -> (
    None
):
    room = _FakeRoom()
    schema = pa.schema([("id", pa.int64()), ("name", pa.string())])
    room.datasets.sql_result = DatasetSqlQuery(schema=schema, query_id="q1")
    room.datasets.sql_batches = [
        pa.record_batch([[1], ["Alice"]], schema=schema),
    ]
    room.datasets.sql_close_error = RuntimeError("close failed")
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

    with pytest.raises(RuntimeError, match="close failed"):
        await toolkit.execute(
            context=_tool_context(room),
            name="execute_sql",
            input=JsonContent(json={"query": "select * from users"}),
        )

    assert room.datasets.read_sql_query_calls == [{"query_id": "q1"}]
    assert room.datasets.close_sql_query_calls == [{"query_id": "q1"}]


@pytest.mark.asyncio
async def test_dataset_toolkit_execute_sql_close_error_masks_read_error() -> None:
    room = _FakeRoom()
    schema = pa.schema([("id", pa.int64()), ("name", pa.string())])
    room.datasets.sql_result = DatasetSqlQuery(schema=schema, query_id="q1")
    room.datasets.sql_read_error = RuntimeError("read failed")
    room.datasets.sql_close_error = RuntimeError("close failed")
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

    with pytest.raises(RuntimeError, match="close failed"):
        await toolkit.execute(
            context=_tool_context(room),
            name="execute_sql",
            input=JsonContent(json={"query": "select * from users"}),
        )

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
    tool = toolkit.get_tool("execute_sql")

    assert tool.name == "execute_sql"
    assert tool.title == "execute SQL"
    assert tool.description == (
        "execute a DataFusion SQL query or statement against the room datasets. "
        "SELECT-like commands return rows; update, delete, DDL, and other statements return rows_affected."
    )
    assert tool.input_schema["required"] == ["query", "params"]
    assert tool.input_schema["properties"]["params"]["description"] == (
        "optional named parameter values encoded as a single JSON object"
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
    assert room.datasets.execute_sql_calls[-1]["params"] is None

    for params in ({}, None):
        result = await toolkit.execute(
            context=_tool_context(room),
            name="execute_sql",
            input=JsonContent(
                json={
                    "query": "delete from users",
                    "params": params,
                }
            ),
        )

        assert isinstance(result, JsonContent)
        assert result.json == {"kind": "statement", "rows_affected": 3}
        assert room.datasets.execute_sql_calls[-1]["params"] is None


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
