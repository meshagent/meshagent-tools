from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pyarrow as pa
import pytest

from meshagent.api.messaging import (
    EmptyContent,
    JsonContent,
    pack_message,
    split_message_header,
)
from meshagent.api.room_server_client import DatasetSqlQuery, DatasetSqlStatement
from meshagent.tools import ToolContext
from meshagent.tools.dataset import (
    DatasetToolkit,
    SpawnTaskForEachRow,
    make_dataset_toolkit,
)
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
        self.sql_batches: list[pa.RecordBatch | pa.Table] = []
        self.sql_read_error: Exception | None = None
        self.sql_close_error: Exception | None = None
        self.search_result = pa.table({"id": [1], "name": ["Alice"]})

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


def test_spawn_task_for_each_row_prompt_format_uses_python_format_specs() -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64(), "name": pa.string()},
        prompt="{id:04d}|{score:.2f}|{score:08.2f}|{id:+d}|{id: d}|{neg:04d}|{id:x}|{id:#04x}|{id:b}|{id:o}|{id:X}|{score:e}|{huge}|{huge:}|{tiny}|{tiny:}|{name!r}|{quote!r}|{control!r}|{nonascii!r}|{nonascii!a}|{none!s}|{{{name}}}|{name:8}|{name:>8}|{name:<8}|{name:^8}|{name:*^8}|{name:.3}|{name:s}|{name:>8s}|{flag!r}|{flag:d}|{flag:04d}|{flag:b}|{flag:x}|{flag:f}|{flag:>8}|{id:<8d}|{id:^8d}|{score:<8.2f}|{name:>{width}}|{score:.{precision}f}|{id:{int_spec}}|{name:{name_spec}}|{name:08}|{name!r:>10}|{flag!s:^6}|{score!r:.4}|{name!r:>{width}}|{id:{width!r}}|{big:,d}|{big:_d}|{big:,}|{big:_}|{id:=+6d}|{neg:=06d}|{id:*=+6d}|{id:*=#8x}|{score:g}|{score:.2g}|{score:G}|{small:.2g}|{bigfloat:.3g}|{score:%}|{score:.1%}|{score:08.1%}|{negscore:+08.1%}|{whole:#.0%}|{letter:c}|{letter:4c}|{letter:^5c}|{big:n}|{score:n}|{whole:#g}|{whole:#.2g}|{whole:#.0f}|{whole:#.0e}|{bigfloat:,.1f}|{bigfloat:_.1f}|{bigfloat:,.8g}|{bigfloat:_.8g}|{bigfloat:,%}",
        queue="jobs",
        namespace=["prod"],
    )

    assert tool.make_message(
        context=_tool_context(room),
        row={
            "id": 7,
            "name": "Alice",
            "quote": "it's",
            "control": "\x07",
            "nonascii": "é",
            "score": 3.14159,
            "huge": 1e20,
            "tiny": 1e-7,
            "flag": True,
            "none": None,
            "neg": -7,
            "width": 8,
            "precision": 3,
            "big": 1234567,
            "small": 0.0001234,
            "bigfloat": 1234567.0,
            "negscore": -3.14159,
            "whole": 3.0,
            "letter": 65,
            "int_spec": "04d",
            "name_spec": "*^8",
        },
    ) == {
        "prompt": "0007|3.14|00003.14|+7| 7|-007|7|0x07|111|7|7|3.141590e+00|1e+20|1e+20|1e-07|1e-07|'Alice'|\"it's\"|'\\x07'|'é'|'\\xe9'|None|{Alice}|Alice   |   Alice|Alice   | Alice  |*Alice**|Ali|Alice|   Alice|True|1|0001|1|1|1.000000|       1|7       |   7    |3.14    |   Alice|3.142|0007|*Alice**|Alice000|   'Alice'| True |3.14| 'Alice'|       7|1,234,567|1_234_567|1,234,567|1_234_567|+    7|-00007|+****7|0x*****7|3.14159|3.1|3.14159|0.00012|1.23e+06|314.159000%|314.2%|00314.2%|-0314.2%|300.%|A|   A|  A  |1234567|3.14159|3.00000|3.0|3.|3.e+00|1,234,567.0|1_234_567.0|1,234,567|1_234_567|123,456,700.000000%",
        "row": {
            "id": 7,
            "name": "Alice",
            "quote": "it's",
            "control": "\x07",
            "nonascii": "é",
            "score": 3.14159,
            "huge": 1e20,
            "tiny": 1e-7,
            "flag": True,
            "none": None,
            "neg": -7,
            "width": 8,
            "precision": 3,
            "big": 1234567,
            "small": 0.0001234,
            "bigfloat": 1234567.0,
            "negscore": -3.14159,
            "whole": 3.0,
            "letter": 65,
            "int_spec": "04d",
            "name_spec": "*^8",
        },
    }


def test_spawn_task_for_each_row_prompt_format_numeric_attributes() -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64(), "name": pa.string()},
        prompt="{id.real}|{id.imag}|{id.numerator}|{id.denominator}|{score.real}|{score.imag}|{id.real.real}|{id.real.imag}|{score.real.real}|{score.real.imag}|{flag.numerator}|{id.real:04d}|{score.imag:.1f}",
        queue="jobs",
        namespace=["prod"],
    )

    assert tool.make_message(
        context=_tool_context(room),
        row={"id": 7, "score": 3.14159, "flag": True},
    ) == {
        "prompt": "7|0|7|1|3.14159|0.0|7|0|3.14159|0.0|1|0007|0.0",
        "row": {"id": 7, "score": 3.14159, "flag": True},
    }


def test_spawn_task_for_each_row_prompt_format_dict_keys_are_literal() -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64()},
        prompt="{user[score]}|{user['score']}|{user[\"score\"]}",
        queue="jobs",
        namespace=["prod"],
    )

    assert tool.make_message(
        context=_tool_context(room),
        row={"user": {"score": 12, "'score'": 13, '"score"': 14}},
    ) == {
        "prompt": "12|13|14",
        "row": {"user": {"score": 12, "'score'": 13, '"score"': 14}},
    }


