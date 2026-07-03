import pytest

from meshagent.api import RoomException, ToolContentSpec
from meshagent.tools import ContentTool, FunctionTool
from meshagent.tools.strict_schema import ensure_strict_json_schema


def test_tool_forces_json_input_type() -> None:
    tool = FunctionTool(
        name="sample",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    )

    assert tool.input_spec is not None
    assert tool.input_spec.types == ["json"]
    assert tool.input_spec.stream is False


def test_tool_requires_input_schema_dict() -> None:
    with pytest.raises(TypeError, match="input_schema must be a dict"):
        FunctionTool(name="sample", input_schema=None)  # type: ignore[arg-type]


def test_strict_schema_rejects_extra_properties_with_source_neutral_error() -> None:
    with pytest.raises(RoomException, match="generated schema allows extra properties"):
        ensure_strict_json_schema(
            {
                "type": "object",
                "additionalProperties": True,
            }
        )


def test_stream_tool_preserves_declared_content_types() -> None:
    input_spec = ToolContentSpec(types=["text", "json"], stream=True)
    output_spec = ToolContentSpec(types=["json", "text"], stream=True)

    tool = ContentTool(
        name="sample_stream",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        input_spec=input_spec,
        output_spec=output_spec,
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"value": {"type": "number"}},
        },
        defs={"Value": {"type": "number"}},
    )

    assert tool.input_spec is not None
    assert tool.input_spec.types == input_spec.types
    assert tool.input_spec.stream == input_spec.stream
    assert tool.input_spec.schema == {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }
    assert tool.output_spec is not None
    assert tool.output_spec.types == output_spec.types
    assert tool.output_spec.stream == output_spec.stream
    assert tool.output_spec.schema == {
        "type": "object",
        "additionalProperties": False,
        "properties": {"value": {"type": "number"}},
    }
    assert tool.output_schema == {
        "type": "object",
        "additionalProperties": False,
        "properties": {"value": {"type": "number"}},
    }
    assert tool.defs == {"Value": {"type": "number"}}
