from meshagent.api.room_server_client import RoomClient, ToolContentSpec
from meshagent.api.participant import Participant
import logging
from abc import ABC

from typing import Optional, Dict, Any, Callable, get_type_hints
from collections.abc import AsyncIterable

import inspect

from pydantic import BaseModel, create_model

from meshagent.tools.strict_schema import ensure_strict_json_schema

from meshagent.api.messaging import Content, ensure_content


from opentelemetry import trace

tracer = trace.get_tracer("meshagent.tools")

logger = logging.getLogger("tools")


class ToolContext:
    def __init__(
        self,
        *,
        room: RoomClient,
        caller: Participant,
        on_behalf_of: Optional[Participant] = None,
        caller_context: Optional[Dict[str, Any]] = None,
        event_handler: Optional[Callable[[dict], None]] = None,
    ):
        self._room = room
        self._caller = caller
        self._on_behalf_of = on_behalf_of
        self._caller_context = caller_context
        self._event_handler = event_handler

    @property
    def caller(self) -> Participant:
        return self._caller

    @property
    def on_behalf_of(self) -> Optional[Participant] | None:
        return self._on_behalf_of

    @property
    def room(self) -> RoomClient:
        return self._room

    @property
    def caller_context(self) -> Optional[Dict[str, Any]]:
        return self._caller_context

    def emit(self, event: dict):
        if self._event_handler is not None:
            self._event_handler(event)


class BaseTool(ABC):
    def __init__(
        self,
        *,
        name: str,
        input_schema: dict | None = None,
        input_spec: ToolContentSpec | None = None,
        output_spec: ToolContentSpec | None = None,
        output_schema: dict | None = None,
        defs: Optional[dict[str, dict]] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        rules: Optional[list[str]] = None,
        thumbnail_url: Optional[str] = None,
        pricing: Optional[str] = None,
        supports_context: Optional[bool] = None,
    ):
        if supports_context is None:
            supports_context = False

        self.name = name

        if title is None:
            title = name
        self.title = title

        if description is None:
            description = ""

        self.description = description
        self.rules = rules
        self.thumbnail_url = thumbnail_url
        self.pricing = pricing
        if input_schema is not None and not isinstance(input_schema, dict):
            raise TypeError("input_schema must be a dict when provided")
        if input_spec is not None and not isinstance(input_spec, ToolContentSpec):
            raise TypeError("input_spec must be a ToolContentSpec when provided")
        if output_spec is not None and not isinstance(output_spec, ToolContentSpec):
            raise TypeError("output_spec must be a ToolContentSpec when provided")
        if output_schema is not None and not isinstance(output_schema, dict):
            raise TypeError("output_schema must be a dict when provided")
        if defs is not None and not isinstance(defs, dict):
            raise TypeError("defs must be a dict when provided")

        resolved_input_schema = input_schema
        if input_spec is not None:
            if input_schema is not None:
                if input_spec.schema is not None and input_spec.schema != input_schema:
                    raise ValueError("input_schema conflicts with input_spec.schema")
                input_spec = ToolContentSpec(
                    types=[*input_spec.types],
                    stream=input_spec.stream,
                    schema=input_schema,
                )
                resolved_input_schema = input_schema
            elif input_spec.schema is not None:
                resolved_input_schema = input_spec.schema

        resolved_output_schema = output_schema
        if output_spec is not None:
            if output_schema is not None:
                if (
                    output_spec.schema is not None
                    and output_spec.schema != output_schema
                ):
                    raise ValueError("output_schema conflicts with output_spec.schema")
                output_spec = ToolContentSpec(
                    types=[*output_spec.types],
                    stream=output_spec.stream,
                    schema=output_schema,
                )
                resolved_output_schema = output_schema
            elif output_spec.schema is not None:
                resolved_output_schema = output_spec.schema

        self.input_spec = input_spec
        self.output_spec = output_spec
        self._input_schema = resolved_input_schema
        self._output_schema = resolved_output_schema
        self.defs = defs

        self.supports_context = supports_context

    @property
    def input_schema(self) -> dict | None:
        return self._input_schema

    @property
    def output_schema(self) -> dict | None:
        return self._output_schema