def test_spawn_task_for_each_row_prompt_format_repr_escapes_non_printing_unicode() -> (
    None
):
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64()},
        prompt="{nbsp!r}|{line_sep!r}|{zero_width!r}|{emoji!r}|{nbsp!a}|{emoji!a}",
        queue="jobs",
        namespace=["prod"],
    )

    assert tool.make_message(
        context=_tool_context(room),
        row={
            "nbsp": "\u00a0",
            "line_sep": "\u2028",
            "zero_width": "\u200b",
            "emoji": "😀",
        },
    ) == {
        "prompt": "'\\xa0'|'\\u2028'|'\\u200b'|'😀'|'\\xa0'|'\\U0001f600'",
        "row": {
            "nbsp": "\u00a0",
            "line_sep": "\u2028",
            "zero_width": "\u200b",
            "emoji": "😀",
        },
    }


def test_spawn_task_for_each_row_prompt_format_collection_repr_matches_python() -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64()},
        prompt="{obj}|{arr}|{ascii_obj!a}|{ascii_arr!a}",
        queue="jobs",
        namespace=["prod"],
    )

    assert tool.make_message(
        context=_tool_context(room),
        row={
            "obj": {"b": 1, "a": 2, "it's": "it's", "huge": 1e20},
            "arr": ["it's", "\u2028", 1e-7],
            "ascii_obj": {"é": "é", "nested": ["é"]},
            "ascii_arr": ["é", {"é": "é"}],
        },
    ) == {
        "prompt": "{'b': 1, 'a': 2, \"it's\": \"it's\", 'huge': 1e+20}|[\"it's\", '\\u2028', 1e-07]|{'\\xe9': '\\xe9', 'nested': ['\\xe9']}|['\\xe9', {'\\xe9': '\\xe9'}]",
        "row": {
            "obj": {"b": 1, "a": 2, "it's": "it's", "huge": 1e20},
            "arr": ["it's", "\u2028", 1e-7],
            "ascii_obj": {"é": "é", "nested": ["é"]},
            "ascii_arr": ["é", {"é": "é"}],
        },
    }


@pytest.mark.parametrize(
    ("prompt", "error_type", "message"),
    [
        (
            "{score.numerator}",
            AttributeError,
            "'float' object has no attribute 'numerator'",
        ),
        (
            "{score.imag.numerator}",
            AttributeError,
            "'float' object has no attribute 'numerator'",
        ),
        ("{id.real[0]}", TypeError, "'int' object is not subscriptable"),
        ("{name.real}", AttributeError, "'str' object has no attribute 'real'"),
        (
            "{user[name]suffix}",
            ValueError,
            "Only '.' or '\\[' may follow '\\]' in format field specifier",
        ),
        ("{user[}", ValueError, "expected '\\}' before end of string"),
        ("{user[]}", ValueError, "Empty attribute in format string"),
        ("{user.}", ValueError, "Empty attribute in format string"),
        ("{user..name}", ValueError, "Empty attribute in format string"),
        (
            "{items[+1]}",
            TypeError,
            "list indices must be integers or slices, not str",
        ),
        (
            "{items[9223372036854775808]}",
            ValueError,
            "Too many decimal digits in format string",
        ),
        ("{foo{bar}}", ValueError, "unexpected '\\{' in field name"),
    ],
)
def test_spawn_task_for_each_row_prompt_format_attribute_errors_match_python(
    prompt: str,
    error_type: type[Exception],
    message: str,
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
        tool.make_message(
            context=_tool_context(room),
            row={
                "id": 7,
                "score": 3.14159,
                "name": "Alice",
                "user": {"name": 1},
                "items": ["a", "b"],
                "foo{bar}": "ignored",
            },
        )


def test_spawn_task_for_each_row_prompt_format_class_attributes_match_python() -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={
            "name": pa.string(),
            "id": pa.int64(),
            "score": pa.float64(),
            "flag": pa.bool_(),
            "none": pa.string(),
            "items": pa.list_(pa.int64()),
            "user": pa.struct([pa.field("x", pa.int64())]),
        },
        prompt="{name.__class__}|{name.__class__.__name__}|{name.__class__.__module__}|{id.__class__}|{score.__class__.__name__}|{flag.__class__.__name__}|{flag.__class__.__base__}|{flag.__class__.__mro__}|{none.__class__.__name__}|{items.__class__.__name__}|{user.__class__.__name__}|{name.__class__.__base__}|{name.__class__.__base__.__class__}|{name.__class__.__base__.__base__}|{name.__class__.__base__.__base__.__class__}|{name.__class__.__bases__[0].__name__}|{name.__class__.__bases__.__class__.__name__}",
        queue="jobs",
    )

    assert tool.make_message(
        context=_tool_context(room),
        row={
            "name": "Alice",
            "id": 7,
            "score": 1.5,
            "flag": True,
            "none": None,
            "items": [1],
            "user": {"x": 1},
        },
    )["prompt"] == (
        "<class 'str'>|str|builtins|<class 'int'>|float|bool|"
        "<class 'int'>|(<class 'bool'>, <class 'int'>, <class 'object'>)|"
        "NoneType|list|dict|<class 'object'>|<class 'type'>|None|"
        "<class 'NoneType'>|object|tuple"
    )

    tuple_error_tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={"name": pa.string()},
        prompt="{name.__class__.__bases__[x]}",
        queue="jobs",
    )
    with pytest.raises(TypeError, match="tuple indices must be integers or slices"):
        tuple_error_tool.make_message(
            context=_tool_context(room), row={"name": "Alice"}
        )

    tuple_attribute_error_tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={"name": pa.string()},
        prompt="{name.__class__.__bases__.foo}",
        queue="jobs",
    )
    with pytest.raises(AttributeError, match="'tuple' object has no attribute 'foo'"):
        tuple_attribute_error_tool.make_message(
            context=_tool_context(room), row={"name": "Alice"}
        )

    method_tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={"name": pa.string()},
        prompt="{name.upper.__name__}|{name.upper.__qualname__}|{name.upper.__self__}|{name.upper.__self__.__class__.__name__}|{name.upper.__class__}|{name.upper.__class__.__name__}|{name.upper.__class__.__mro__}",
        queue="jobs",
    )
    assert method_tool.make_message(context=_tool_context(room), row={"name": "Alice"})[
        "prompt"
    ] == (
        "upper|str.upper|Alice|str|<class 'builtin_function_or_method'>|"
        "builtin_function_or_method|(<class 'builtin_function_or_method'>, "
        "<class 'object'>)"
    )

    method_subscript_error_tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={"name": pa.string()},
        prompt="{name.upper[0]}",
        queue="jobs",
    )
    with pytest.raises(
        TypeError,
        match="'builtin_function_or_method' object is not subscriptable",
    ):
        method_subscript_error_tool.make_message(
            context=_tool_context(room), row={"name": "Alice"}
        )

    method_attribute_error_tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={"name": pa.string()},
        prompt="{name.upper.foo}",
        queue="jobs",
    )
    with pytest.raises(
        AttributeError,
        match="'builtin_function_or_method' object has no attribute 'foo'",
    ):
        method_attribute_error_tool.make_message(
            context=_tool_context(room), row={"name": "Alice"}
        )

    method_format_error_tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={"name": pa.string()},
        prompt="{name.upper:>8}",
        queue="jobs",
    )
    with pytest.raises(
        TypeError,
        match="unsupported format string passed to builtin_function_or_method.__format__",
    ):
        method_format_error_tool.make_message(
            context=_tool_context(room), row={"name": "Alice"}
        )


