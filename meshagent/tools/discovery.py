from meshagent.api import RoomClient

from .tool import LocalRoomTool, ToolContext
from .toolkit import Toolkit


class ListTools(LocalRoomTool):
    def __init__(self, *, room: RoomClient):
        super().__init__(
            room=room,
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
        toolkits = await self.room.agents.list_toolkits(participant_id=participant_id)
        return {"toolkits": [*(t.to_json() for t in toolkits)]}


class DiscoveryToolkit(Toolkit):
    def __init__(self, *, room: RoomClient):
        super().__init__(
            name="discovery",
            title="discovery",
            description="toolkit for discovering tools in a room",
            room=room,
            tools=[
                ListTools(room=room),
            ],
        )
