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


@pytest.mark.asyncio
async def test_parse_iso_accepts_python_fromisoformat_variants():
    toolkit = DatetimeToolkit()
    context = ToolContext(caller=object())

    cases = [
        ("2026-01-11", "2026-01-11T00:00:00+00:00"),
        ("2026-01-11 12:34:56", "2026-01-11T12:34:56+00:00"),
        ("2026-01-11T12:34:56,123456", "2026-01-11T12:34:56.123456+00:00"),
        ("2026-01-11T12:34:56+0000", "2026-01-11T12:34:56+00:00"),
        ("20260111T123456", "2026-01-11T12:34:56+00:00"),
        ("2026-W02-7T12:34:56", "2026-01-11T12:34:56+00:00"),
        ("20260111", "2026-01-11T00:00:00+00:00"),
        ("2026-W02-7", "2026-01-11T00:00:00+00:00"),
        ("2026-01-11T12:34", "2026-01-11T12:34:00+00:00"),
        ("2026-01-11T12", "2026-01-11T12:00:00+00:00"),
    ]

    for value, expected_iso in cases:
        result = await toolkit.execute(
            context=context,
            name="parse_iso",
            input=JsonContent(
                json={
                    "dt": value,
                    "assume_tz": None,
                    "tz": None,
                }
            ),
        )

        assert isinstance(result, JsonContent)
        assert result.json["iso"] == expected_iso


@pytest.mark.asyncio
async def test_parse_iso_invalid_inputs_raise_python_value_error():
    toolkit = DatetimeToolkit()
    context = ToolContext(caller=object())

    for value in ("bad", "2026-011", "2026-01-11T12:bad"):
        with pytest.raises(ValueError, match=f"Invalid isoformat string: {value!r}"):
            tool = next(tool for tool in toolkit.tools if tool.name == "parse_iso")
            await tool.execute(context, dt=value, assume_tz=None, tz=None)


@pytest.mark.asyncio
async def test_parse_iso_naive_dst_transition_uses_python_zoneinfo_attachment():
    toolkit = DatetimeToolkit()
    context = ToolContext(caller=object())

    cases = [
        ("2026-11-01T01:30:00", "2026-11-01T01:30:00-04:00"),
        ("2026-03-08T02:30:00", "2026-03-08T02:30:00-05:00"),
    ]

    for value, expected_iso in cases:
        result = await toolkit.execute(
            context=context,
            name="parse_iso",
            input=JsonContent(
                json={
                    "dt": value,
                    "assume_tz": "America/New_York",
                    "tz": None,
                }
            ),
        )

        assert isinstance(result, JsonContent)
        assert result.json["iso"] == expected_iso
        assert result.json["tz"] == "America/New_York"