def test_spawn_task_for_each_row_prompt_format_list_index_overflow_matches_python() -> (
    None
):
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64()},
        prompt="{items[0000000000000000000]}",
        queue="jobs",
        namespace=["prod"],
    )

    assert tool.make_message(
        context=_tool_context(room),
        row={"items": ["a"]},
    ) == {
        "prompt": "a",
        "row": {"items": ["a"]},
    }


def test_spawn_task_for_each_row_prompt_format_integer_base_grouping_matches_python() -> (
    None
):
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64()},
        prompt="{big:_x}|{big:_X}|{big:_b}|{big:_o}|{big:#_x}|{big:#_b}|{big:#_o}|{neg:x}|{neg:b}|{neg:o}|{neg:#_x}|{small_neg:#08x}|{small_neg:#08b}|{small_neg:#08o}",
        queue="jobs",
        namespace=["prod"],
    )

    assert tool.make_message(
        context=_tool_context(room),
        row={"big": 1234567, "neg": -1234567, "small_neg": -7},
    ) == {
        "prompt": "12_d687|12_D687|1_0010_1101_0110_1000_0111|455_3207|0x12_d687|0b1_0010_1101_0110_1000_0111|0o455_3207|-12d687|-100101101011010000111|-4553207|-0x12_d687|-0x00007|-0b00111|-0o00007",
        "row": {"big": 1234567, "neg": -1234567, "small_neg": -7},
    }


