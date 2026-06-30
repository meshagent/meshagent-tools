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


def test_spawn_task_for_each_row_prompt_format_uses_python_format_specs() -> None:
    room = _FakeRoom()
    tool = SpawnTaskForEachRow(
        room=room,
        table="users",
        schema={"id": pa.int64(), "name": pa.string()},
        prompt="{id:04d}|{score:.2f}|{score:08.2f}|{id:+d}|{id: d}|{neg:04d}|{id:x}|{id:#04x}|{id:b}|{id:o}|{id:X}|{score:e}|{name!r}|{nonascii!r}|{nonascii!a}|{none!s}|{{{name}}}|{name:8}|{name:>8}|{name:<8}|{name:^8}|{name:*^8}|{name:.3}|{name:s}|{name:>8s}|{flag!r}|{flag:d}|{flag:04d}|{flag:b}|{flag:x}|{flag:f}|{flag:>8}|{id:<8d}|{id:^8d}|{score:<8.2f}|{name:>{width}}|{score:.{precision}f}|{id:{int_spec}}|{name:{name_spec}}|{name:08}|{name!r:>10}|{flag!s:^6}|{score!r:.4}|{name!r:>{width}}|{id:{width!r}}|{big:,d}|{big:_d}|{big:,}|{big:_}|{id:=+6d}|{neg:=06d}|{id:*=+6d}|{id:*=#8x}|{score:g}|{score:.2g}|{score:G}|{small:.2g}|{bigfloat:.3g}|{score:%}|{score:.1%}|{score:08.1%}|{negscore:+08.1%}|{letter:c}|{letter:4c}|{letter:^5c}|{big:n}|{score:n}|{whole:#g}|{whole:#.2g}|{whole:#.0f}|{whole:#.0e}|{bigfloat:,.1f}|{bigfloat:_.1f}|{bigfloat:,.8g}|{bigfloat:_.8g}|{bigfloat:,%}",
        queue="jobs",
        namespace=["prod"],
    )

    assert tool.make_message(
        context=_tool_context(room),
        row={
            "id": 7,
            "name": "Alice",
            "nonascii": "é",
            "score": 3.14159,
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
        "prompt": "0007|3.14|00003.14|+7| 7|-007|7|0x07|111|7|7|3.141590e+00|'Alice'|'é'|'\\xe9'|None|{Alice}|Alice   |   Alice|Alice   | Alice  |*Alice**|Ali|Alice|   Alice|True|1|0001|1|1|1.000000|       1|7       |   7    |3.14    |   Alice|3.142|0007|*Alice**|Alice000|   'Alice'| True |3.14| 'Alice'|       7|1,234,567|1_234_567|1,234,567|1_234_567|+    7|-00007|+****7|0x*****7|3.14159|3.1|3.14159|0.00012|1.23e+06|314.159000%|314.2%|00314.2%|-0314.2%|A|   A|  A  |1234567|3.14159|3.00000|3.0|3.|3.e+00|1,234,567.0|1_234_567.0|1,234,567|1_234_567|123,456,700.000000%",
        "row": {
            "id": 7,
            "name": "Alice",
            "nonascii": "é",
            "score": 3.14159,
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
        },
        prompt="{event_date}|{event_date!r}|{event_date.year:04d}-{event_date.month:02d}-{event_date.day:02d}|{event_date:%a|%A|%b|%B|%j|%w|%u|%U|%W|%I|%p|%y|%x|%X}|{created_at}|{created_at!r}|{created_at.year:04d}-{created_at.month:02d}-{created_at.day:02d}T{created_at.hour:02d}:{created_at.minute:02d}:{created_at.second:02d}.{created_at.microsecond:06d}|{created_at.tzinfo}|{created_at.tzinfo!r}|{created_at.tzinfo.zone}|{created_at.fold}|{created_at:%Y/%m/%d %H:%M:%S.%f %z %Z|%a|%b|%j|%I|%p}|{price}|{price!r}|{price:f}|{price:.2f}|{price:,f}|{price:,.2f}|{price:+012.2f}|{price:n}|{payload}|{payload!r}",
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

    assert tool.make_message(context=_tool_context(room), row=row) == {
        "prompt": "2026-04-09|datetime.date(2026, 4, 9)|2026-04-09|Thu|Thursday|Apr|April|099|4|4|14|14|12|AM|26|04/09/26|00:00:00|2026-04-09 12:30:45.123456+00:00|datetime.datetime(2026, 4, 9, 12, 30, 45, 123456, tzinfo=<UTC>)|2026-04-09T12:30:45.123456|UTC|<UTC>|UTC|0|2026/04/09 12:30:45.123456 +0000 UTC|Thu|Apr|099|12|PM|1234.5600|Decimal('1234.5600')|1234.5600|1234.56|1,234.5600|1,234.56|+00001234.56|1234.5600|b'\\x00\\x01\\xfa\\xff'|b'\\x00\\x01\\xfa\\xff'",
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
        ("1000", ".3g", "1.00e+3"),
        ("1234.5600", ".3G", "1.23E+3"),
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
        ("1234.5600", "+012.3n", "+00001.23e+3"),
        ("0.0012345600", "+012.3n", "+00000.00123"),
        ("1", "#n", "1."),
        ("10.00", "#.3n", "10.0"),
        ("1234.5600", "*=+12.3n", "+****1.23e+3"),
        ("1234.56", "=+12.2f", "+    1234.56"),
        ("-1234.56", "=+12.2f", "-    1234.56"),
        ("1234.56", "*=+12.2f", "+****1234.56"),
        ("-1234.56", "*=+12.2e", "-****1.23e+3"),
        ("1234.56", "=+12.2g", "+     1.2e+3"),
        ("1234.56", "*=+12.2%", "+*123456.00%"),
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
    "spec", ["_.2f", "_.2%", "_.2e", "_.3g", ",.3n", "_.3n", "=012.2f"]
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
        ("{id:.2d}", ValueError, "Precision not allowed in integer format specifier"),
        ("{id:.2}", ValueError, "Precision not allowed in integer format specifier"),
        ("{score:d}", ValueError, "Unknown format code 'd' for object of type 'float'"),
        (
            "{none:>8}",
            TypeError,
            "unsupported format string passed to NoneType.__format__",
        ),
        (
            "{name:=8}",
            ValueError,
            "'=' alignment not allowed in string format specifier",
        ),
        ("{score:c}", ValueError, "Unknown format code 'c' for object of type 'float'"),
        ("{neg:c}", OverflowError, "%c arg not in range"),
        ("{flag:s}", ValueError, "Unknown format code 's' for object of type 'bool'"),
        ("{flag:.2d}", ValueError, "Precision not allowed in integer format specifier"),
        ("{big:_n}", ValueError, "Cannot specify '_' with 'n'."),
        ("{big:,n}", ValueError, "Cannot specify ',' with 'n'."),
        ("{id:,_d}", ValueError, "Cannot specify both ',' and '_'."),
        ("{id:,x}", ValueError, "Cannot specify ',' with 'x'."),
        ("{id:,b}", ValueError, "Cannot specify ',' with 'b'."),
        ("{id:,o}", ValueError, "Cannot specify ',' with 'o'."),
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
