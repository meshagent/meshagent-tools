import pytest
from pydantic import BaseModel

from meshagent.api.messaging import JsonContent, TextContent
from meshagent.tools import Toolkit, ToolContext, tool


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


def test_decorator_schema_is_strict():
    assert make_payload.input_spec is not None
    schema = make_payload.input_spec.schema
    assert schema is not None

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "payload" in schema["properties"]
    assert "flag" in schema["properties"]


def test_decorator_infers_strict_json_output_schema() -> None:
    assert make_payload.output_spec is not None
    assert make_payload.output_spec.types == ["json"]
    assert make_payload.output_spec.stream is False
    assert make_payload.output_spec.schema is not None
    assert make_payload.output_spec.schema["additionalProperties"] is False
    assert set(make_payload.output_spec.schema["required"]) == {"name", "count", "flag"}


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
    assert maybe_payload.output_spec.schema is not None
    assert "anyOf" in maybe_payload.output_spec.schema
