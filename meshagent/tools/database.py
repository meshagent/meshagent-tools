from .config import ToolkitConfig
from .tool import FunctionTool
from .toolkit import ToolContext, ToolkitBuilder
from .hosting import RemoteToolkit, Toolkit
from typing import Any, Literal, Optional
from meshagent.api.room_server_client import (
    BinaryDataType,
    DataType,
    DateDataType,
    JsonDataType,
    ListDataType,
    TimestampDataType,
    StructDataType,
    UuidDataType,
    decode_records,
    RoomClient,
)

import logging

logger = logging.getLogger("database_toolkit")

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


def _wrapped_database_value_json_schema(
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


def _tool_input_schema_for_data_type(
    data_type: DataType,
    *,
    allow_expression: bool = False,
) -> dict[str, Any]:
    variants = list[dict[str, Any]]()
    if not isinstance(
        data_type,
        (
            BinaryDataType,
            DateDataType,
            JsonDataType,
            ListDataType,
            StructDataType,
            TimestampDataType,
            UuidDataType,
        ),
    ):
        variants.append(data_type.to_json_schema())
    if isinstance(data_type, BinaryDataType):
        variants.append(
            _wrapped_database_value_json_schema(
                wrapper="binary",
                payload_schema={
                    "type": "string",
                    "description": "a base64 encoded byte string",
                },
            )
        )
    if isinstance(data_type, DateDataType):
        variants.append(
            _wrapped_database_value_json_schema(
                wrapper="date",
                payload_schema={
                    "type": "string",
                    "description": "an ISO formatted date string",
                },
            )
        )
    if isinstance(data_type, TimestampDataType):
        variants.append(
            _wrapped_database_value_json_schema(
                wrapper="timestamp",
                payload_schema={
                    "type": "string",
                    "description": "an ISO formatted timestamp string",
                },
            )
        )
    if isinstance(data_type, UuidDataType):
        variants.append(
            _wrapped_database_value_json_schema(
                wrapper="uuid",
                payload_schema={
                    "type": "string",
                    "description": "a UUID string",
                },
            )
        )
    if isinstance(data_type, ListDataType):
        variants.append(
            _wrapped_database_value_json_schema(
                wrapper="list",
                payload_schema={"type": "array"},
            )
        )
    if isinstance(data_type, StructDataType):
        variants.append(
            _wrapped_database_value_json_schema(
                wrapper="struct",
                payload_schema={"type": "object"},
            )
        )
    if isinstance(data_type, JsonDataType):
        variants.append(
            _wrapped_database_value_json_schema(
                wrapper="json",
                payload_schema=_JSON_VALUE_SCHEMA,
            )
        )
    if allow_expression:
        variants.append(_EXPRESSION_JSON_SCHEMA)
    if data_type.nullable:
        variants.append({"type": "null"})
    if len(variants) == 1:
        return variants[0]
    return {"anyOf": variants}


def _normalize_database_tool_record(record: dict[str, Any]) -> dict[str, Any]:
    return decode_records([record])[0]


def _normalize_database_tool_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return decode_records(rows)


class ListTablesTool(FunctionTool):
    def __init__(self):
        input_schema = {
            "type": "object",
            "required": [],
            "additionalProperties": False,
            "properties": {},
        }

        super().__init__(
            name="list_tables",
            title="list tables",
            description="list the tables in the room",
            input_schema=input_schema,
        )

    async def execute(self, context: ToolContext):
        return {"tables": await context.room.database.list_tables()}


class InsertRowsTool(FunctionTool):
    def __init__(
        self,
        *,
        table: str,
        schema: dict[str, DataType],
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
        await context.room.database.insert(
            table=self.table,
            records=_normalize_database_tool_rows(rows),
            namespace=self.namespace,
        )


class DeleteRowsTool(FunctionTool):
    def __init__(
        self,
        *,
        table: str,
        schema: dict[str, DataType],
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
            name=f"delete_{table}_rows",
            title=f"delete {table} rows",
            description=f"delete from {table} where rows match the specified values (specify null for a column to exclude it from the search)",
            input_schema=input_schema,
        )

    async def execute(self, context: ToolContext, **values):
        search = {}

        for k, v in values.items():
            if v is not None:
                search[k] = v
        if search:
            search = _normalize_database_tool_record(search)

        await context.room.database.delete(
            table=self.table,
            where=search if len(search) > 0 else None,
            namespace=self.namespace,
        )
        return {"ok": True}


class UpdateTool(FunctionTool):
    def __init__(
        self,
        *,
        table: str,
        schema: dict[str, DataType],
        namespace: Optional[list[str]] = None,
    ):
        self.table = table
        self.namespace = namespace

        columns = ""

        for k, v in schema.items():
            columns += f"column {k} => {v.model_dump(mode='json')}"

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
            name=f"update_{table}_rows",
            title=f"update {table} rows",
            description=f"update {table} table where rows match the specified filter (with a lancedb compatible filter)",
            input_schema=input_schema,
        )

    async def execute(self, context: ToolContext, *, where: str, values: list[dict]):
        set = {}
        for value in values:
            for k, v in value.items():
                set[k] = v
        set = _normalize_database_tool_record(set)

        await context.room.database.update(
            table=self.table, where=where, values=set, namespace=self.namespace
        )

        return {"ok": True}


class SearchTool(FunctionTool):
    def __init__(
        self,
        *,
        table: str,
        schema: dict[str, DataType],
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
        search = {}

        for k, v in query.items():
            if v is not None:
                search[k] = v
        if search:
            search = _normalize_database_tool_record(search)

        return {
            "rows": await context.room.database.search(
                select=select,
                table=self.table,
                where=search if len(search) > 0 else None,
                namespace=self.namespace,
                offset=offset,
                limit=limit,
            )
        }


class LLMSearchTool(FunctionTool):
    def __init__(
        self,
        *,
        table: str,
        schema: dict[str, DataType],
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
        search = {}

        for k, v in query.items():
            if v is not None:
                search[k] = v
        if search:
            search = _normalize_database_tool_record(search)

        return {
            "rows": await context.room.database.search(
                select=select,
                table=self.table,
                where=search if len(search) > 0 else None,
                namespace=self.namespace,
                offset=offset,
                limit=limit,
            )
        }


class SpawnTaskForEachRow(FunctionTool):
    def __init__(
        self,
        *,
        table: str,
        schema: dict[str, DataType],
        prompt: str,
        queue: str,
        namespace: Optional[list[str]] = None,
        name: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        supports_context: bool = True,
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
            name=name or f"spawn_task_for_each_{table}_row",
            title=title or f"Spawn task for each {table} row",
            description=description
            or f"for each result in {table} where rows match the specified values (specify null for a column to exclude it from the search), queue a worker task",
            input_schema=input_schema,
            supports_context=supports_context,
        )

    def make_message(self, *, context: ToolContext, row: dict):
        msg = {
            "prompt": self.prompt.format(**row),
            "row": row,
        }

        if self.supports_context:
            msg["caller_context"] = context.caller_context

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
            search = _normalize_database_tool_record(search)

        rows = await context.room.database.search(
            select=select,
            table=self.table,
            where=search if len(search) > 0 else None,
            namespace=self.namespace,
            offset=offset,
            limit=limit,
        )

        logger.info(
            f"spawn_task_for_each_{self.table}_row matched {len(rows)}. adding items to the queue {self.queue}"
        )

        for row in rows:
            await context.room.queues.send(
                name=self.queue, message=self.make_message(context=context, row=row)
            )

        return {f"added {len(row)} items to the queue {self.queue}"}


class CountTool(FunctionTool):
    def __init__(
        self,
        *,
        table: str,
        schema: dict[str, DataType],
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
            name=f"count_{table}",
            title=f"count_{table}",
            description=f"count matching rows in the {table} table",
            input_schema=input_schema,
        )

    async def execute(self, context: ToolContext, query: object):
        search = {}

        for k, v in query.items():
            if v is not None:
                search[k] = v
        if search:
            search = _normalize_database_tool_record(search)

        return {
            "rows": await context.room.database.count(
                table=self.table,
                where=search if len(search) > 0 else None,
                namespace=self.namespace,
            )
        }


class AdvancedSearchTool(FunctionTool):
    def __init__(
        self,
        *,
        table: str,
        schema: dict[str, DataType],
        namespace: Optional[list[str]] = None,
    ):
        self.table = table
        self.namespace = namespace

        columns = ""

        for k, v in schema.items():
            columns += f"column {k} => {v.model_dump(mode='json')}\n"

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
            name=f"advanced_search_{table}",
            title=f"advanced search {table}",
            description=f"advanced search {table} table with a lancedb compatible filter",
            input_schema=input_schema,
        )

    async def execute(self, context: ToolContext, *, where: str):
        return {
            "rows": await context.room.database.search(
                table=self.table, where=where, namespace=self.namespace
            )
        }


class AdvancedDeleteRowsTool(FunctionTool):
    def __init__(
        self,
        *,
        table: str,
        schema: dict[str, DataType],
        namespace: Optional[list[str]] = None,
    ):
        self.table = table
        self.namespace = namespace
        columns = ""

        for k, v in schema.items():
            columns += f"column {k} => {v.model_dump(mode='json')}"

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
            name=f"advanced_delete_{table}",
            title=f"advanced delete {table}",
            description=f"advanced search {table} table with a lancedb compatible filter and delete the matching rows",
            input_schema=input_schema,
        )

    async def execute(self, context: ToolContext, *, where: str):
        await context.room.database.delete(
            table=self.table, where=where, namespace=self.namespace
        )
        return {"ok": True}


class DatabaseToolkit(RemoteToolkit):
    def __init__(
        self,
        *,
        tables: dict[str, dict[str, DataType]],
        read_only: bool = False,
        namespace: Optional[list[str]] = None,
    ):
        tools = [
            # ListTablesTool()
        ]

        for table, schema in tables.items():
            if not read_only:
                tools.append(
                    InsertRowsTool(table=table, schema=schema, namespace=namespace)
                )
                tools.append(
                    UpdateTool(table=table, schema=schema, namespace=namespace)
                )
                # tools.append(
                #    DeleteRowsTool(table=table, schema=schema, namespace=namespace)
                # )
                tools.append(
                    AdvancedDeleteRowsTool(
                        table=table, schema=schema, namespace=namespace
                    )
                )

            tools.append(CountTool(table=table, schema=schema, namespace=namespace))
            # tools.append(SearchTool(table=table, schema=schema, namespace=namespace))
            tools.append(
                AdvancedSearchTool(table=table, schema=schema, namespace=namespace)
            )

        super().__init__(
            name="database",
            title="database",
            description="tools for interacting with meshagent databases",
            tools=tools,
        )


class DatabaseToolkitConfig(ToolkitConfig):
    name: Literal["database"] = "database"
    tables: list[str]
    namespace: Optional[list[str]] = None
    read_only: bool


class DatabaseToolkitBuilder(ToolkitBuilder):
    def __init__(self):
        super().__init__(name="database", type=DatabaseToolkitConfig)

    async def make(
        self, *, room: RoomClient, model: str, config: DatabaseToolkitConfig
    ) -> Toolkit:
        tables = {}
        for t in config.tables:
            tables[t] = await room.database.inspect(table=t, namespace=config.namespace)
        return DatabaseToolkit(
            tables=tables, read_only=config.read_only, namespace=config.namespace
        )
