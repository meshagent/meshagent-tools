from .config import ToolkitConfig
from .tool import Tool
from .toolkit import ToolContext, ToolkitBuilder
from .hosting import RemoteToolkit, Toolkit
from typing import Literal, Optional
from meshagent.api.room_server_client import DataType, RoomClient


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
            schema = v.to_json_schema()
            schema["type"] = [schema["type"], "null"]
            input_schema["properties"][k] = schema

        super().__init__(
            name=f"delete_{table}_rows",
            title=f"delete {table} rows",
            description=f"search {table} table for rows with the specified values (specify null for a column to exclude it from the search) and then delete them",
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

        input_schema = {
            "type": "object",
            "required": [],
            "additionalProperties": False,
            "properties": {},
        }

        for k, v in schema.items():
            input_schema["required"].append(k)
            schema = v.to_json_schema()
            schema["type"] = [schema["type"], "null"]
            input_schema["properties"][k] = schema

        super().__init__(
            name=f"search_{table}",
            title=f"search {table}",
            description=f"search {table} table for rows with the specified values (specify null for a column to exclude it from the search)",
            input_schema=input_schema,
        )

    async def execute(self, context: ToolContext, **values):
        search = {}

        for k, v in values.items():
            if v is not None:
                search[k] = v

        return {
            "rows": await context.room.database.search(
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
    name: str = Literal["database"]
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