def test_spawn_task_for_each_row_prompt_format_uses_pyarrow_to_pylist_scalars() -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={
            "event_date": pa.date32(),
            "created_at": pa.timestamp("us", tz="UTC"),
            "price": pa.decimal128(12, 4),
            "payload": pa.binary(),
            "quote_payload": pa.binary(),
        },
        prompt="{event_date}|{event_date!r}|{event_date.year:04d}-{event_date.month:02d}-{event_date.day:02d}|{event_date:%a|%A|%b|%B|%j|%w|%u|%U|%W|%I|%p|%y|%x|%X|%C|%D|%F|%R|%T|%V|%G|%g|%-d|%_d|%0d|%Q|%%Q|%%%Q}|{created_at}|{created_at!r}|{created_at.year:04d}-{created_at.month:02d}-{created_at.day:02d}T{created_at.hour:02d}:{created_at.minute:02d}:{created_at.second:02d}.{created_at.microsecond:06d}|{created_at.tzinfo}|{created_at.tzinfo!r}|{created_at.tzinfo.zone}|{created_at.fold}|{created_at:%Y/%m/%d %H:%M:%S.%f %z %Z|%a|%b|%j|%I|%p|%C|%D|%F|%R|%T|%V|%G|%g|%-d|%_d|%0d|%Q|%%Q|%%%Q}|{price}|{price!r}|{price:f}|{price:.2f}|{price:,f}|{price:,.2f}|{price:+012.2f}|{price:n}|{payload}|{payload!r}|{quote_payload}",
        queue="jobs",
    )
    row = pa.Table.from_pylist(
        [
            {
                "event_date": date(2026, 4, 9),
                "created_at": datetime(
                    2026, 4, 9, 12, 30, 45, 123456, tzinfo=timezone.utc
                ),
                "price": Decimal("1234.5600"),
                "payload": b"\x00\x01\xfa\xff",
                "quote_payload": b"a'b",
            }
        ],
        schema=pa.schema(
            [
                pa.field("event_date", pa.date32()),
                pa.field("created_at", pa.timestamp("us", tz="UTC")),
                pa.field("price", pa.decimal128(12, 4)),
                pa.field("payload", pa.binary()),
                pa.field("quote_payload", pa.binary()),
            ]
        ),
    ).to_pylist()[0]

    assert tool.make_message(context=_tool_context(room), row=row) == {
        "prompt": "2026-04-09|datetime.date(2026, 4, 9)|2026-04-09|Thu|Thursday|Apr|April|099|4|4|14|14|12|AM|26|04/09/26|00:00:00|20|04/09/26|2026-04-09|00:00|00:00:00|15|2026|26|9| 9|09|Q|%Q|%Q|2026-04-09 12:30:45.123456+00:00|datetime.datetime(2026, 4, 9, 12, 30, 45, 123456, tzinfo=<UTC>)|2026-04-09T12:30:45.123456|UTC|<UTC>|UTC|0|2026/04/09 12:30:45.123456 +0000 UTC|Thu|Apr|099|12|PM|20|04/09/26|2026-04-09|12:30|12:30:45|15|2026|26|9| 9|09|Q|%Q|%Q|1234.5600|Decimal('1234.5600')|1234.5600|1234.56|1,234.5600|1,234.56|+00001234.56|1234.5600|b'\\x00\\x01\\xfa\\xff'|b'\\x00\\x01\\xfa\\xff'|b\"a'b\"",
        "row": row,
    }
    packed = pack_message(
        {
            "type": "json",
            "json": {
                "message": tool.make_message(context=_tool_context(room), row=row),
            },
        }
    )
    assert json.loads(split_message_header(data=packed))["json"]["message"]["row"] == {
        "event_date": "2026-04-09",
        "created_at": "2026-04-09 12:30:45.123456+00:00",
        "price": "1234.5600",
        "payload": "b'\\x00\\x01\\xfa\\xff'",
        "quote_payload": 'b"a\'b"',
    }

    attr_tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={
            "event_date": pa.date32(),
            "created_at": pa.timestamp("us", tz="UTC"),
            "price": pa.decimal128(12, 4),
            "payload": pa.binary(),
            "quote_payload": pa.binary(),
        },
        prompt="{price.real}|{price.imag}|{payload[0]}|{quote_payload[1]}|{event_date.max}|{event_date.min}|{event_date.resolution}|{created_at.max}|{created_at.min}|{created_at.resolution}",
        queue="jobs",
    )

    assert attr_tool.make_message(context=_tool_context(room), row=row)["prompt"] == (
        "1234.5600|0|0|39|9999-12-31|0001-01-01|1 day, 0:00:00|"
        "9999-12-31 23:59:59.999999|0001-01-01 00:00:00|0:00:00.000001"
    )

    class_tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={
            "event_date": pa.date32(),
            "created_at": pa.timestamp("us", tz="UTC"),
            "price": pa.decimal128(12, 4),
            "payload": pa.binary(),
            "quote_payload": pa.binary(),
        },
        prompt="{event_date.__class__}|{event_date.__class__.__name__}|{event_date.__class__.__module__}|{created_at.__class__}|{created_at.__class__.__name__}|{created_at.__class__.__base__}|{created_at.__class__.__base__.__base__.__name__}|{created_at.__class__.__mro__}|{created_at.tzinfo.__class__}|{created_at.tzinfo.__class__.__module__}|{created_at.tzinfo.__class__.__base__.__base__.__name__}|{created_at.tzinfo.__class__.__mro__}|{price.__class__}|{price.__class__.__name__}|{payload.__class__}|{payload.__class__.__name__}",
        queue="jobs",
    )

    assert class_tool.make_message(context=_tool_context(room), row=row)["prompt"] == (
        "<class 'datetime.date'>|date|datetime|<class 'datetime.datetime'>|"
        "datetime|<class 'datetime.date'>|object|(<class 'datetime.datetime'>, "
        "<class 'datetime.date'>, <class 'object'>)|<class 'pytz.UTC'>|pytz|"
        "tzinfo|"
        "(<class 'pytz.UTC'>, <class 'pytz.tzinfo.BaseTzInfo'>, "
        "<class 'datetime.tzinfo'>, <class 'object'>)|"
        "<class 'decimal.Decimal'>|Decimal|<class 'bytes'>|bytes"
    )

    method_tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={
            "event_date": pa.date32(),
            "created_at": pa.timestamp("us", tz="UTC"),
            "price": pa.decimal128(12, 4),
            "payload": pa.binary(),
        },
        prompt="{payload.hex.__name__}|{payload.hex.__qualname__}|{payload.hex.__self__}|{payload.hex.__self__.__class__.__name__}|{event_date.isoformat.__name__}|{event_date.isoformat.__qualname__}|{event_date.isoformat.__self__}|{event_date.isoformat.__self__.__class__.__name__}|{created_at.isoformat.__name__}|{created_at.isoformat.__qualname__}|{created_at.isoformat.__self__}|{created_at.isoformat.__self__.__class__.__name__}|{price.as_tuple.__name__}|{price.as_tuple.__qualname__}|{price.as_tuple.__self__}|{price.as_tuple.__self__.__class__.__name__}",
        queue="jobs",
    )
    assert method_tool.make_message(context=_tool_context(room), row=row)["prompt"] == (
        "hex|bytes.hex|b'\\x00\\x01\\xfa\\xff'|bytes|isoformat|"
        "date.isoformat|2026-04-09|date|isoformat|datetime.isoformat|"
        "2026-04-09 12:30:45.123456+00:00|datetime|as_tuple|"
        "Decimal.as_tuple|1234.5600|Decimal"
    )

    class_error_tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={"event_date": pa.date32()},
        prompt="{event_date.__class__[0]}",
        queue="jobs",
    )
    with pytest.raises(TypeError, match="type 'datetime\\.date' is not subscriptable"):
        class_error_tool.make_message(context=_tool_context(room), row=row)

    class_attribute_error_tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={"event_date": pa.date32()},
        prompt="{event_date.__class__.foo}",
        queue="jobs",
    )
    with pytest.raises(
        AttributeError, match="type object 'datetime\\.date' has no attribute 'foo'"
    ):
        class_attribute_error_tool.make_message(context=_tool_context(room), row=row)


@pytest.mark.parametrize(
    ("prompt", "error_type", "message"),
    [
        ("{price[0]}", TypeError, "'decimal\\.Decimal' object is not subscriptable"),
        (
            "{price.foo}",
            AttributeError,
            "'decimal\\.Decimal' object has no attribute 'foo'",
        ),
        ("{payload[-1]}", TypeError, "byte indices must be integers or slices"),
        ("{payload[3]}", IndexError, "index out of range"),
        ("{payload.foo}", AttributeError, "'bytes' object has no attribute 'foo'"),
        (
            "{event_date[0]}",
            TypeError,
            "'datetime\\.date' object is not subscriptable",
        ),
        (
            "{event_date.foo}",
            AttributeError,
            "'datetime\\.date' object has no attribute 'foo'",
        ),
        (
            "{created_at[0]}",
            TypeError,
            "'datetime\\.datetime' object is not subscriptable",
        ),
        (
            "{created_at.foo}",
            AttributeError,
            "'datetime\\.datetime' object has no attribute 'foo'",
        ),
        ("{created_at.tzinfo[0]}", TypeError, "'UTC' object is not subscriptable"),
        (
            "{created_at.tzinfo.key}",
            AttributeError,
            "'UTC' object has no attribute 'key'",
        ),
    ],
)
def test_spawn_task_for_each_row_prompt_format_pyarrow_scalar_attribute_errors(
    prompt: str,
    error_type: type[Exception],
    message: str,
) -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={
            "event_date": pa.date32(),
            "created_at": pa.timestamp("us", tz="UTC"),
            "price": pa.decimal128(12, 4),
            "payload": pa.binary(),
        },
        prompt=prompt,
        queue="jobs",
    )
    row = pa.Table.from_pylist(
        [
            {
                "event_date": date(2026, 4, 9),
                "created_at": datetime(
                    2026, 4, 9, 12, 30, 45, 123456, tzinfo=timezone.utc
                ),
                "price": Decimal("1234.5600"),
                "payload": b"abc",
            }
        ],
        schema=pa.schema(
            [
                pa.field("event_date", pa.date32()),
                pa.field("created_at", pa.timestamp("us", tz="UTC")),
                pa.field("price", pa.decimal128(12, 4)),
                pa.field("payload", pa.binary()),
            ]
        ),
    ).to_pylist()[0]

    with pytest.raises(error_type, match=message):
        tool.make_message(context=_tool_context(room), row=row)


