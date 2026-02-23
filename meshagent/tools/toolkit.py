from meshagent.api.room_server_client import RoomException
from meshagent.api.messaging import Content, EmptyContent, JsonContent, ensure_content
from meshagent.api import RoomClient
from jsonschema import validate
import logging

import json

from typing import Optional, Literal
from meshagent.tools.config import ToolkitConfig
from meshagent.tools.tool import ToolContext, BaseTool, FunctionTool, ContentTool

from opentelemetry import trace
from collections.abc import AsyncIterable

tracer = trace.get_tracer("meshagent.tools")

logger = logging.getLogger("tools")


def _schema_allows_null(schema: object) -> bool:
    if not isinstance(schema, dict):
        return False

    type_value = schema.get("type")
    if isinstance(type_value, list):
        return "null" in type_value
    if type_value == "null":
        return True

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        for variant in any_of:
            if _schema_allows_null(variant):
                return True

    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        for variant in one_of:
            if _schema_allows_null(variant):
                return True

    return False


def _coerce_missing_nullable_required_arguments(
    *, schema: dict, arguments: dict
) -> dict:
    required = schema.get("required")
    properties = schema.get("properties")
    if not isinstance(required, list) or not isinstance(properties, dict):
        return arguments

    normalized = dict(arguments)
    for key in required:
        if not isinstance(key, str):
            continue
        if key in normalized:
            continue

        property_schema = properties.get(key)
        if _schema_allows_null(property_schema):
            normalized[key] = None

    return normalized


class ToolkitConfig(ToolkitConfig):
    toolkit: str
    tool: str


def make_basic_toolkit_config_cls(toolkit: "Toolkit"):
    class CustomToolkitConfig:
        name: Literal[toolkit.name] = toolkit.name

    return CustomToolkitConfig


class ToolkitBuilder:
    def __init__(self, *, name: str, type: type):
        self.name = name
        self.type = type

    async def make(
        self, *, room: RoomClient, model: str, config: ToolkitConfig
    ) -> "Toolkit": ...


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
        input: Content | AsyncIterable[Content],
    ):
        with tracer.start_as_current_span("toolkit.execute") as span:
            span.set_attributes({"toolkit": self.name, "tool": name})

            tool = self.get_tool(name)
            if not isinstance(tool, (FunctionTool, ContentTool)):
                raise RoomException(
                    "tools must extend the FunctionTool or ContentTool class to be invokable"
                )

            if isinstance(input, AsyncIterable):
                if not isinstance(tool, ContentTool):
                    raise RoomException(f"tool '{name}' does not accept streamed input")
                response = await tool.execute(context=context, input=input)
            else:
                normalized_input = ensure_content(input)
                if isinstance(tool, ContentTool):
                    response = await tool.execute(
                        context=context, input=normalized_input
                    )
                else:
                    if not isinstance(tool, FunctionTool):
                        raise RoomException(f"tool '{name}' requires streamed input")
                    if isinstance(normalized_input, EmptyContent):
                        normalized_arguments = {}
                    elif isinstance(normalized_input, JsonContent):
                        if not isinstance(normalized_input.json, dict):
                            raise RoomException(
                                f"tool '{name}' requires JSON object input"
                            )
                        normalized_arguments = normalized_input.json
                    else:
                        raise RoomException(f"tool '{name}' requires JSON object input")

                    schema = tool.input_schema
                    if schema is None:
                        raise RoomException(
                            f"tool '{name}' is missing required function input schema"
                        )
                    schema_for_validation = {**schema}
                    if tool.defs is not None:
                        schema_for_validation["$defs"] = {**tool.defs}

                    normalized_arguments = _coerce_missing_nullable_required_arguments(
                        schema=schema_for_validation,
                        arguments=normalized_arguments,
                    )
                    validate(normalized_arguments, schema_for_validation)
                    span.set_attribute(
                        "arguments", json.dumps(normalized_arguments, sort_keys=True)
                    )
                    response = await tool.execute(
                        context=context, **normalized_arguments
                    )
            if isinstance(response, AsyncIterable):
                span.set_attribute("response_type", "stream")
                return response

            response = ensure_content(response)

            span.set_attribute("response_type", response.to_json()["type"])
            return response

    async def make(self, *, room: RoomClient, model: str, config: ToolkitConfig):
        return self


async def make_toolkits(
    *,
    room: RoomClient,
    model: str,
    providers: list[ToolkitBuilder],
    tools: list[ToolkitConfig],
) -> list[Toolkit]:
    result = []
    if tools is not None:
        for config in tools:
            found = False
            if isinstance(config, dict):
                for t in providers:
                    if t.name == config["name"]:
                        config = t.type.model_validate(config)
                        result.append(
                            await t.make(room=room, model=model, config=config)
                        )
                        found = True
                        break

            else:
                for t in providers:
                    if t.type is type(config):
                        result.append(
                            await t.make(room=room, model=model, config=config)
                        )
                        found = True
                        break

            if not found:
                raise RoomException(f"tool cannot be configured: {config}")

    return result
