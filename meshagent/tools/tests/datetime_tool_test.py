import pytest
from jsonschema.exceptions import ValidationError

from meshagent.api.messaging import JsonContent
from meshagent.tools import ToolContext
from meshagent.tools.datetime import DatetimeToolkit


@pytest.mark.asyncio
async def test_now_tool_accepts_empty_arguments():
    toolkit = DatetimeToolkit()
    context = ToolContext(caller=object())

    result = await toolkit.execute(
        context=context,
        name="now",
        input=JsonContent(json={}),
    )

    assert isinstance(result, JsonContent)
    assert "utc" in result.json
    assert "local" not in result.json
    assert "tz" not in result.json


def test_datetime_tool_schemas_remain_strict_for_openai():
    toolkit = DatetimeToolkit()
    required_by_tool = {
        tool.name: set((tool.input_schema or {}).get("required", []))
        for tool in toolkit.tools
    }

    assert required_by_tool["now"] == {"tz"}
    assert required_by_tool["today_range"] == {"tz"}
    assert required_by_tool["week_range"] == {"dt", "tz", "week_start"}
    assert required_by_tool["month_range"] == {"dt", "tz"}
    assert required_by_tool["add_duration"] == {
        "dt",
        "tz",
        "days",
        "hours",
        "minutes",
        "seconds",
    }
    assert required_by_tool["diff"] == {"dt1", "dt2", "assume_tz"}
    assert required_by_tool["parse_iso"] == {"dt", "assume_tz", "tz"}
    assert required_by_tool["format_dt"] == {"dt", "fmt", "assume_tz", "tz"}
    assert required_by_tool["to_utc_z"] == {"dt", "assume_tz", "drop_microseconds"}


@pytest.mark.asyncio
async def test_non_nullable_required_fields_are_not_auto_filled():
    toolkit = DatetimeToolkit()
    context = ToolContext(caller=object())

    with pytest.raises(ValidationError):
        await toolkit.execute(
            context=context,
            name="add_duration",
            input=JsonContent(json={}),
        )
