import uuid
from .tool import Tool
from .toolkit import ToolContext
from .hosting import RemoteToolkit


class UuidV4Tool(Tool):
    def __init__(self):
        super().__init__(
            name="uuid_v4",
            title="uuid v4",
            description="Generate UUIDv4 strings (standard 8-4-4-4-12 format).",
            input_schema={
                "type": "object",
                "required": ["count"],
                "additionalProperties": False,
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "How many UUIDs to generate (default 1).",
                    },
                },
            },
        )

    async def execute(self, context: ToolContext, *, count: int = 1):
        # uuid.uuid4() returns a UUID object; str(...) yields canonical form:
        # xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
        uuids = [str(uuid.uuid4()) for _ in range(int(count or 1))]
        if count == 1:
            return {"uuid": uuids[0]}
        return {"uuids": uuids, "count": len(uuids)}


class UUIDToolkit(RemoteToolkit):
    def __init__(self):
        tools = [UuidV4Tool()]
        super().__init__(
            name="uuid",
            title="uuid",
            description="Generate uuids",
            tools=tools,
        )
