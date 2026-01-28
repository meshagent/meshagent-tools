from meshagent.api.room_server_client import RoomClient
from meshagent.api.participant import Participant
import logging
from abc import ABC

from typing import Optional, Dict, Any, Callable, get_type_hints

import inspect

from pydantic import BaseModel, create_model

from meshagent.tools.strict_schema import ensure_strict_json_schema

from meshagent.api.messaging import Response, ensure_response


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
    def caller_context(self) -> Dict[str, Any]:
        return self._caller_context

    def emit(self, event: dict):
        if self._event_handler is not None:
            self._event_handler(event)


class BaseTool(ABC):
    def __init__(
        self,
        *,
        name: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        rules: Optional[list[str]] = None,
        thumbnail_url: Optional[str] = None,
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

        self.supports_context = supports_context


class Tool(BaseTool):
    def __init__(
        self,
        *,
        name: str,
        input_schema: dict,
        title: Optional[str] = None,
        description: Optional[str] = None,
        rules: Optional[list[str]] = None,
        thumbnail_url: Optional[str] = None,
        defs: Optional[dict[str, dict]] = None,
        supports_context: Optional[bool] = None,
    ):
        super().__init__(
            name=name,
            title=title,
            description=description,
            rules=rules,
            thumbnail_url=thumbnail_url,
            supports_context=supports_context,
        )

        if not isinstance(input_schema, dict):
            raise Exception(
                "schema must be a dict, got: {type}".format(type=type(input_schema))
            )

        self.input_schema = input_schema
        self.defs = defs

        openai_schema = {**input_schema}

        if defs is not None:
            openai_schema["$defs"] = {**defs}

    async def invoke(
        self, context: ToolContext, arguments: dict, attachment: Optional[bytes] = None
    ) -> Response:
        return await self.execute(context=context, **arguments)

    async def execute(self, context: ToolContext, **kwargs) -> Response:
        raise (Exception("Not implemented"))


def tool(
    *,
    name: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    rules: Optional[list[str]] = None,
    thumbnail_url: Optional[str] = None,
):
    def decorator(fn: Callable[..., Response]):
        signature = inspect.signature(fn)
        hints = get_type_hints(fn, include_extras=True)

        supports_context = False
        fields: dict[str, tuple[Any, Any]] = {}

        for param_name, param in signature.parameters.items():
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

        class FunctionTool(Tool):
            def __init__(self):
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

            async def invoke(
                self,
                context: ToolContext,
                arguments: dict,
                attachment: Optional[bytes] = None,
            ) -> Response:
                data = InputModel.model_validate(arguments)
                parsed_args = {field: getattr(data, field) for field in fields}

                if supports_context:
                    result = fn(context, **parsed_args)
                else:
                    result = fn(**parsed_args)

                if inspect.isawaitable(result):
                    result = await result

                if isinstance(result, BaseModel):
                    result = result.model_dump(mode="json")

                return ensure_response(result)

        return FunctionTool()

    return decorator
