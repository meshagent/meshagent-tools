import pytest

from meshagent.tools import ToolContext
from meshagent.tools.uuid import UuidV4Tool, UUIDToolkit


@pytest.mark.asyncio
async def test_uuid_tool_direct_execute_matches_python_count_coercion() -> None:
    tool = UuidV4Tool()
    context = ToolContext(caller=object())

    assert "uuid" in await tool.execute(context, count=1)
    assert "uuid" in await tool.execute(context, count=True)
    assert "uuid" in await tool.execute(context, count=1.0)

    for count in (None, 0, False, "", [], {}):
        result = await tool.execute(context, count=count)
        assert set(result) == {"uuids", "count"}
        assert len(result["uuids"]) == 1
        assert result["count"] == 1

    numeric_string = await tool.execute(context, count=" 3 ")
    assert len(numeric_string["uuids"]) == 3
    assert numeric_string["count"] == 3

    one_string = await tool.execute(context, count="1")
    assert set(one_string) == {"uuids", "count"}
    assert len(one_string["uuids"]) == 1
    assert one_string["count"] == 1

    float_count = await tool.execute(context, count=2.7)
    assert len(float_count["uuids"]) == 2
    assert float_count["count"] == 2

    negative = await tool.execute(context, count=-1)
    assert negative == {"uuids": [], "count": 0}

    with pytest.raises(ValueError, match="invalid literal for int"):
        await tool.execute(context, count="bad")
    with pytest.raises(TypeError, match="not 'list'"):
        await tool.execute(context, count=[1])


def test_uuid_toolkit_schema_remains_strict_for_openai() -> None:
    toolkit = UUIDToolkit()
    [tool] = toolkit.tools
    assert tool.name == "uuid_v4"
    assert (tool.input_schema or {}).get("required") == ["count"]
