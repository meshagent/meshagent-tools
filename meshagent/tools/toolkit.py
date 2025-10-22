from meshagent.api.room_server_client import RoomException
from meshagent.api.messaging import ensure_response
from jsonschema import validate
import logging

import json

from typing import Optional, Literal
from meshagent.tools.config import ToolkitConfig
from meshagent.tools.tool import ToolContext, BaseTool

from opentelemetry import trace

tracer = trace.get_tracer("meshagent.tools")

logger = logging.getLogger("tools")


class ToolkitConfig(ToolkitConfig):
    toolkit: str
    tool: str


def make_basic_toolkit_config_cls(toolkit: "Toolkit"):
    class CustomToolkitConfig:
        name: Literal[toolkit.name]

    return CustomToolkitConfig


class ToolkitBuilder:
    def __init__(self, *, name: str, type: type):
        self.name = name
        self.type = type

    def make(self, *, model: str, config: ToolkitConfig) -> "Toolkit": ...


class Toolkit(ToolkitBuilder):
    def __init__(
        self,
        *,
        name: str,
        tools: list[BaseTool],
        rules: list[str] = list[str](),
        title: Optional[str] = None,
        description: Optional[str] = None,
        thumbnail_url: Optional[str] = None,
    ):
        self.name = name
        if title is None:
            title = name
        self.title = title
        if description is None:
            description = ""
        self.description = description
        self.tools = tools
        self.rules = rules
        self.thumbnail_url = thumbnail_url

    def get_tool(self, name: str) -> BaseTool:
        for tool in self.tools:
            if tool.name == name:
                return tool
        raise RoomException(
            f'a tool with the name "{name}" was not found in the toolkit'
        )

    async def execute(
        self,
        *,
        context: ToolContext,
        name: str,
        arguments: dict,
        attachment: Optional[bytes] = None,
    ):
        with tracer.start_as_current_span("toolkit.execute") as span:
            span.set_attributes(
                {"toolkit": self.name, "tool": name, "arguments": json.dumps(arguments)}
            )

            tool = self.get_tool(name)

            schema = {
                **tool.input_schema,
            }
            if tool.defs is not None:
                schema["$defs"] = {**tool.defs}

            validate(arguments, schema)
            response = await tool.execute(context=context, **arguments)
            response = ensure_response(response)

            span.set_attribute("response_type", response.to_json()["type"])
            return response

    def make(self, *, model: str, config: ToolkitConfig):
        return self


def make_tools(
    *, model: str, providers: list[ToolkitBuilder], tools: list[ToolkitConfig]
) -> list[Toolkit]:
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
