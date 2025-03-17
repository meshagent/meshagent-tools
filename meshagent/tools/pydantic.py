from pydantic import BaseModel
from abc import abstractmethod
import logging
from typing import Optional

from meshagent.tools.toolkit import Tool, Response, ToolContext

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