def test_spawn_task_for_each_row_prompt_format_date_strftime_extra_directives() -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={"event_date": pa.date32(), "created_at": pa.timestamp("us", tz="UTC")},
        prompt="{event_date:%e|%h|%k|%l|%n|%t|%r|%c|%+}|{created_at:%e|%h|%k|%l|%n|%t|%r|%c|%+}",
        queue="jobs",
    )
    row = pa.Table.from_pylist(
        [
            {
                "event_date": date(2026, 4, 9),
                "created_at": datetime(
                    2026, 4, 9, 12, 30, 45, 123456, tzinfo=timezone.utc
                ),
            }
        ],
        schema=pa.schema(
            [
                pa.field("event_date", pa.date32()),
                pa.field("created_at", pa.timestamp("us", tz="UTC")),
            ]
        ),
    ).to_pylist()[0]

    assert tool.make_message(context=_tool_context(room), row=row) == {
        "prompt": " 9|Apr| 0|12|\n|\t|12:00:00 AM|Thu Apr  9 00:00:00 2026|Thu Apr  9 00:00:00  2026| 9|Apr|12|12|\n|\t|12:30:45 PM|Thu Apr  9 12:30:45 2026|Thu Apr  9 12:30:45 PST 2026",
        "row": row,
    }


@pytest.mark.parametrize(
    ("value", "spec", "expected"),
    [
        ("1234.5650", ".2f", "1234.56"),
        ("1234.5750", ".2f", "1234.58"),
        ("-1234.5650", ".2f", "-1234.56"),
        ("-1234.5750", ".2f", "-1234.58"),
        ("9.9950", ".2f", "10.00"),
        ("9.9850", ".2f", "9.98"),
        ("0.0050", ".2f", "0.00"),
        ("0.0150", ".2f", "0.02"),
        ("1234.5650", "+012.2f", "+00001234.56"),
        ("1234.5750", ",.2f", "1,234.58"),
        ("1234.5600", "%", "123456.00%"),
        ("0.0012345600", "%", "0.12345600%"),
        ("-1234.5600", "%", "-123456.00%"),
        ("1234.5600", ".1%", "123456.0%"),
        ("1234.5600", ",.2%", "123,456.00%"),
        ("1234.5600", "+012.2%", "+0123456.00%"),
        ("1234.5", ".0f", "1234"),
        ("1235.5", ".0f", "1236"),
        ("1234.5600", "#.0f", "1235."),
        ("1234", "#f", "1234."),
        ("1234.5600", "F", "1234.5600"),
        ("1234.5600", ".2F", "1234.56"),
        ("1234.5600", "+012.2F", "+00001234.56"),
        ("1234.5600", ",.2F", "1,234.56"),
        ("1234.5600", "#.0F", "1235."),
        ("1234", "#.0%", "123400.%"),
        ("0.001", "#%", "0.1%"),
        ("1234.5600", "e", "1.2345600e+3"),
        ("0.0012345600", "e", "1.2345600e-3"),
        ("-0.0012345600", "e", "-1.2345600e-3"),
        ("10.00", "E", "1.000E+1"),
        ("0.0000", "e", "0e-4"),
        ("0.0000", ".2e", "0.00e-2"),
        ("999.9500", ".2e", "1.00e+3"),
        ("1234.5600", ".2E", "1.23E+3"),
        ("1234.5600", "+012.2e", "+00001.23e+3"),
        ("-1234.5600", "+012.2e", "-00001.23e+3"),
        ("1234.5600", ",.2e", "1.23e+3"),
        ("1234.5600", "#.0e", "1.e+3"),
        ("1", "#e", "1.e+0"),
        ("1234.5600", "g", "1234.5600"),
        ("0.0000", ".3g", "0.0000"),
        ("10.00", ".3g", "10.0"),
        ("1234.5600", ".0g", "1e+3"),
        ("0.001234", ".0g", "0.001"),
        ("9.9500", ".0g", "1e+1"),
        ("1234.5600", "#.0g", "1.e+3"),
        ("0.001234", "#.0g", "0.001"),
        ("1000", ".3g", "1.00e+3"),
        ("1234.5600", ".3G", "1.23E+3"),
        ("1234.5600", ".0G", "1E+3"),
        ("9.9500", ".0G", "1E+1"),
        ("1234.5600", "#.0G", "1.E+3"),
        ("0.0012345600", ".3g", "0.00123"),
        ("0.00012345600", ".3g", "0.000123"),
        ("999.9500", ".3g", "1.00e+3"),
        ("1234.5600", "+012.3g", "+00001.23e+3"),
        ("0.0012345600", "+012.3g", "+00000.00123"),
        ("1234.5600", ",.3g", "1.23e+3"),
        ("1", "#g", "1."),
        ("10.00", "#.3g", "10.0"),
        ("1234.5600", "n", "1234.5600"),
        ("10.00", ".3n", "10.0"),
        ("1000", ".3n", "1.00e+3"),
        ("1234.5600", ".3n", "1.23e+3"),
        ("0.0012345600", ".3n", "0.00123"),
        ("999.9500", ".3n", "1.00e+3"),
        ("1234.5600", ".0n", "1e+3"),
        ("0.001234", ".0n", "0.001"),
        ("1234.5600", "#.0n", "1.e+3"),
        ("1234.5600", "+012.3n", "+00001.23e+3"),
        ("0.0012345600", "+012.3n", "+00000.00123"),
        pytest.param(
            "1234.5600",
            "N",
            "1234.5600",
            marks=pytest.mark.filterwarnings(
                "ignore:Format specifier 'N' is deprecated"
            ),
        ),
        pytest.param(
            "1234.5600",
            ".3N",
            "1.23E+3",
            marks=pytest.mark.filterwarnings(
                "ignore:Format specifier 'N' is deprecated"
            ),
        ),
        pytest.param(
            "1234.5600",
            ".0N",
            "1E+3",
            marks=pytest.mark.filterwarnings(
                "ignore:Format specifier 'N' is deprecated"
            ),
        ),
        pytest.param(
            "1234.5600",
            "+012.3N",
            "+00001.23E+3",
            marks=pytest.mark.filterwarnings(
                "ignore:Format specifier 'N' is deprecated"
            ),
        ),
        ("1", "#n", "1."),
        ("10.00", "#.3n", "10.0"),
        ("1234.5600", "*=+12.3n", "+****1.23e+3"),
        ("3.1400", ".3", "3.14"),
        ("3.1400", "08.3", "00003.14"),
        ("1234567.0", ".3", "1.23E+6"),
        ("1234567.0", "08.3", "01.23E+6"),
        ("1E+20", "#.2", "1.0E+20"),
        ("1E+20", "08,.3", "1.00E+20"),
        ("1E-7", "#.2", "1.E-7"),
        ("1E-7", "08,.3", "0,001E-7"),
        ("-0.0000", "z08,.2", "000.0000"),
        ("1234.5600", "<12.2f", "1234.56     "),
        ("1234.5600", ">12.2f", "     1234.56"),
        ("1234.5600", "^12.2f", "  1234.56   "),
        ("1234.5600", "*<12.2f", "1234.56*****"),
        ("1234.5600", "*>12.2f", "*****1234.56"),
        ("1234.5600", "*^12.2f", "**1234.56***"),
        ("1234.5600", "<+12.2f", "+1234.56    "),
        ("1234.5600", "<12,.2f", "1,234.56    "),
        ("1234.5600", "^12.3g", "  1.23e+3   "),
        ("1234.5600", "^12.2%", " 123456.00% "),
        ("1234.5600", "<=12.2f", "<<<<<1234.56"),
        ("1234.5600", "><12.2f", "1234.56>>>>>"),
        ("1234.5600", "^=12.2f", "^^^^^1234.56"),
        ("1234.5600", "==12.2f", "=====1234.56"),
        ("1234.5600", "-z12.2f", "     1234.56"),
        ("1234.5600", "z#12.2f", "     1234.56"),
        ("1234.5600", "0<12.2f", "1234.5600000"),
        ("1234.5600", "0^12.2f", "001234.56000"),
        ("1234.5600", "0>12.2f", "000001234.56"),
        ("1234.5600", "12,.2g", "      1.2e+3"),
        ("1234.5600", "12,.2e", "     1.23e+3"),
        ("1234.5600", "12,.2%", " 123,456.00%"),
        ("1234.5600", "12,", "  1,234.5600"),
        ("3.1400", "*^12.2f", "****3.14****"),
        ("1234.56", "=+12.2f", "+    1234.56"),
        ("-1234.56", "=+12.2f", "-    1234.56"),
        ("1234.56", "*=+12.2f", "+****1234.56"),
        ("1234.56", "0=+12.2f", "+00001234.56"),
        ("-1234.56", "0=+12.2f", "-00001234.56"),
        ("-1234.56", "*=+12.2e", "-****1.23e+3"),
        ("1234.56", "=+12.2g", "+     1.2e+3"),
        ("1234.56", "*=+12.2%", "+*123456.00%"),
        ("0.00", "z.2f", "0.00"),
        ("0.00", "+z012.2f", "+00000000.00"),
        ("-1.20", "z.2f", "-1.20"),
        ("0.00", "ze", "0e-2"),
        ("0.00", "z.2e", "0.00e+0"),
        ("0.00", "z%", "0%"),
    ],
)
def test_spawn_task_for_each_row_decimal_format_rounds_like_python_decimal(
    value: str, spec: str, expected: str
) -> None:
    room = _FakeRoom()
    decimal_value = Decimal(value)
    scale = max(0, -decimal_value.as_tuple().exponent)
    decimal_type = pa.decimal128(24, scale)
    tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={"price": decimal_type},
        prompt="{price:" + spec + "}",
        queue="jobs",
    )
    row = pa.Table.from_pylist(
        [{"price": decimal_value}],
        schema=pa.schema([pa.field("price", decimal_type)]),
    ).to_pylist()[0]

    assert tool.make_message(context=_tool_context(room), row=row)["prompt"] == expected


