from .tool import LocalRoomTool
from .toolkit import ToolContext, Toolkit
from typing import Any, Optional
import pyarrow as pa
from meshagent.api.room_server_client import (
    decode_records,
    DatasetSqlQuery,
    DatasetSqlStatement,
    RoomClient,
)

import logging

logger = logging.getLogger("dataset_toolkit")

_EXPRESSION_JSON_SCHEMA = {
    "type": "object",
    "required": ["expression"],
    "additionalProperties": False,
    "properties": {
        "expression": {
            "type": "string",
            "description": "a DataFusion / Lance SQL expression",
        }
    },
}


_JSON_VALUE_SCHEMA: dict[str, Any] = {
    "anyOf": [
        {"type": "object"},
        {"type": "array"},
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
    ]
}


def _wrapped_dataset_value_json_schema(
    *,
    wrapper: str,
    payload_schema: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "object",
        "required": [wrapper],
        "additionalProperties": False,
        "properties": {
            wrapper: payload_schema,
        },
    }


type DatasetToolSchemaValue = pa.DataType | pa.Field


def _dataset_rows(table: pa.Table) -> list[dict[str, Any]]:
    return table.to_pylist()


def _describe_schema_value(name: str, value: DatasetToolSchemaValue) -> str:
    if isinstance(value, pa.Field):
        return f"column {name} => {value}"
    return f"column {name} => {value}"


def _tool_input_schema_for_data_type(
    schema_value: DatasetToolSchemaValue,
    *,
    allow_expression: bool = False,
) -> dict[str, Any]:
    if isinstance(schema_value, pa.Field):
        data_type = schema_value.type
        nullable = schema_value.nullable
    else:
        data_type = schema_value
        nullable = True

    variants = list[dict[str, Any]]()
    if pa.types.is_binary(data_type) or pa.types.is_large_binary(data_type):
        variants.append(
            _wrapped_dataset_value_json_schema(
                wrapper="binary",
                payload_schema={
                    "type": "string",
                    "description": "a base64 encoded byte string",
                },
            )
        )
    elif pa.types.is_date(data_type):
        variants.append(
            _wrapped_dataset_value_json_schema(
                wrapper="date",
                payload_schema={
                    "type": "string",
                    "description": "an ISO formatted date string",
                },
            )
        )
    elif pa.types.is_timestamp(data_type):
        variants.append(
            _wrapped_dataset_value_json_schema(
                wrapper="timestamp",
                payload_schema={
                    "type": "string",
                    "description": "an ISO formatted timestamp string",
                },
            )
        )
    elif data_type == pa.uuid():
        variants.append(
            _wrapped_dataset_value_json_schema(
                wrapper="uuid",
                payload_schema={
                    "type": "string",
                    "description": "a UUID string",
                },
            )
        )
    elif (
        pa.types.is_list(data_type)
        or pa.types.is_large_list(data_type)
        or pa.types.is_fixed_size_list(data_type)
    ):
        variants.append(
            _wrapped_dataset_value_json_schema(
                wrapper="list",
                payload_schema={"type": "array"},
            )
        )
    elif pa.types.is_struct(data_type):
        variants.append(
            _wrapped_dataset_value_json_schema(
                wrapper="struct",
                payload_schema={"type": "object"},
            )
        )
    elif hasattr(pa, "json_") and data_type == pa.json_():
        variants.append(
            _wrapped_dataset_value_json_schema(
                wrapper="json",
                payload_schema=_JSON_VALUE_SCHEMA,
            )
        )
    elif pa.types.is_boolean(data_type):
        variants.append({"type": "boolean"})
    elif pa.types.is_integer(data_type) or pa.types.is_floating(data_type):
        variants.append({"type": "number"})
    elif pa.types.is_string(data_type) or pa.types.is_large_string(data_type):
        variants.append({"type": "string"})
    else:
        variants.append(_JSON_VALUE_SCHEMA)
    if allow_expression:
        variants.append(_EXPRESSION_JSON_SCHEMA)
    if nullable:
        variants.append({"type": "null"})
    if len(variants) == 1:
        return variants[0]
    return {"anyOf": variants}


def _normalize_dataset_tool_record(record: dict[str, Any]) -> dict[str, Any]:
    return decode_records([record])[0]


