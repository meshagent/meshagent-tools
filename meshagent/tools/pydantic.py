from pydantic import BaseModel
from abc import abstractmethod
import logging
from typing import Optional

from meshagent.api.messaging import Response
from meshagent.tools.toolkit import Tool, ToolContext

logger = logging.getLogger("pydantic_tool")


class PydanticTool[TInput: BaseModel](Tool):
    def __init__(
        self,
        name: str,
        input_model: TInput,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ):
        self.input_model = input_model

        super().__init__(
            name=name,
            description=description,
            title=title,
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
    async def execute_model(
        self, *, context: ToolContext, arguments: TInput
    ) -> Response | dict | None | str:
        pass