@pytest.mark.parametrize(
    "spec",
    [
        "_.2f",
        "_.2%",
        "_.2e",
        "_.3g",
        ",.3n",
        "_.3n",
        "=012.2f",
        "q",
        "#q",
        "z+12.2f",
        "+zz.2f",
        pytest.param(
            "#N",
            marks=pytest.mark.filterwarnings(
                "ignore:Format specifier 'N' is deprecated"
            ),
        ),
        pytest.param(
            ",.3N",
            marks=pytest.mark.filterwarnings(
                "ignore:Format specifier 'N' is deprecated"
            ),
        ),
        pytest.param(
            "_.3N",
            marks=pytest.mark.filterwarnings(
                "ignore:Format specifier 'N' is deprecated"
            ),
        ),
        ".",
        ".f",
        ".xf",
        "1.2.3f",
        "00f",
        "00.2f",
        "**12.2f",
        "***12.2f",
        "+-12.2f",
        "-+12.2f",
        " +12.2f",
        "+ 12.2f",
        "z-12.2f",
        "#z12.2f",
        "00=12.2f",
        "12,.2n",
        "12_.2f",
        ",12.2f",
        "_12.2f",
        "12_",
        "12,,.2f",
        "12__.2f",
        "12,_ .2f",
        "12,_f",
        "12_,f",
        ",,f",
        "__f",
        ",_f",
        "_,f",
    ],
)
def test_spawn_task_for_each_row_decimal_format_errors_match_python_decimal(
    spec: str,
) -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={"price": pa.decimal128(12, 4)},
        prompt="{price:" + spec + "}",
        queue="jobs",
    )
    row = pa.Table.from_pylist(
        [{"price": Decimal("1234.5600")}],
        schema=pa.schema([pa.field("price", pa.decimal128(12, 4))]),
    ).to_pylist()[0]

    with pytest.raises(ValueError, match="invalid format string"):
        tool.make_message(context=_tool_context(room), row=row)


