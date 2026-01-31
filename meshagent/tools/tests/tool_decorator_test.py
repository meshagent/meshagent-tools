import pytest
from pydantic import BaseModel

from meshagent.api.messaging import JsonResponse
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

    assert isinstance(result, JsonResponse)
    assert result.json == {"name": "alpha", "count": 2, "flag": True}


def test_decorator_schema_is_strict():
    schema = make_payload.input_schema

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "payload" in schema["properties"]
    assert "flag" in schema["properties"]
