from pydantic import BaseModel
from meshagent.tools.tool import BaseTool
from meshagent.api import RoomException


class ToolConfig(BaseModel):
    name: str


class ToolProvider:
    def __init__(self, *, name: str, type: type):
        self.name = name
        self.type = type

    def make(self, *, model: str, config: ToolConfig) -> BaseTool: ...


def make_tools(
    *, model: str, providers: list[ToolProvider], tools: list[ToolConfig]
) -> list[BaseTool]:
    result = []
    if tools is not None:
        for config in tools:
            found = False
            if isinstance(config, dict):
                for t in providers:
                    if t.name == config["name"]:
                        config = t.type.model_validate(config)
                        result.append(t.make(model=model, config=config))
                        found = True
                        break

            else:
                for t in providers:
                    if t.type is type(config):
                        result.append(t.make(model=model, config=config))
                        found = True
                        break

            if not found:
                raise RoomException(f"tool cannot be configured: {config}")

    return result