@pytest.mark.parametrize(
    "spec",
    [
        "999999999999999999999999999999f",
        ".999999999999999999999999999999f",
    ],
)
def test_spawn_task_for_each_row_decimal_format_overflow_errors_match_python_decimal(
    spec: str,
) -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="events",
        schema={"price": pa.decimal128(12, 4)},
        prompt="{price:" + spec + "}",
        queue="jobs",
    )
    row = pa.Table.from_pylist(
        [{"price": Decimal("1234.5600")}],
        schema=pa.schema([pa.field("price", pa.decimal128(12, 4))]),
    ).to_pylist()[0]

    with pytest.raises(
        OverflowError, match="cannot fit 'int' into an index-sized integer"
    ):
        tool.make_message(context=_tool_context(room), row=row)


def test_spawn_task_for_each_row_prompt_format_nested_specs_compose_like_python() -> (
    None
):
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64()},
        prompt=(
            "{id:{fill}{align}{width}d}|{score:{spec}}|"
            "{id:0{width}d}|{id:{align}{width}d}|"
            "{id:{zero}{width}d}|{id:{zero}>{width}d}|"
            "{id:{zero}={width},d}|{id:{sign}{zero}{width}d}|"
            "{id:{hash}{zero}{width}x}|{id:{spec_obj[x]}}|"
            "{id:{spec_list[0]}}|{score:{zero}{width}.{precision}f}|"
            "{score:{zero}{width}{comma}.{precision}f}|"
            "{score:{zero}={width}{comma}.{precision}f}|"
            "{score:{width}.{precision}}|{score:{width}.{precision}g}|"
            "{name:{brace}>{width}}|{name:{fill}{align}{width}.{precision}}|"
            "{name:{fill}{align}{width}s}|{id:{empty}}|{id:{width!a}}"
        ),
        queue="jobs",
    )

    assert tool.make_message(
        context=_tool_context(room),
        row={
            "id": 7,
            "score": 3.14159,
            "fill": "*",
            "align": ">",
            "width": 8,
            "precision": 3,
            "spec": ".2f",
            "zero": "0",
            "sign": "+",
            "hash": "#",
            "comma": ",",
            "brace": "{",
            "empty": "",
            "spec_obj": {"x": "04d"},
            "spec_list": ["04d"],
            "name": "Alice",
        },
    ) == {
        "prompt": (
            "*******7|3.14|00000007|       7|00000007|00000007|"
            "0,000,007|+0000007|0x000007|0007|0007|0003.142|"
            "0,003.142|0,003.142|    3.14|    3.14|{{{Alice|"
            "*****Ali|***Alice|7|       7"
        ),
        "row": {
            "id": 7,
            "score": 3.14159,
            "fill": "*",
            "align": ">",
            "width": 8,
            "precision": 3,
            "spec": ".2f",
            "zero": "0",
            "sign": "+",
            "hash": "#",
            "comma": ",",
            "brace": "{",
            "empty": "",
            "spec_obj": {"x": "04d"},
            "spec_list": ["04d"],
            "name": "Alice",
        },
    }


def test_spawn_task_for_each_row_prompt_format_float_z_sign_matches_python() -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64()},
        prompt="{negzero:z.2f}|{negzero:+z08.2f}|{negzero: z.2f}|{negzero:z.2e}|{negzero:z.2g}|{negzero:z%}|{score:z.2f}|{poszero:+z08.2f}|{amount:F}|{amount:.2F}|{amount:+08.2F}|{amount:,.2F}|{amount:#.0F}",
        queue="jobs",
    )

    assert tool.make_message(
        context=_tool_context(room),
        row={"negzero": -0.0, "poszero": 0.0, "score": -1.25, "amount": 1234.56},
    ) == {
        "prompt": "0.00|+0000.00| 0.00|0.00e+00|0|0.000000%|-1.25|+0000.00|1234.560000|1234.56|+1234.56|1,234.56|1235.",
        "row": {"negzero": -0.0, "poszero": 0.0, "score": -1.25, "amount": 1234.56},
    }


@pytest.mark.parametrize("prompt", ["{}", "{01}", "{[id]}"])
def test_spawn_task_for_each_row_prompt_format_positional_fields_use_python_errors(
    prompt: str,
) -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64()},
        prompt=prompt,
        queue="jobs",
    )

    with pytest.raises(IndexError, match="Replacement index .* out of range"):
        tool.make_message(context=_tool_context(room), row={"id": 7, "01": "ignored"})


@pytest.mark.parametrize(
    ("prompt", "message"),
    [
        ("{id!s:04d}", "Unknown format code 'd' for object of type 'str'"),
        ("{name!z}", "Unknown conversion specifier z"),
        ("{name!rr}", "expected ':' after conversion specifier"),
        ("{!}", "unmatched '\\{' in format spec"),
        ("{name!}", "unmatched '\\{' in format spec"),
    ],
)
def test_spawn_task_for_each_row_prompt_format_conversion_errors_match_python(
    prompt: str,
    message: str,
) -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64(), "name": pa.string()},
        prompt=prompt,
        queue="jobs",
    )

    with pytest.raises(ValueError, match=message):
        tool.make_message(context=_tool_context(room), row={"id": 7, "name": "Alice"})


def test_spawn_task_for_each_row_prompt_format_nested_specs_match_python_limit() -> (
    None
):
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64()},
        prompt="{id:{width:{precision}}}",
        queue="jobs",
    )

    with pytest.raises(ValueError, match="Max string recursion exceeded"):
        tool.make_message(
            context=_tool_context(room),
            row={"id": 7, "width": 8, "precision": 3},
        )


