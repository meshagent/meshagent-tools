import inspect
from typing import Annotated, Any, Literal, Union

import pytest
from pydantic import BaseModel, Field

from meshagent.api.messaging import JsonContent, TextContent
from meshagent.tools import Toolkit, ToolContext, tool
from meshagent.tools.tool import (
    _create_execution_input_model,
    _infer_output_spec_from_annotation,
)


class Payload(BaseModel):
    name: str
    count: int


class Result(BaseModel):
    name: str
    count: int
    flag: bool


@tool(name="make_payload")
async def make_payload(context: ToolContext, payload: Payload, flag: bool) -> Result:
    return Result(name=payload.name, count=payload.count, flag=flag)


@tool(name="echo_unannotated_context")
def echo_unannotated_context(context: str) -> str:
    return context


@tool(name="typed_params")
def typed_params(*, count: int, label: str = "x", active: bool = False) -> str:
    return f"{count}:{label}:{active}"


@tool(name="list_params")
def list_params(*, tags: list[str]) -> str:
    return ",".join(tags)


@tool(name="dict_params")
def dict_params(*, scores: dict[str, int]) -> int:
    return sum(scores.values())


@tool(name="nullable_params")
def nullable_params(*, nickname: str | None = None) -> str:
    return nickname or ""


@tool(name="literal_params")
def literal_params(*, mode: Literal["fast", "slow"]) -> str:
    return mode


@tool(name="bounded_params")
def bounded_params(*, score: Annotated[int, Field(ge=1, le=5)]) -> int:
    return score


@pytest.mark.asyncio
async def test_decorated_tool_executes_with_toolkit():
    toolkit = Toolkit(name="test", tools=[make_payload])
    context = ToolContext(caller=object())

    result = await toolkit.execute(
        context=context,
        name="make_payload",
        input=JsonContent(
            json={"payload": {"name": "alpha", "count": 2}, "flag": True}
        ),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"name": "alpha", "count": 2, "flag": True}


@pytest.mark.asyncio
async def test_decorator_treats_unannotated_context_as_input_field():
    schema = echo_unannotated_context.input_spec.schema
    assert schema is not None
    assert "context" in schema["properties"]
    assert schema["required"] == ["context"]

    toolkit = Toolkit(name="test", tools=[echo_unannotated_context])
    with pytest.raises(
        TypeError, match="multiple values for keyword argument 'context'"
    ):
        await toolkit.execute(
            context=ToolContext(caller=object()),
            name="echo_unannotated_context",
            input=JsonContent(json={"context": "from input"}),
        )


class Greeter:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    @tool(name="greet")
    def greet(self, *, name: str) -> str:
        return f"{self._prefix}{name}"


@pytest.mark.asyncio
async def test_decorated_method_executes_with_toolkit():
    greeter = Greeter("hello ")
    toolkit = Toolkit(name="test", tools=[greeter.greet])
    context = ToolContext(caller=object())

    result = await toolkit.execute(
        context=context,
        name="greet",
        input=JsonContent(json={"name": "mesh"}),
    )

    assert isinstance(result, TextContent)
    assert result.text == "hello mesh"


class ClassGreeter:
    prefix = "class "

    @tool(name="class_greet")
    def class_greet(cls, *, name: str) -> str:
        return f"{cls.prefix}{name}"


@pytest.mark.asyncio
async def test_decorated_cls_method_binds_owner_when_accessed_on_class():
    toolkit = Toolkit(name="test", tools=[ClassGreeter.class_greet])
    context = ToolContext(caller=object())

    result = await toolkit.execute(
        context=context,
        name="class_greet",
        input=JsonContent(json={"name": "mesh"}),
    )

    assert isinstance(result, TextContent)
    assert result.text == "class mesh"


def test_create_execution_input_model_skips_context_and_rejects_unsupported_params():
    def supported(
        self,
        cls,
        context,
        typed_context: ToolContext,
        value: int,
        *,
        flag: bool = False,
    ):
        return value, flag

    model, fields = _create_execution_input_model(name="SupportedInput", fn=supported)

    assert model is not None
    assert fields == ("value", "flag")
    assert tuple(model.model_fields) == ("value", "flag")
    assert model.model_validate(
        {"value": "3", "flag": True, "extra": "ignored"}
    ).model_dump() == {"value": 3, "flag": True}

    def unsupported_positional_only(value: int, /):
        return value

    assert _create_execution_input_model(
        name="UnsupportedPositionalOnlyInput", fn=unsupported_positional_only
    ) == (None, ())

    def unsupported_varargs(*values: int):
        return values

    assert _create_execution_input_model(
        name="UnsupportedVarargsInput", fn=unsupported_varargs
    ) == (None, ())

    def unsupported_kwargs(**values: int):
        return values

    assert _create_execution_input_model(
        name="UnsupportedKwargsInput", fn=unsupported_kwargs
    ) == (None, ())


def test_decorator_schema_is_strict():
    assert make_payload.input_spec is not None
    schema = make_payload.input_spec.schema
    assert schema is not None

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "payload" in schema["properties"]
    assert "flag" in schema["properties"]


