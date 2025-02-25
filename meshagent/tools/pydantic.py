from pydantic import BaseModel
from abc import abstractmethod
import logging
from typing import Optional

from meshagent.api import RoomClient

from meshagent.tools.toolkit import Tool, Response, ToolContext
from meshagent.agents import AgentCallContext
from meshagent.agents.adapter import ToolResponseAdapter

logger = logging.getLogger("pydantic_tool")
logger.setLevel(logging.INFO)

class PydanticTool[TInput:BaseModel](Tool):
    def __init__(self,
        name: str,
        input_model: TInput,
        title: Optional[str] = None,
        description: Optional[str] = None
    ):
        self.input_model = input_model
     
        super().__init__(
            name = name,
            description = description,
            title = title,
            input_schema=input_model.model_json_schema(),
        )

    async def execute(self, context, **kwargs):
        try:
            input = self.input_model.model_validate(kwargs)
            return await self.execute_model(context=context, arguments=input)
        except Exception as e:
            logger.error("Unhandled exception in ask agent call", exc_info=e)
            raise

    @abstractmethod
    async def execute_model(self, *, context: ToolContext, arguments: TInput) -> Response | dict | None | str:
        pass


# -------

import pydantic_ai 

def get_pydantic_ai_tool_definition(*, tool: Tool) -> pydantic_ai.tools.ToolDefinition:
    tool_definition = pydantic_ai.tools.ToolDefinition(
        name=tool.name,
        description=tool.description,
        parameters_json_schema=tool.input_schema
    )
    return tool_definition



def get_pydantic_ai_tool(*, room: RoomClient, tool: Tool, response_adapter: ToolResponseAdapter) -> pydantic_ai.tools.Tool:
     async def prepare(ctx: pydantic_ai.RunContext, tool_def: pydantic_ai.tools.ToolDefinition):
         return get_pydantic_ai_tool_definition(tool=tool)
     
     async def execute(**kwargs):
         response = await tool.execute(context=ToolContext(room=room, caller=room.local_participant), **kwargs)
         return await response_adapter.to_plain_text(room=room, response=response)

     return pydantic_ai.Tool(
        name=tool.name,
        takes_ctx=False,
        description=tool.description,
        prepare=prepare,
        function=execute
    )

def get_pydantic_ai_tools_from_context(*, context: AgentCallContext, response_adapter: ToolResponseAdapter) -> list[pydantic_ai.tools.Tool]:

    tools = list[pydantic_ai.tools.Tool]()

    for toolkit in context.toolkits:
        
        for tool in toolkit.tools:

            tools.append(get_pydantic_ai_tool(room=context.room, tool=tool, response_adapter=response_adapter))

    return tools