def _normalize_dataset_tool_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return decode_records(rows)


class _DatasetTool(LocalRoomTool):
    pass


class ListTablesTool(_DatasetTool):
    def __init__(self, *, room: RoomClient):
        input_schema = {
            "type": "object",
            "required": [],
            "additionalProperties": False,
            "properties": {},
        }

        super().__init__(
            room=room,
            name="list_tables",
            title="list tables",
            description="list the tables in the room",
            input_schema=input_schema,
        )

    async def execute(self, context: ToolContext):
        del context
        return {"tables": await self.room.datasets.list_tables()}


class InsertRowsTool(_DatasetTool):
    def __init__(
        self,
        *,
        room: RoomClient,
        table: str,
        schema: dict[str, DatasetToolSchemaValue],
        namespace: Optional[list[str]] = None,
    ):
        self.table = table
        self.namespace = namespace

        input_schema = {
            "type": "object",
            "required": [],
            "additionalProperties": False,
            "properties": {},
        }

        for k, v in schema.items():
            input_schema["required"].append(k)
            input_schema["properties"][k] = _tool_input_schema_for_data_type(
                v,
                allow_expression=True,
            )

        super().__init__(
            room=room,
            name=f"insert_{table}_rows",
            title=f"insert {table} rows",
            description=f"insert rows into the {table} table",
            input_schema={
                "type": "object",
                "required": ["rows"],
                "additionalProperties": False,
                "properties": {"rows": {"type": "array", "items": input_schema}},
            },
        )

    async def execute(self, context: ToolContext, *, rows):
        del context
        await self.room.datasets.insert(
            table=self.table,
            records=_normalize_dataset_tool_rows(rows),
            namespace=self.namespace,
        )


class DeleteRowsTool(_DatasetTool):
    def __init__(
        self,
        *,
        room: RoomClient,
        table: str,
        schema: dict[str, DatasetToolSchemaValue],
        namespace: Optional[list[str]] = None,
    ):
        self.table = table
        self.namespace = namespace

        input_schema = {
            "type": "object",
            "required": [],
            "additionalProperties": False,
            "properties": {},
        }

        for k, v in schema.items():
            input_schema["required"].append(k)
            input_schema["properties"][k] = _tool_input_schema_for_data_type(v)

        super().__init__(
            room=room,
            name=f"delete_{table}_rows",
            title=f"delete {table} rows",
            description=f"delete from {table} where rows match the specified values (specify null for a column to exclude it from the search)",
            input_schema=input_schema,
        )

    async def execute(self, context: ToolContext, **values):
        del context
        search = {}

        for k, v in values.items():
            if v is not None:
                search[k] = v
        if search:
            search = _normalize_dataset_tool_record(search)

        await self.room.datasets.delete(
            table=self.table,
            where=search if len(search) > 0 else None,
            namespace=self.namespace,
        )
        return {"ok": True}


class UpdateTool(_DatasetTool):
    def __init__(
        self,
        *,
        room: RoomClient,
        table: str,
        schema: dict[str, DatasetToolSchemaValue],
        namespace: Optional[list[str]] = None,
    ):
        self.table = table
        self.namespace = namespace

        columns = ""

        for k, v in schema.items():
            columns += _describe_schema_value(k, v)

        anyOf = []

        for k, v in schema.items():
            anyOf.append(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [k],
                    "properties": {
                        k: _tool_input_schema_for_data_type(
                            v,
                            allow_expression=True,
                        )
                    },
                }
            )

        input_schema = {
            "type": "object",
            "required": [
                "where",
                "values",
            ],
            "additionalProperties": False,
            "properties": {
                "where": {
                    "type": "string",
                    "description": f"a lance db compatible filter, columns are: {columns}",
                },
                "values": {
                    "type": "array",
                    "description": "a list of columns to update",
                    "items": {"anyOf": anyOf},
                },
            },
        }

        super().__init__(
            room=room,
            name=f"update_{table}_rows",
            title=f"update {table} rows",
            description=f"update {table} table where rows match the specified filter (with a lancedb compatible filter)",
            input_schema=input_schema,
        )

    async def execute(self, context: ToolContext, *, where: str, values: list[dict]):
        del context
        set = {}
        for value in values:
            for k, v in value.items():
                set[k] = v
        set = _normalize_dataset_tool_record(set)

        await self.room.datasets.update(
            table=self.table, where=where, values=set, namespace=self.namespace
        )

        return {"ok": True}