def test_decorator_generates_strict_input_schema_for_basemodel_parameters() -> None:
    assert make_payload.input_schema == {
        "$defs": {
            "Payload": {
                "properties": {
                    "name": {"title": "Name", "type": "string"},
                    "count": {"title": "Count", "type": "integer"},
                },
                "required": ["name", "count"],
                "title": "Payload",
                "type": "object",
                "additionalProperties": False,
            }
        },
        "properties": {
            "payload": {"$ref": "#/$defs/Payload"},
            "flag": {"title": "Flag", "type": "boolean"},
        },
        "required": ["payload", "flag"],
        "title": "make_payloadInput",
        "type": "object",
        "additionalProperties": False,
    }


def test_decorator_generates_strict_input_schema_from_annotations() -> None:
    schema = typed_params.input_schema

    assert schema == {
        "properties": {
            "count": {"title": "Count", "type": "integer"},
            "label": {"default": "x", "title": "Label", "type": "string"},
            "active": {"default": False, "title": "Active", "type": "boolean"},
        },
        "required": ["count", "label", "active"],
        "title": "typed_paramsInput",
        "type": "object",
        "additionalProperties": False,
    }


def test_decorator_generates_strict_input_schema_for_list_annotations() -> None:
    assert list_params.input_schema == {
        "properties": {
            "tags": {
                "items": {"type": "string"},
                "title": "Tags",
                "type": "array",
            }
        },
        "required": ["tags"],
        "title": "list_paramsInput",
        "type": "object",
        "additionalProperties": False,
    }


def test_decorator_generates_strict_input_schema_for_dict_annotations() -> None:
    assert dict_params.input_schema == {
        "properties": {
            "scores": {
                "additionalProperties": {"type": "integer"},
                "title": "Scores",
                "type": "object",
                "required": [],
                "properties": {},
            }
        },
        "required": ["scores"],
        "title": "dict_paramsInput",
        "type": "object",
        "additionalProperties": False,
    }


def test_decorator_generates_strict_input_schema_for_nullable_annotations() -> None:
    assert nullable_params.input_schema == {
        "properties": {
            "nickname": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "title": "Nickname",
            }
        },
        "title": "nullable_paramsInput",
        "type": "object",
        "additionalProperties": False,
        "required": ["nickname"],
    }


def test_decorator_generates_strict_input_schema_for_literal_annotations() -> None:
    assert literal_params.input_schema == {
        "properties": {
            "mode": {
                "enum": ["fast", "slow"],
                "title": "Mode",
                "type": "string",
            }
        },
        "required": ["mode"],
        "title": "literal_paramsInput",
        "type": "object",
        "additionalProperties": False,
    }


def test_decorator_generates_strict_input_schema_for_bounded_annotations() -> None:
    assert bounded_params.input_schema == {
        "properties": {
            "score": {
                "maximum": 5,
                "minimum": 1,
                "title": "Score",
                "type": "integer",
            }
        },
        "required": ["score"],
        "title": "bounded_paramsInput",
        "type": "object",
        "additionalProperties": False,
    }


def test_decorator_infers_strict_json_output_schema() -> None:
    assert make_payload.output_spec is not None
    assert make_payload.output_spec.types == ["json"]
    assert make_payload.output_spec.stream is False
    assert make_payload.output_schema == {
        "properties": {
            "name": {"title": "Name", "type": "string"},
            "count": {"title": "Count", "type": "integer"},
            "flag": {"title": "Flag", "type": "boolean"},
        },
        "required": ["name", "count", "flag"],
        "title": "Result",
        "type": "object",
        "additionalProperties": False,
    }
    assert make_payload.output_spec.schema == make_payload.output_schema


def test_infer_output_spec_from_annotation_matches_python_branches() -> None:
    assert _infer_output_spec_from_annotation(inspect.Signature.empty) == (None, None)
    assert _infer_output_spec_from_annotation(Any) == (None, None)

    none_spec, none_schema = _infer_output_spec_from_annotation(None)
    assert none_spec is not None
    assert none_spec.types == ["empty"]
    assert none_spec.stream is False
    assert none_schema is None

    json_spec, json_schema = _infer_output_spec_from_annotation(Result)
    assert json_spec is not None
    assert json_spec.types == ["json"]
    assert json_spec.stream is False
    assert json_schema is not None
    assert json_schema["additionalProperties"] is False

    optional_spec, optional_schema = _infer_output_spec_from_annotation(
        Union[Result, None]
    )
    assert optional_spec is not None
    assert optional_spec.types == ["json", "empty"]
    assert optional_spec.stream is False
    assert optional_schema is not None
    assert "anyOf" in optional_schema

    assert _infer_output_spec_from_annotation(str) == (None, None)
    assert _infer_output_spec_from_annotation(Union[Result, MaybeResult]) == (
        None,
        None,
    )
    assert _infer_output_spec_from_annotation(Union[int, None]) == (None, None)


class MaybeResult(BaseModel):
    value: str


@tool(name="maybe_payload")
def maybe_payload(*, enabled: bool) -> MaybeResult | None:
    if not enabled:
        return None
    return MaybeResult(value="ok")


def test_decorator_infers_optional_model_output_schema() -> None:
    assert maybe_payload.output_spec is not None
    assert maybe_payload.output_spec.types == ["json", "empty"]
    assert maybe_payload.output_spec.stream is False
    assert maybe_payload.output_schema == {
        "anyOf": [
            {
                "properties": {"value": {"title": "Value", "type": "string"}},
                "required": ["value"],
                "title": "MaybeResult",
                "type": "object",
                "additionalProperties": False,
            },
            {"type": "null"},
        ]
    }
    assert maybe_payload.output_spec.schema == maybe_payload.output_schema
