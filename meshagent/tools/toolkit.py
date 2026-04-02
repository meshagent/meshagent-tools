from collections.abc import AsyncIterable
import json
from typing import Any, Optional, Literal

from jsonschema import ValidationError, validate
from pydantic import BaseModel

from meshagent.api import RoomClient, ToolContentSpec
from meshagent.api.messaging import (
    BinaryContent,
    Content,
    EmptyContent,
    ErrorContent,
    FileContent,
    JsonContent,
    LinkContent,
    TextContent,
    _ControlContent,
    ensure_content,
)
from meshagent.api.room_server_client import RoomException
from meshagent.tools.config import ToolkitConfig
from meshagent.tools.tool import (
    ToolContext,
    BaseTool,
    FunctionTool,
    ContentTool,
    ValidationMode,
)

from opentelemetry import trace

tracer = trace.get_tracer("meshagent.tools")


class InvalidToolDataException(RoomException):
    pass


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


def _span_json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return str(value)


def build_tool_span_name(
    *, operation: Literal["invoke", "execute"], toolkit_name: str, tool_name: str
) -> str:
    return f"{operation}.{toolkit_name}.{tool_name}"


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
        validation_mode: ValidationMode = "full",
    ):
        if validation_mode not in ("full", "content_types", "none"):
            raise ValueError(
                "validation_mode must be one of 'full', 'content_types', or 'none'"
            )

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
        self.validation_mode: ValidationMode = validation_mode

    @staticmethod
    def _should_validate_content_types(*, validation_mode: ValidationMode) -> bool:
        return validation_mode in ("full", "content_types")

    @staticmethod
    def _should_validate_schema(*, validation_mode: ValidationMode) -> bool:
        return validation_mode == "full"

    @staticmethod
    def _content_kind(content: Content) -> str:
        if isinstance(content, JsonContent):
            return "json"
        if isinstance(content, TextContent):
            return "text"
        if isinstance(content, FileContent):
            return "file"
        if isinstance(content, BinaryContent):
            return "binary"
        if isinstance(content, LinkContent):
            return "link"
        if isinstance(content, EmptyContent):
            return "empty"
        if isinstance(content, _ControlContent):
            return "control"
        if isinstance(content, ErrorContent):
            return "error"
        content_type = content.to_json().get("type", None)
        if isinstance(content_type, str):
            return content_type
        return "unknown"

    @staticmethod
    def _schema_value_for_content(content: Content) -> Any:
        if isinstance(content, JsonContent):
            return content.json
        if isinstance(content, TextContent):
            return content.text
        if isinstance(content, EmptyContent):
            return None
        if isinstance(content, LinkContent):
            return {"name": content.name, "url": content.url}
        if isinstance(content, FileContent):
            return {
                "name": content.name,
                "mime_type": content.mime_type,
                "size": len(content.data),
            }
        if isinstance(content, BinaryContent):
            return dict(content.headers)
        if isinstance(content, _ControlContent):
            return {"method": content.method}
        if isinstance(content, ErrorContent):
            return {"text": content.text}
        return content.to_json()

    @staticmethod
    def _schema_with_defs(
        *, schema: dict | None, defs: Optional[dict[str, dict]]
    ) -> dict | None:
        if schema is None:
            return None
        merged = {**schema}
        if defs is None:
            return merged
        existing_defs = merged.get("$defs", None)
        if isinstance(existing_defs, dict):
            merged["$defs"] = {**defs, **existing_defs}
        else:
            merged["$defs"] = {**defs}
        return merged

    def _validate_stream_mode(
        self,
        *,
        tool_name: str,
        direction: Literal["input", "output"],
        spec: ToolContentSpec | None,
        stream: bool,
        validation_mode: ValidationMode,
    ) -> None:
        if spec is None or not self._should_validate_content_types(
            validation_mode=validation_mode
        ):
            return
        if spec.stream != stream:
            expected = "streamed" if spec.stream else "single-content"
            actual = "streamed" if stream else "single-content"
            raise InvalidToolDataException(
                f"tool '{tool_name}' {direction} is {actual} but {direction}_spec requires {expected} {direction}"
            )

    def _validate_content_type(
        self,
        *,
        tool_name: str,
        direction: Literal["input", "output"],
        spec: ToolContentSpec | None,
        content: Content,
        validation_mode: ValidationMode,
    ) -> None:
        if spec is None or not self._should_validate_content_types(
            validation_mode=validation_mode
        ):
            return
        content_type = self._content_kind(content)
        if content_type not in spec.types:
            allowed = ", ".join(spec.types)
            raise InvalidToolDataException(
                f"tool '{tool_name}' {direction} content type '{content_type}' is not allowed by {direction}_spec ({allowed})"
            )

    def _validate_schema(
        self,
        *,
        tool_name: str,
        direction: Literal["input", "output"],
        content: Content,
        schema: dict | None,
        defs: Optional[dict[str, dict]],
        validation_mode: ValidationMode,
    ) -> None:
        if not self._should_validate_schema(validation_mode=validation_mode):
            return
        resolved_schema = self._schema_with_defs(schema=schema, defs=defs)
        if resolved_schema is None:
            return
        try:
            validate(
                instance=self._schema_value_for_content(content),
                schema=resolved_schema,
            )
        except ValidationError as ex:
            raise InvalidToolDataException(
                f"tool '{tool_name}' {direction} does not match {direction}_schema: {ex.message}"
            ) from ex

    def _validate_input_content(
        self,
        *,
        tool: BaseTool,
        content: Content,
        validate_schema: bool,
        validation_mode: ValidationMode,
    ) -> None:
        self._validate_content_type(
            tool_name=tool.name,
            direction="input",
            spec=tool.input_spec,
            content=content,
            validation_mode=validation_mode,
        )
        if validate_schema:
            self._validate_schema(
                tool_name=tool.name,
                direction="input",
                content=content,
                schema=tool.input_schema,
                defs=tool.defs,
                validation_mode=validation_mode,
            )

    def _validate_output_content(
        self,
        *,
        tool: BaseTool,
        content: Content,
        validation_mode: ValidationMode,
    ) -> None:
        self._validate_content_type(
            tool_name=tool.name,
            direction="output",
            spec=tool.output_spec,
            content=content,
            validation_mode=validation_mode,
        )
        self._validate_schema(
            tool_name=tool.name,
            direction="output",
            content=content,
            schema=tool.output_schema,
            defs=tool.defs,
            validation_mode=validation_mode,
        )

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
        validate_function_schema: bool = True,
    ):
        with tracer.start_as_current_span(
            build_tool_span_name(
                operation="execute",
                toolkit_name=self.name,
                tool_name=name,
            )
        ) as span:
            span.set_attributes({"toolkit": self.name, "tool": name})
            context_validation_mode = context.validation_mode
            if context_validation_mode is not None:
                validate_function_schema = context_validation_mode == "full"

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

                    if validate_function_schema:
                        schema = tool.input_schema
                        if schema is None:
                            raise RoomException(
                                f"tool '{name}' is missing required function input schema"
                            )
                        schema_for_validation = {**schema}
                        if tool.defs is not None:
                            schema_for_validation["$defs"] = {**tool.defs}

                        normalized_arguments = (
                            _coerce_missing_nullable_required_arguments(
                                schema=schema_for_validation,
                                arguments=normalized_arguments,
                            )
                        )
                        validate(normalized_arguments, schema_for_validation)
                    else:
                        schema = tool.input_schema
                        if schema is not None:
                            schema_for_normalization = {**schema}
                            if tool.defs is not None:
                                schema_for_normalization["$defs"] = {**tool.defs}
                            normalized_arguments = (
                                _coerce_missing_nullable_required_arguments(
                                    schema=schema_for_normalization,
                                    arguments=normalized_arguments,
                                )
                            )

                        normalized_arguments = tool.normalize_execution_arguments(
                            normalized_arguments
                        )

                    span.set_attribute(
                        "arguments",
                        json.dumps(
                            normalized_arguments,
                            sort_keys=True,
                            default=_span_json_default,
                        ),
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

    async def invoke(
        self,
        *,
        context: ToolContext,
        name: str,
        input: Content | AsyncIterable[Content],
        validation_mode: ValidationMode | None = None,
    ) -> Content | AsyncIterable[Content]:
        validation_mode = (
            context.validation_mode
            if validation_mode is None and context.validation_mode is not None
            else self.validation_mode
            if validation_mode is None
            else validation_mode
        )

        tool = self.get_tool(name)
        if not isinstance(tool, (FunctionTool, ContentTool)):
            raise RoomException(
                "tools must extend the FunctionTool or ContentTool class to be invokable"
            )

        execution_input: Content | AsyncIterable[Content]
        if isinstance(input, AsyncIterable):
            if not isinstance(tool, ContentTool):
                raise RoomException(f"tool '{name}' does not accept streamed input")

            self._validate_stream_mode(
                tool_name=name,
                direction="input",
                spec=tool.input_spec,
                stream=True,
                validation_mode=validation_mode,
            )

            async def validated_input_stream() -> AsyncIterable[Content]:
                async for item in input:
                    normalized_item = ensure_content(item)
                    self._validate_input_content(
                        tool=tool,
                        content=normalized_item,
                        validate_schema=True,
                        validation_mode=validation_mode,
                    )
                    yield normalized_item

            execution_input = validated_input_stream()
        else:
            normalized_input = ensure_content(input)
            self._validate_stream_mode(
                tool_name=name,
                direction="input",
                spec=tool.input_spec,
                stream=False,
                validation_mode=validation_mode,
            )

            if isinstance(tool, ContentTool):
                self._validate_input_content(
                    tool=tool,
                    content=normalized_input,
                    validate_schema=True,
                    validation_mode=validation_mode,
                )
                execution_input = normalized_input
            else:
                if isinstance(normalized_input, EmptyContent):
                    normalized_arguments = {}
                elif isinstance(normalized_input, JsonContent):
                    if not isinstance(normalized_input.json, dict):
                        raise InvalidToolDataException(
                            f"tool '{name}' requires JSON object input"
                        )
                    normalized_arguments = normalized_input.json
                else:
                    raise InvalidToolDataException(
                        f"tool '{name}' requires JSON object input"
                    )

                self._validate_input_content(
                    tool=tool,
                    content=JsonContent(json=normalized_arguments),
                    validate_schema=False,
                    validation_mode=validation_mode,
                )
                execution_input = JsonContent(json=normalized_arguments)

        execution_result = await self.execute(
            context=context,
            name=name,
            input=execution_input,
            validate_function_schema=validation_mode == "full",
        )

        if isinstance(execution_result, AsyncIterable):
            self._validate_stream_mode(
                tool_name=name,
                direction="output",
                spec=tool.output_spec,
                stream=True,
                validation_mode=validation_mode,
            )

            async def validated_output_stream() -> AsyncIterable[Content]:
                async for item in execution_result:
                    normalized_item = ensure_content(item)
                    self._validate_output_content(
                        tool=tool,
                        content=normalized_item,
                        validation_mode=validation_mode,
                    )
                    yield normalized_item

            return validated_output_stream()

        normalized_output = ensure_content(execution_result)
        self._validate_stream_mode(
            tool_name=name,
            direction="output",
            spec=tool.output_spec,
            stream=False,
            validation_mode=validation_mode,
        )
        self._validate_output_content(
            tool=tool,
            content=normalized_output,
            validation_mode=validation_mode,
        )
        return normalized_output

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