class SearchTool(_DatasetTool):
    def __init__(
        self,
        *,
        room: RoomClient,
        table: str,
        schema: dict[str, DatasetToolSchemaValue],
        namespace: Optional[list[str]] = None,
    ):
        self.table = table
        self.namespace = namespace

        query = {
            "type": "object",
            "required": [],
            "additionalProperties": False,
            "properties": {},
        }

        for k, v in schema.items():
            query["required"].append(k)
            query["properties"][k] = _tool_input_schema_for_data_type(v)

        input_schema = {
            "type": "object",
            "required": ["query", "limit", "offset", "select"],
            "additionalProperties": False,
            "properties": {
                "query": query,
                "select": {
                    "type": "array",
                    "description": f"the columns to return, available columns: {','.join(schema.keys())}",
                    "items": {
                        "type": "string",
                    },
                },
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
        }

        super().__init__(
            room=room,
            name=f"search_{table}",
            title=f"search {table}",
            description=f"search {table} table for rows with the specified values (specify null for a column to exclude it from the search)",
            input_schema=input_schema,
        )

    async def execute(
        self,
        context: ToolContext,
        query: object,
        limit: int,
        offset: int,
        select: list[str],
    ):
        del context
        search = {}

        for k, v in query.items():
            if v is not None:
                search[k] = v
        if search:
            search = _normalize_dataset_tool_record(search)

        return {
            "rows": _dataset_rows(
                await self.room.datasets.search(
                    select=select,
                    table=self.table,
                    where=search if len(search) > 0 else None,
                    namespace=self.namespace,
                    offset=offset,
                    limit=limit,
                )
            )
        }


class LLMSearchTool(_DatasetTool):
    def __init__(
        self,
        *,
        room: RoomClient,
        table: str,
        schema: dict[str, DatasetToolSchemaValue],
        namespace: Optional[list[str]] = None,
    ):
        self.table = table
        self.namespace = namespace

        query = {
            "type": "object",
            "required": [],
            "additionalProperties": False,
            "properties": {},
        }

        for k, v in schema.items():
            query["required"].append(k)
            query["properties"][k] = _tool_input_schema_for_data_type(v)

        input_schema = {
            "type": "object",
            "required": ["query", "limit", "offset", "select"],
            "additionalProperties": False,
            "properties": {
                "query": query,
                "select": {
                    "type": "array",
                    "description": f"the columns to return, available columns: {','.join(schema.keys())}",
                    "items": {
                        "type": "string",
                    },
                },
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
        }

        super().__init__(
            room=room,
            name=f"search_{table}",
            title=f"search {table}",
            description=f"search {table} table for rows with the specified values (specify null for a column to exclude it from the search)",
            input_schema=input_schema,
        )

    async def execute(
        self,
        context: ToolContext,
        query: object,
        limit: int,
        offset: int,
        select: list[str],
    ):
        del context
        search = {}

        for k, v in query.items():
            if v is not None:
                search[k] = v
        if search:
            search = _normalize_dataset_tool_record(search)

        return {
            "rows": _dataset_rows(
                await self.room.datasets.search(
                    select=select,
                    table=self.table,
                    where=search if len(search) > 0 else None,
                    namespace=self.namespace,
                    offset=offset,
                    limit=limit,
                )
            )
        }


class SpawnTaskForEachRow(_DatasetTool):
    def __init__(
        self,
        *,
        room: RoomClient,
        table: str,
        schema: dict[str, DatasetToolSchemaValue],
        prompt: str,
        queue: str,
        namespace: Optional[list[str]] = None,
        name: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ):
        self.table = table
        self.namespace = namespace
        self.queue = queue
        self.prompt = prompt

        query = {
            "type": "object",
            "required": [],
            "additionalProperties": False,
            "properties": {},
        }

        for k, v in schema.items():
            query["required"].append(k)
            query["properties"][k] = _tool_input_schema_for_data_type(v)

        input_schema = {
            "type": "object",
            "required": ["query", "limit", "offset", "select"],
            "additionalProperties": False,
            "properties": {
                "query": query,
                "select": {
                    "type": "array",
                    "description": f"the columns to return, available columns: {','.join(schema.keys())}",
                    "items": {
                        "type": "string",
                    },
                },
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
        }

        print(input_schema)
        super().__init__(
            room=room,
            name=name or f"spawn_task_for_each_{table}_row",
            title=title or f"Spawn task for each {table} row",
            description=description
            or f"for each result in {table} where rows match the specified values (specify null for a column to exclude it from the search), queue a worker task",
            input_schema=input_schema,
        )

    def make_message(self, *, context: ToolContext, row: dict):
        msg = {
            "prompt": self.prompt.format(**row),
            "row": row,
        }

        return msg

    async def execute(
        self,
        context: ToolContext,
        query: object,
        limit: int,
        offset: int,
        select: list[str],
    ):
        search = {}

        for k, v in query.items():
            if v is not None:
                search[k] = v
        if search:
            search = _normalize_dataset_tool_record(search)

        rows = _dataset_rows(
            await self.room.datasets.search(
                select=select,
                table=self.table,
                where=search if len(search) > 0 else None,
                namespace=self.namespace,
                offset=offset,
                limit=limit,
            )
        )

        logger.info(
            f"spawn_task_for_each_{self.table}_row matched {len(rows)}. adding items to the queue {self.queue}"
        )

        for row in rows:
            await self.room.queues.send(
                name=self.queue, message=self.make_message(context=context, row=row)
            )

        return {f"added {len(row)} items to the queue {self.queue}"}


class CountTool(_DatasetTool):
    def __init__(
        self,
        *,
        room: RoomClient,
        table: str,
        schema: dict[str, DatasetToolSchemaValue],
        namespace: Optional[list[str]] = None,
    ):
        self.table = table
        self.namespace = namespace

        query = {
            "type": "object",
            "required": [],
            "additionalProperties": False,
            "properties": {},
        }

        input_schema = {
            "type": "object",
            "required": ["query"],
            "additionalProperties": False,
            "properties": {
                "query": query,
            },
        }

        for k, v in schema.items():
            query["required"].append(k)
            query["properties"][k] = _tool_input_schema_for_data_type(v)

        super().__init__(
            room=room,
            name=f"count_{table}",
            title=f"count_{table}",
            description=f"count matching rows in the {table} table",
            input_schema=input_schema,
        )

    async def execute(self, context: ToolContext, query: object):
        del context
        search = {}

        for k, v in query.items():
            if v is not None:
                search[k] = v
        if search:
            search = _normalize_dataset_tool_record(search)

        return {
            "rows": await self.room.datasets.count(
                table=self.table,
                where=search if len(search) > 0 else None,
                namespace=self.namespace,
            )
        }


class AdvancedSearchTool(_DatasetTool):
    def __init__(
        self,
        *,
        room: RoomClient,
        table: str,
        schema: dict[str, DatasetToolSchemaValue],
        namespace: Optional[list[str]] = None,
    ):
        self.table = table
        self.namespace = namespace

        columns = ""

        for k, v in schema.items():
            columns += f"{_describe_schema_value(k, v)}\n"

        input_schema = {
            "type": "object",
            "required": ["where"],
            "additionalProperties": False,
            "properties": {
                "where": {
                    "type": "string",
                    "description": f"a lance db compatible filter, columns are: {columns}",
                }
            },
        }

        super().__init__(
            room=room,
            name=f"advanced_search_{table}",
            title=f"advanced search {table}",
            description=f"advanced search {table} table with a lancedb compatible filter",
            input_schema=input_schema,
        )

    async def execute(self, context: ToolContext, *, where: str):
        del context
        return {
            "rows": _dataset_rows(
                await self.room.datasets.search(
                    table=self.table, where=where, namespace=self.namespace
                )
            )
        }


class AdvancedDeleteRowsTool(_DatasetTool):
    def __init__(
        self,
        *,
        room: RoomClient,
        table: str,
        schema: dict[str, DatasetToolSchemaValue],
        namespace: Optional[list[str]] = None,
    ):
        self.table = table
        self.namespace = namespace
        columns = ""

        for k, v in schema.items():
            columns += _describe_schema_value(k, v)

        input_schema = {
            "type": "object",
            "required": ["where"],
            "additionalProperties": False,
            "properties": {
                "where": {
                    "type": "string",
                    "description": f"a lance db compatible filter, columns are: {columns}",
                }
            },
        }

        super().__init__(
            room=room,
            name=f"advanced_delete_{table}",
            title=f"advanced delete {table}",
            description=f"advanced search {table} table with a lancedb compatible filter and delete the matching rows",
            input_schema=input_schema,
        )

    async def execute(self, context: ToolContext, *, where: str):
        del context
        await self.room.datasets.delete(
            table=self.table, where=where, namespace=self.namespace
        )
        return {"ok": True}


class ExecuteSqlTool(_DatasetTool):
    def __init__(
        self,
        *,
        room: RoomClient,
        namespace: Optional[list[str]] = None,
    ):
        self.namespace = namespace

        super().__init__(
            room=room,
            name="execute_sql",
            title="execute SQL",
            description=(
                "execute a DataFusion SQL query or statement against the room datasets. "
                "SELECT-like commands return rows; update, delete, DDL, and other statements return rows_affected."
            ),
            input_schema={
                "type": "object",
                "required": ["query"],
                "additionalProperties": False,
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "a DataFusion SQL query or statement",
                    },
                    "params": {
                        "type": "object",
                        "description": "optional named parameter values encoded as a single JSON object",
                        "additionalProperties": _JSON_VALUE_SCHEMA,
                    },
                },
            },
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        query: str,
        params: Optional[dict[str, Any]] = None,
    ):
        del context
        arrow_params = None
        if params:
            arrow_params = pa.Table.from_pylist(
                [_normalize_dataset_tool_record(params)]
            )

        result = await self.room.datasets.execute_sql(
            query=query,
            params=arrow_params,
            namespace=self.namespace,
        )
        if isinstance(result, DatasetSqlStatement):
            return {
                "kind": "statement",
                "rows_affected": result.rows_affected,
            }
        if not isinstance(result, DatasetSqlQuery):
            raise TypeError(f"Unexpected SQL result type: {type(result).__name__}")

        try:
            batches = []
            async for batch in self.room.datasets.read_sql_query(
                query_id=result.query_id,
            ):
                batches.append(batch)
            table = pa.Table.from_batches(batches, schema=result.schema)
            return {
                "kind": "query",
                "rows": _dataset_rows(table),
            }
        finally:
            await self.room.datasets.close_sql_query(query_id=result.query_id)


class DatasetToolkit(Toolkit):
    def __init__(
        self,
        *,
        tables: dict[str, dict[str, DatasetToolSchemaValue]],
        read_only: bool = False,
        namespace: Optional[list[str]] = None,
        room: RoomClient,
    ):
        tools = [
            # ListTablesTool()
            ExecuteSqlTool(room=room, namespace=namespace),
        ]

        for table, schema in tables.items():
            if not read_only:
                tools.append(
                    InsertRowsTool(
                        room=room,
                        table=table,
                        schema=schema,
                        namespace=namespace,
                    )
                )
                tools.append(
                    UpdateTool(
                        room=room,
                        table=table,
                        schema=schema,
                        namespace=namespace,
                    )
                )
                # tools.append(
                #    DeleteRowsTool(table=table, schema=schema, namespace=namespace)
                # )
                tools.append(
                    AdvancedDeleteRowsTool(
                        room=room,
                        table=table,
                        schema=schema,
                        namespace=namespace,
                    )
                )

            tools.append(
                CountTool(room=room, table=table, schema=schema, namespace=namespace)
            )
            # tools.append(SearchTool(table=table, schema=schema, namespace=namespace))
            tools.append(
                AdvancedSearchTool(
                    room=room,
                    table=table,
                    schema=schema,
                    namespace=namespace,
                )
            )

        super().__init__(
            name="dataset",
            title="dataset",
            description="tools for interacting with meshagent datasets",
            room=room,
            tools=tools,
        )


async def make_dataset_toolkit(
    *,
    room: RoomClient,
    tables: list[str],
    read_only: bool,
    namespace: Optional[list[str]] = None,
) -> DatasetToolkit:
    table_schemas = {}
    for table in tables:
        table_schemas[table] = await room.datasets.inspect(
            table=table,
            namespace=namespace,
        )
    return DatasetToolkit(
        tables=table_schemas,
        read_only=read_only,
        namespace=namespace,
        room=room,
    )