class FunctionTool(BaseTool):
    def __init__(
        self,
        *,
        name: str,
        input_schema: dict,
        output_spec: ToolContentSpec | None = None,
        output_schema: dict | None = None,
        defs: Optional[dict[str, dict]] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        rules: Optional[list[str]] = None,
        thumbnail_url: Optional[str] = None,
        pricing: Optional[str] = None,
        supports_context: Optional[bool] = None,
    ):
        if not isinstance(input_schema, dict):
            raise TypeError("input_schema must be a dict")

        # FunctionTool always accepts a single JSON object as input.
        fixed_input_spec = ToolContentSpec(
            types=["json"],
            stream=False,
            schema=input_schema,
        )
        super().__init__(
            name=name,
            input_spec=fixed_input_spec,
            output_spec=output_spec,
            output_schema=output_schema,
            defs=defs,
            title=title,
            description=description,
            rules=rules,
            thumbnail_url=thumbnail_url,
            pricing=pricing,
            supports_context=supports_context,
        )

    async def execute(self, context: ToolContext, **kwargs):
        raise (Exception("Not implemented"))


class ContentTool(BaseTool):
    def __init__(
        self,
        *,
        name: str,
        input_schema: dict | None = None,
        input_spec: ToolContentSpec | None = None,
        output_spec: ToolContentSpec | None = None,
        output_schema: dict | None = None,
        defs: Optional[dict[str, dict]] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        rules: Optional[list[str]] = None,
        thumbnail_url: Optional[str] = None,
        pricing: Optional[str] = None,
        supports_context: Optional[bool] = None,
    ):
        if input_schema is not None and not isinstance(input_schema, dict):
            raise TypeError("input_schema must be a dict when provided")

        super().__init__(
            name=name,
            input_schema=input_schema,
            input_spec=input_spec,
            output_spec=output_spec,
            output_schema=output_schema,
            defs=defs,
            title=title,
            description=description,
            rules=rules,
            thumbnail_url=thumbnail_url,
            pricing=pricing,
            supports_context=supports_context,
        )

    async def execute(
        self,
        *,
        context: ToolContext,
        input: AsyncIterable[Content] | Content,
    ) -> AsyncIterable[Content] | Content:
        raise (Exception("Not implemented"))


def tool(
    *,
    name: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    rules: Optional[list[str]] = None,
    thumbnail_url: Optional[str] = None,
):
    def decorator(fn: Callable[..., Content]):
        signature = inspect.signature(fn)
        hints = get_type_hints(fn, include_extras=True)

        supports_context = False
        fields: dict[str, tuple[Any, Any]] = {}

        parameters = list(signature.parameters.items())
        bound_param_name = None
        if parameters:
            first_param_name, _first_param = parameters[0]
            if first_param_name in ("self", "cls"):
                bound_param_name = first_param_name

        for param_name, param in parameters:
            if bound_param_name == param_name:
                continue
            annotation = hints.get(param_name, Any)
            if annotation is ToolContext:
                supports_context = True
                continue

            default = param.default if param.default is not inspect._empty else ...
            fields[param_name] = (annotation, default)

        InputModel = create_model(f"{fn.__name__}Input", **fields)
        schema = InputModel.model_json_schema()
        strict_schema = ensure_strict_json_schema(schema)

        tool_name = name or fn.__name__
        tool_title = title or tool_name
        tool_description = (
            description if description is not None else (fn.__doc__ or "").strip()
        )

        class DecoratedFunctionTool(FunctionTool):
            def __init__(self, bound_instance: Optional[object] = None):
                super().__init__(
                    name=tool_name,
                    title=tool_title,
                    description=tool_description,
                    rules=rules,
                    thumbnail_url=thumbnail_url,
                    input_schema=strict_schema,
                    supports_context=supports_context,
                )
                self.strict = True
                self._bound_instance = bound_instance

            def __get__(self, instance, owner):
                if instance is None:
                    if bound_param_name == "cls" and owner is not None:
                        if self._bound_instance is owner:
                            return self
                        return DecoratedFunctionTool(bound_instance=owner)
                    return self

                if self._bound_instance is instance:
                    return self

                return DecoratedFunctionTool(bound_instance=instance)

            async def execute(
                self,
                context: ToolContext,
                **arguments,
            ):
                data = InputModel.model_validate(arguments)
                parsed_args = {field: getattr(data, field) for field in fields}

                bound_instance = self._bound_instance

                if supports_context:
                    if bound_instance is not None:
                        result = fn(bound_instance, context, **parsed_args)
                    else:
                        result = fn(context, **parsed_args)
                else:
                    if bound_instance is not None:
                        result = fn(bound_instance, **parsed_args)
                    else:
                        result = fn(**parsed_args)

                if inspect.isawaitable(result):
                    result = await result

                if isinstance(result, AsyncIterable):
                    return result

                if isinstance(result, BaseModel):
                    result = result.model_dump(mode="json")

                return ensure_content(result)

        return DecoratedFunctionTool()

    return decorator
