from meshagent.api.room_server_client import RoomClient, ToolContentSpec
from meshagent.api.participant import Participant
import logging
from abc import ABC

from typing import (
    Optional,
    Any,
    Callable,
    Union,
    Literal,
    get_args,
    get_origin,
    get_type_hints,
)
from collections.abc import AsyncIterable
from types import NoneType, UnionType

import inspect

from pydantic import BaseModel, ConfigDict, create_model

from meshagent.tools.strict_schema import ensure_strict_json_schema

from meshagent.api.messaging import Content, ensure_content


from opentelemetry import trace

tracer = trace.get_tracer("meshagent.tools")

logger = logging.getLogger("tools")

ValidationMode = Literal["full", "content_types", "none"]


def _strict_model_schema(model_type: type[BaseModel]) -> dict[str, Any]:
    return ensure_strict_json_schema(model_type.model_json_schema())


def _optional_json_output_schema(model_type: type[BaseModel]) -> dict[str, Any]:
    model_schema = model_type.model_json_schema()
    defs = model_schema.pop("$defs", None)
    schema: dict[str, Any] = {
        "anyOf": [
            model_schema,
            {"type": "null"},
        ]
    }
    if isinstance(defs, dict) and len(defs) > 0:
        schema["$defs"] = defs
    return ensure_strict_json_schema(schema)


def _create_execution_input_model(
    *,
    name: str,
    fn: Callable[..., Any],
) -> tuple[type[BaseModel] | None, tuple[str, ...]]:
    signature = inspect.signature(fn)
    hints = get_type_hints(fn, include_extras=True)
    fields: dict[str, tuple[Any, Any]] = {}

    for param_name, param in signature.parameters.items():
        if param_name in ("self", "cls", "context"):
            continue
        hint = hints.get(param_name)
        if inspect.isclass(hint) and issubclass(hint, ToolContext):
            continue
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            return None, ()

        annotation = hints.get(param_name, Any)
        default = param.default if param.default is not inspect._empty else ...
        fields[param_name] = (annotation, default)

    model = create_model(
        name,
        __config__=ConfigDict(extra="ignore"),
        **fields,
    )
    return model, tuple(fields)


def _infer_output_spec_from_annotation(
    annotation: Any,
) -> tuple[ToolContentSpec | None, dict[str, Any] | None]:
    if annotation in (inspect.Signature.empty, Any):
        return None, None

    if annotation in (None, NoneType):
        return ToolContentSpec(types=["empty"], stream=False), None

    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        schema = _strict_model_schema(annotation)
        return ToolContentSpec(types=["json"], stream=False, schema=schema), schema

    origin = get_origin(annotation)
    if origin not in (UnionType, Union):
        return None, None

    args = [arg for arg in get_args(annotation) if arg is not NoneType]
    if len(args) != 1:
        return None, None

    model_type = args[0]
    if not inspect.isclass(model_type) or not issubclass(model_type, BaseModel):
        return None, None

    schema = _optional_json_output_schema(model_type)
    return ToolContentSpec(types=["json", "empty"], stream=False, schema=schema), schema


class ToolContext:
    def __init__(
        self,
        *,
        caller: Participant,
        on_behalf_of: Optional[Participant] = None,
        event_handler: Optional[Callable[[dict], None]] = None,
    ):
        self._caller = caller
        self._on_behalf_of = on_behalf_of
        self._event_handler = event_handler

    @property
    def caller(self) -> Participant:
        return self._caller

    @property
    def on_behalf_of(self) -> Optional[Participant] | None:
        return self._on_behalf_of

    def emit(self, event: dict):
        if self._event_handler is not None:
            self._event_handler(event)


class RoomToolContext(ToolContext):
    def __init__(
        self,
        *,
        room: RoomClient,
        caller: Participant,
        on_behalf_of: Optional[Participant] = None,
        event_handler: Optional[Callable[[dict], None]] = None,
    ):
        super().__init__(
            caller=caller,
            on_behalf_of=on_behalf_of,
            event_handler=event_handler,
        )
        self._room = room

    @property
    def room(self) -> RoomClient:
        return self._room


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
    ):
        self.name = name

        if title is None:
            title = name
        self.title = title

        if description is None:
            description = ""

        self.description = description
        self.rules = rules
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
        strict: bool = True,
        output_spec: ToolContentSpec | None = None,
        output_schema: dict | None = None,
        defs: Optional[dict[str, dict]] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        rules: Optional[list[str]] = None,
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
        )
        (
            self._execution_input_model,
            self._execution_input_fields,
        ) = _create_execution_input_model(
            name=f"{self.__class__.__name__}ExecutionInput",
            fn=self.execute,
        )
        self.strict = strict

    async def execute(self, context: ToolContext, **kwargs):
        raise (Exception("Not implemented"))

    def normalize_execution_arguments(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if self._execution_input_model is None:
            return arguments

        parsed = self._execution_input_model.model_validate(arguments)
        return {
            field_name: getattr(parsed, field_name)
            for field_name in self._execution_input_fields
        }


class LocalRoomTool(FunctionTool):
    def __init__(
        self,
        *,
        room: RoomClient,
        **kwargs: Any,
    ) -> None:
        self._room = room
        super().__init__(**kwargs)

    @property
    def room(self) -> RoomClient:
        return self._room


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
):
    def decorator(fn: Callable[..., Content]):
        signature = inspect.signature(fn)
        hints = get_type_hints(fn, include_extras=True)
        inferred_output_spec, inferred_output_schema = (
            _infer_output_spec_from_annotation(
                hints.get("return", inspect.Signature.empty)
            )
        )

        accepts_context = False
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
            if inspect.isclass(annotation) and issubclass(annotation, ToolContext):
                accepts_context = True
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
                    input_schema=strict_schema,
                    output_spec=inferred_output_spec,
                    output_schema=inferred_output_schema,
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

                if accepts_context:
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
