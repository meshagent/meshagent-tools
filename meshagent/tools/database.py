from .config import ToolkitConfig
from .tool import Tool
from .toolkit import ToolContext, ToolkitBuilder
from .hosting import RemoteToolkit, Toolkit
from typing import Literal, Optional
from meshagent.api.room_server_client import DataType, RoomClient

import logging

logger = logging.getLogger("database_toolkit")


class ListTablesTool(Tool):
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


class InsertRowsTool(Tool):
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
            input_schema["properties"][k] = v.to_json_schema()

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
            table=self.table, records=rows, namespace=self.namespace
        )


class DeleteRowsTool(Tool):
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
            input_schema["properties"][k] = v.to_json_schema()

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

        await context.room.database.delete(
            table=self.table,
            where=search if len(search) > 0 else None,
            namespace=self.namespace,
        )
        return {"ok": True}


class UpdateTool(Tool):
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
            columns += f"column {k} => {v.to_json()}"

        values_schema = {
            "type": "object",
            "required": [],
            "additionalProperties": False,
            "properties": {},
        }

        for k, v in schema.items():
            values_schema["required"].append(k)
            values_schema["properties"][k] = v.to_json_schema()

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
                "values": values_schema,
            },
        }

        super().__init__(
            name=f"update_{table}_rows",
            title=f"update {table} rows",
            description=f"update {table} table where rows match the specified filter (with a lancedb compatible filter)",
            input_schema=input_schema,
        )

    async def execute(self, context: ToolContext, *, where: str, values: dict):
        await context.room.database.update(
            table=self.table, where=where, values=values, namespace=self.namespace
        )

        return {"ok": True}


class SearchTool(Tool):
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
            query["properties"][k] = v.to_json_schema()

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


class LLMßSearchTool(Tool):
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
            query["properties"][k] = v.to_json_schema()

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


class SpawnTaskForEachRow(Tool):
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
            query["properties"][k] = v.to_json_schema()

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


class CountTool(Tool):
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
            query["properties"][k] = v.to_json_schema()

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

        return {
            "rows": await context.room.database.count(
                table=self.table,
                where=search if len(search) > 0 else None,
                namespace=self.namespace,
            )
        }


class AdvancedSearchTool(Tool):
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
            columns += f"column {k} => {v.to_json()}"

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


class AdvancedDeleteRowsTool(Tool):
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
            columns += f"column {k} => {v.to_json()}"

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
                tools.append(
                    DeleteRowsTool(table=table, schema=schema, namespace=namespace)
                )
                tools.append(
                    AdvancedDeleteRowsTool(
                        table=table, schema=schema, namespace=namespace
                    )
                )

            tools.append(CountTool(table=table, schema=schema, namespace=namespace))
            tools.append(SearchTool(table=table, schema=schema, namespace=namespace))
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
