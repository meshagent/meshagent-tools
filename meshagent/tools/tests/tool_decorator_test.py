import pytest
from pydantic import BaseModel

from meshagent.api.messaging import JsonChunk, TextChunk
from meshagent.tools import Toolkit, ToolContext, tool


class Payload(BaseModel):
    name: str
    count: int


class Result(BaseModel):
    name: str
    count: int
    flag: bool


@tool(name="make_payload")
async def make_payload(context: ToolContext, payload: Payload, flag: bool):
    return Result(name=payload.name, count=payload.count, flag=flag)


@pytest.mark.asyncio
async def test_decorated_tool_executes_with_toolkit():
    toolkit = Toolkit(name="test", tools=[make_payload])
    context = ToolContext(room=object(), caller=object())

    result = await toolkit.execute(
        context=context,
        name="make_payload",
        arguments={"payload": {"name": "alpha", "count": 2}, "flag": True},
    )

    assert isinstance(result, JsonChunk)
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
    context = ToolContext(room=object(), caller=object())

    result = await toolkit.execute(
        context=context,
        name="greet",
        arguments={"name": "mesh"},
    )

    assert isinstance(result, TextChunk)
    assert result.text == "hello mesh"


def test_decorator_schema_is_strict():
    schema = make_payload.input_schema

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "payload" in schema["properties"]
    assert "flag" in schema["properties"]
