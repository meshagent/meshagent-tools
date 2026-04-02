import pytest
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel

import meshagent.tools.toolkit as toolkit_module
from meshagent.api import ToolContentSpec
from meshagent.api.messaging import JsonContent
from meshagent.api.specs.service import ContainerMountSpec
from meshagent.tools import FunctionTool, ToolContext, Toolkit, tool


_ADD_INPUT_SCHEMA = {
    "type": "object",
    "required": ["a", "b"],
    "additionalProperties": False,
    "properties": {
        "a": {"type": "integer"},
        "b": {"type": "integer"},
    },
}

_ADD_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["c"],
    "additionalProperties": False,
    "properties": {
        "c": {"type": "integer"},
    },
}


class _AddTool(FunctionTool):
    def __init__(self) -> None:
        super().__init__(
            name="add",
            input_schema=_ADD_INPUT_SCHEMA,
            output_spec=ToolContentSpec(
                types=["json"],
                stream=False,
                schema=_ADD_OUTPUT_SCHEMA,
            ),
        )

    async def execute(self, context: ToolContext, *, a: int, b: int):
        del context
        return {"c": a + b}


class _NestedPayload(BaseModel):
    value: str


class _NestedPayloadTool(FunctionTool):
    def __init__(self) -> None:
        super().__init__(
            name="nested_payload",
            input_schema={
                "type": "object",
                "required": ["payload"],
                "additionalProperties": False,
                "properties": {
                    "payload": {"type": "object"},
                },
            },
            output_spec=ToolContentSpec(types=["json"], stream=False),
        )

    async def execute(self, context: ToolContext, *, payload: _NestedPayload):
        del context
        return {"value": payload.value}


class _RecordedSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, object] = {}

    def set_attribute(self, name: str, value: object) -> None:
        self.attributes[name] = value

    def set_attributes(self, attributes: dict[str, object]) -> None:
        self.attributes.update(attributes)


class _RecordedSpanContext:
    def __init__(self, spans: list[_RecordedSpan], name: str) -> None:
        self._spans = spans
        self._span = _RecordedSpan(name)

    def __enter__(self) -> _RecordedSpan:
        self._spans.append(self._span)
        return self._span

    def __exit__(self, exc_type, exc, tb) -> bool:
        del exc_type, exc, tb
        return False


class _RecordedTracer:
    def __init__(self) -> None:
        self.spans: list[_RecordedSpan] = []

    def start_as_current_span(self, name: str) -> _RecordedSpanContext:
        return _RecordedSpanContext(self.spans, name)


@tool(name="count_mounts")
def count_mounts(*, mounts: list[ContainerMountSpec]) -> dict[str, int]:
    return {"count": len(mounts)}


@pytest.mark.asyncio
async def test_toolkit_uses_pydantic_argument_parsing_when_input_validation_is_relaxed():
    toolkit = Toolkit(
        name="test",
        tools=[_AddTool()],
        validation_mode="content_types",
    )
    context = ToolContext(room=object(), caller=object())

    result = await toolkit.invoke(
        context=context,
        name="add",
        input=JsonContent(json={"a": "1", "b": 2, "ignored": True}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"c": 3}


@pytest.mark.asyncio
async def test_toolkit_full_validation_mode_still_rejects_non_strict_inputs():
    toolkit = Toolkit(name="test", tools=[_AddTool()])
    context = ToolContext(room=object(), caller=object())

    with pytest.raises(
        JsonSchemaValidationError,
        match="Additional properties are not allowed",
    ):
        await toolkit.invoke(
            context=context,
            name="add",
            input=JsonContent(json={"a": "1", "b": 2, "ignored": True}),
        )


@pytest.mark.asyncio
async def test_toolkit_content_types_mode_allows_optional_nested_pydantic_fields():
    toolkit = Toolkit(
        name="test",
        tools=[count_mounts],
        validation_mode="content_types",
    )
    context = ToolContext(room=object(), caller=object())

    result = await toolkit.invoke(
        context=context,
        name="count_mounts",
        input=JsonContent(
            json={"mounts": [{"room": [{"path": "/", "read_only": False}]}]}
        ),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"count": 1}


@pytest.mark.asyncio
async def test_toolkit_content_types_mode_supports_manual_nested_pydantic_arguments():
    toolkit = Toolkit(
        name="test",
        tools=[_NestedPayloadTool()],
        validation_mode="content_types",
    )
    context = ToolContext(room=object(), caller=object())

    result = await toolkit.invoke(
        context=context,
        name="nested_payload",
        input=JsonContent(json={"payload": {"value": "ok"}}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"value": "ok"}


@pytest.mark.asyncio
async def test_toolkit_execute_uses_descriptive_span_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_tracer = _RecordedTracer()
    monkeypatch.setattr(toolkit_module, "tracer", recorded_tracer)
    toolkit = Toolkit(name="math-toolkit", tools=[_AddTool()])
    context = ToolContext(room=object(), caller=object())

    result = await toolkit.execute(
        context=context,
        name="add",
        input=JsonContent(json={"a": 1, "b": 2}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"c": 3}
    assert [span.name for span in recorded_tracer.spans] == ["execute.math-toolkit.add"]
    assert recorded_tracer.spans[0].attributes["toolkit"] == "math-toolkit"
    assert recorded_tracer.spans[0].attributes["tool"] == "add"


@pytest.mark.asyncio
async def test_toolkit_full_validation_mode_rejects_optional_nested_pydantic_fields():
    toolkit = Toolkit(name="test", tools=[count_mounts])
    context = ToolContext(room=object(), caller=object())

    with pytest.raises(
        JsonSchemaValidationError,
        match="'project' is a required property",
    ):
        await toolkit.invoke(
            context=context,
            name="count_mounts",
            input=JsonContent(
                json={"mounts": [{"room": [{"path": "/", "read_only": False}]}]}
            ),
        )
