from .hosting import RemoteToolkit, ToolContext, Tool


class ListTools(Tool):
    def __init__(self):
        super().__init__(
            name="list_tools",
            title="list toolkits",
            description="lists the available toolkits in the room",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": [],
                "properties": {},
            },
        )

    async def execute(self, context: ToolContext):
        participant_id = (
            context.on_behalf_of.id
            if context.on_behalf_of is not None
            else context.caller.id
        )
        print(
            f"{participant_id} {context.caller.id} {context.on_behalf_of}", flush=True
        )
        toolkits = await context.room.agents.list_toolkits(
            participant_id=participant_id
        )
        return {"toolkits": [*(t.to_json() for t in toolkits)]}


class ListAgents(Tool):
    def __init__(self):
        super().__init__(
            name="list_agents",
            title="list agents",
            description="lists the callable agents in the room",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": [],
                "properties": {},
            },
        )

    async def execute(self, context: ToolContext):
        agents = await context.room.agents.list_agents()
        return {"agents": [*(t.to_json() for t in agents)]}


class DiscoveryToolkit(RemoteToolkit):
    def __init__(self):
        super().__init__(
            name="storage",
            title="storage",
            description="tools for interacting with meshagent room storage",
            tools=[
                ListTools(),
                ListAgents(),
            ],
        )