@pytest.mark.parametrize(
    ("prompt", "error_type", "message"),
    [
        (
            "{name:{{}}}",
            ValueError,
            "Invalid format specifier '\\{\\}' for object of type 'str'",
        ),
        (
            "{name:{{{width}}}}",
            ValueError,
            "Invalid format specifier '\\{8\\}' for object of type 'str'",
        ),
        (
            "{id:{{spec}}}",
            ValueError,
            "Invalid format specifier '\\{spec\\}' for object of type 'int'",
        ),
        (
            "{id:{literal}}",
            ValueError,
            "Invalid format specifier '\\{width\\}' for object of type 'int'",
        ),
        ("{id:{}}", IndexError, "Replacement index 0 out of range"),
        ("{id:{:{width}}}", IndexError, "Replacement index 0 out of range"),
        (
            "{id:{name!r}}",
            ValueError,
            "Invalid format specifier ''Alice'' for object of type 'int'",
        ),
        (
            "{id:{name!s}}",
            ValueError,
            "Invalid format specifier 'Alice' for object of type 'int'",
        ),
        ("{id:{name:{precision}}}", ValueError, "Max string recursion exceeded"),
    ],
)
def test_spawn_task_for_each_row_prompt_format_nested_specs_errors_match_python(
    prompt: str,
    error_type: type[Exception],
    message: str,
) -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64(), "name": pa.string()},
        prompt=prompt,
        queue="jobs",
    )

    with pytest.raises(error_type, match=message):
        tool.make_message(
            context=_tool_context(room),
            row={
                "id": 7,
                "name": "Alice",
                "width": 8,
                "precision": 3,
                "spec": "04d",
                "literal": "{width}",
            },
        )


@pytest.mark.parametrize(
    ("prompt", "error_type", "message"),
    [
        ("{id:.2d}", ValueError, "Precision not allowed in integer format specifier"),
        ("{id:.2}", ValueError, "Precision not allowed in integer format specifier"),
        ("{id:.}", ValueError, "Format specifier missing precision"),
        ("{score:.f}", ValueError, "Format specifier missing precision"),
        ("{flag:.d}", ValueError, "Format specifier missing precision"),
        ("{score:d}", ValueError, "Unknown format code 'd' for object of type 'float'"),
        ("{score:x}", ValueError, "Unknown format code 'x' for object of type 'float'"),
        ("{score:X}", ValueError, "Unknown format code 'X' for object of type 'float'"),
        ("{score:b}", ValueError, "Unknown format code 'b' for object of type 'float'"),
        ("{score:o}", ValueError, "Unknown format code 'o' for object of type 'float'"),
        (
            "{letter:+c}",
            ValueError,
            "Sign not allowed with integer format specifier 'c'",
        ),
        (
            "{letter:#c}",
            ValueError,
            "Alternate form \\(#\\) not allowed with integer format specifier 'c'",
        ),
        ("{letter:,c}", ValueError, "Cannot specify ',' with 'c'."),
        ("{letter:_c}", ValueError, "Cannot specify '_' with 'c'."),
        ("{flag:+c}", ValueError, "Sign not allowed with integer format specifier 'c'"),
        (
            "{none:>8}",
            TypeError,
            "unsupported format string passed to NoneType.__format__",
        ),
        (
            "{user:>8}",
            TypeError,
            "unsupported format string passed to dict.__format__",
        ),
        (
            "{items:>8}",
            TypeError,
            "unsupported format string passed to list.__format__",
        ),
        (
            "{name:=8}",
            ValueError,
            "'=' alignment not allowed in string format specifier",
        ),
        ("{name:.}", ValueError, "Format specifier missing precision"),
        ("{name:8.x}", ValueError, "Format specifier missing precision"),
        (
            "{name:999999999999999999999999999999}",
            ValueError,
            "Too many decimal digits in format string",
        ),
        (
            "{name:.999999999999999999999999999999}",
            ValueError,
            "Too many decimal digits in format string",
        ),
        (
            "{id:999999999999999999999999999999d}",
            ValueError,
            "Too many decimal digits in format string",
        ),
        (
            "{id:.999999999999999999999999999999d}",
            ValueError,
            "Too many decimal digits in format string",
        ),
        (
            "{score:999999999999999999999999999999f}",
            ValueError,
            "Too many decimal digits in format string",
        ),
        (
            "{score:.999999999999999999999999999999f}",
            ValueError,
            "Too many decimal digits in format string",
        ),
        ("{score:c}", ValueError, "Unknown format code 'c' for object of type 'float'"),
        ("{neg:c}", OverflowError, "%c arg not in range"),
        ("{flag:s}", ValueError, "Unknown format code 's' for object of type 'bool'"),
        ("{flag:.2d}", ValueError, "Precision not allowed in integer format specifier"),
        ("{big:_n}", ValueError, "Cannot specify '_' with 'n'."),
        ("{big:,n}", ValueError, "Cannot specify ',' with 'n'."),
        ("{id:,_d}", ValueError, "Cannot specify both ',' and '_'."),
        (
            "{id:__d}",
            ValueError,
            "Invalid format specifier '__d' for object of type 'int'",
        ),
        (
            "{id:,,d}",
            ValueError,
            "Invalid format specifier ',,d' for object of type 'int'",
        ),
        (
            "{flag:__d}",
            ValueError,
            "Invalid format specifier '__d' for object of type 'bool'",
        ),
        ("{id:,x}", ValueError, "Cannot specify ',' with 'x'."),
        ("{id:,b}", ValueError, "Cannot specify ',' with 'b'."),
        ("{id:,o}", ValueError, "Cannot specify ',' with 'o'."),
        (
            "{id:z}",
            ValueError,
            "Negative zero coercion \\(z\\) not allowed in integer format specifier",
        ),
        (
            "{id:zx}",
            ValueError,
            "Negative zero coercion \\(z\\) not allowed in integer format specifier",
        ),
        (
            "{flag:z}",
            ValueError,
            "Negative zero coercion \\(z\\) not allowed in integer format specifier",
        ),
        ("{id:z.2d}", ValueError, "Precision not allowed in integer format specifier"),
        ("{score:N}", ValueError, "Unknown format code 'N' for object of type 'float'"),
        ("{id:N}", ValueError, "Unknown format code 'N' for object of type 'int'"),
        ("{flag:N}", ValueError, "Unknown format code 'N' for object of type 'bool'"),
    ],
)
def test_spawn_task_for_each_row_prompt_format_numeric_errors_match_python(
    prompt: str,
    error_type: type[Exception],
    message: str,
) -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64(), "name": pa.string()},
        prompt=prompt,
        queue="jobs",
    )

    with pytest.raises(error_type, match=message):
        tool.make_message(
            context=_tool_context(room),
            row={
                "id": 7,
                "neg": -1,
                "score": 3.14159,
                "name": "Alice",
                "none": None,
                "flag": True,
                "big": 1234567,
                "letter": 65,
                "user": {"name": "Alice"},
                "items": [1, 2],
            },
        )


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
