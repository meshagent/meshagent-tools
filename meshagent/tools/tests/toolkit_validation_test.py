import pytest
from jsonschema import ValidationError as JsonSchemaValidationError
from typing import Annotated, Literal

from pydantic import BaseModel, Field

import meshagent.tools.toolkit as toolkit_module
from meshagent.api import ToolContentSpec
from meshagent.api.messaging import JsonContent
from meshagent.api.specs.service import ContainerMountSpec
from meshagent.tools import FunctionTool, ToolContext, Toolkit, tool
from meshagent.tools.pydantic import PydanticTool


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


class _CoercedPayload(BaseModel):
    count: int
    enabled: bool
    ratio: float


class _OptionalCoercedPayload(BaseModel):
    count: int | None
    enabled: bool | None
    ratio: float | None


class _BytesPayload(BaseModel):
    data: bytes


class _UnionPayload(BaseModel):
    value: int | str


class _FloatIntUnionPayload(BaseModel):
    value: float | int


class _BoolIntUnionPayload(BaseModel):
    value: bool | int


class _UnionListPayload(BaseModel):
    values: list[int | str]


class _DictPayload(BaseModel):
    counts: dict[str, int]
    values: dict[str, int | str]


class _PatternDictPayload(BaseModel):
    values: dict[Annotated[str, Field(pattern="^x_")], int]


class _TuplePayload(BaseModel):
    pair: tuple[int, bool]
    variadic: tuple[int, ...]


class _ConstrainedPayload(BaseModel):
    count: Annotated[int, Field(ge=1, le=5)]
    ratio: Annotated[float, Field(gt=0, lt=2)]


class _LiteralPayload(BaseModel):
    int_value: Literal[2]
    bool_value: Literal[True]
    str_value: Literal["2"]
    choice: Literal[1, "1"]


class _NestedChildPayload(BaseModel):
    count: Annotated[int, Field(ge=1, le=5)]
    flag: bool


class _NestedPydanticPayload(BaseModel):
    child: _NestedChildPayload


class _NestedPydanticListPayload(BaseModel):
    items: list[_NestedChildPayload]


class _DefaultPayload(BaseModel):
    count: int = 3
    enabled: bool = True
    label: str = "alpha"


class _NestedDefaultChildPayload(BaseModel):
    count: int = 3
    flag: bool = True


class _NestedDefaultPayload(BaseModel):
    child: _NestedDefaultChildPayload


class _NestedDefaultListPayload(BaseModel):
    items: list[_NestedDefaultChildPayload]


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


class _CoercionTool(PydanticTool[_CoercedPayload]):
    def __init__(self) -> None:
        super().__init__(name="coerce", input_model=_CoercedPayload)

    async def execute_model(self, *, context: ToolContext, arguments: _CoercedPayload):
        del context
        return {
            "count": arguments.count,
            "enabled": arguments.enabled,
            "ratio": arguments.ratio,
        }


class _OptionalCoercionTool(PydanticTool[_OptionalCoercedPayload]):
    def __init__(self) -> None:
        super().__init__(name="optional_coerce", input_model=_OptionalCoercedPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _OptionalCoercedPayload
    ):
        del context
        return {
            "count": arguments.count,
            "enabled": arguments.enabled,
            "ratio": arguments.ratio,
        }


class _BytesTool(PydanticTool[_BytesPayload]):
    def __init__(self) -> None:
        super().__init__(name="bytes", input_model=_BytesPayload)

    async def execute_model(self, *, context: ToolContext, arguments: _BytesPayload):
        del context
        return {"data": list(arguments.data)}


class _UnionTool(PydanticTool[_UnionPayload]):
    def __init__(self) -> None:
        super().__init__(name="union", input_model=_UnionPayload)

    async def execute_model(self, *, context: ToolContext, arguments: _UnionPayload):
        del context
        return {"type": type(arguments.value).__name__, "value": arguments.value}


class _FloatIntUnionTool(PydanticTool[_FloatIntUnionPayload]):
    def __init__(self) -> None:
        super().__init__(name="float_int_union", input_model=_FloatIntUnionPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _FloatIntUnionPayload
    ):
        del context
        return {"type": type(arguments.value).__name__, "value": arguments.value}


class _BoolIntUnionTool(PydanticTool[_BoolIntUnionPayload]):
    def __init__(self) -> None:
        super().__init__(name="bool_int_union", input_model=_BoolIntUnionPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _BoolIntUnionPayload
    ):
        del context
        return {"type": type(arguments.value).__name__, "value": arguments.value}


class _UnionListTool(PydanticTool[_UnionListPayload]):
    def __init__(self) -> None:
        super().__init__(name="union_list", input_model=_UnionListPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _UnionListPayload
    ):
        del context
        return {
            "types": [type(value).__name__ for value in arguments.values],
            "values": arguments.values,
        }


class _DictTool(PydanticTool[_DictPayload]):
    def __init__(self) -> None:
        super().__init__(name="dict_values", input_model=_DictPayload)

    async def execute_model(self, *, context: ToolContext, arguments: _DictPayload):
        del context
        return {
            "count_types": {
                key: type(value).__name__ for key, value in arguments.counts.items()
            },
            "counts": arguments.counts,
            "value_types": {
                key: type(value).__name__ for key, value in arguments.values.items()
            },
            "values": arguments.values,
        }


class _PatternDictTool(PydanticTool[_PatternDictPayload]):
    def __init__(self) -> None:
        super().__init__(name="pattern_dict_values", input_model=_PatternDictPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _PatternDictPayload
    ):
        del context
        return {
            "types": {
                key: type(value).__name__ for key, value in arguments.values.items()
            },
            "values": arguments.values,
        }


class _TupleTool(PydanticTool[_TuplePayload]):
    def __init__(self) -> None:
        super().__init__(name="tuple_values", input_model=_TuplePayload)

    async def execute_model(self, *, context: ToolContext, arguments: _TuplePayload):
        del context
        return {
            "pair_types": [type(value).__name__ for value in arguments.pair],
            "pair": list(arguments.pair),
            "variadic_types": [type(value).__name__ for value in arguments.variadic],
            "variadic": list(arguments.variadic),
        }


class _ConstrainedTool(PydanticTool[_ConstrainedPayload]):
    def __init__(self) -> None:
        super().__init__(name="constrained_values", input_model=_ConstrainedPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _ConstrainedPayload
    ):
        del context
        return {
            "count": arguments.count,
            "count_type": type(arguments.count).__name__,
            "ratio": arguments.ratio,
            "ratio_type": type(arguments.ratio).__name__,
        }


class _LiteralTool(PydanticTool[_LiteralPayload]):
    def __init__(self) -> None:
        super().__init__(name="literal_values", input_model=_LiteralPayload)

    async def execute_model(self, *, context: ToolContext, arguments: _LiteralPayload):
        del context
        return {
            "int_type": type(arguments.int_value).__name__,
            "int_value": arguments.int_value,
            "bool_type": type(arguments.bool_value).__name__,
            "bool_value": arguments.bool_value,
            "str_type": type(arguments.str_value).__name__,
            "str_value": arguments.str_value,
            "choice_type": type(arguments.choice).__name__,
            "choice": arguments.choice,
        }


class _NestedPydanticTool(PydanticTool[_NestedPydanticPayload]):
    def __init__(self) -> None:
        super().__init__(name="nested_pydantic", input_model=_NestedPydanticPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _NestedPydanticPayload
    ):
        del context
        return {
            "count": arguments.child.count,
            "count_type": type(arguments.child.count).__name__,
            "flag": arguments.child.flag,
            "flag_type": type(arguments.child.flag).__name__,
        }


class _NestedPydanticListTool(PydanticTool[_NestedPydanticListPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="nested_pydantic_list", input_model=_NestedPydanticListPayload
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _NestedPydanticListPayload
    ):
        del context
        return {
            "items": [
                {
                    "count": item.count,
                    "count_type": type(item.count).__name__,
                    "flag": item.flag,
                    "flag_type": type(item.flag).__name__,
                }
                for item in arguments.items
            ]
        }


class _DefaultTool(PydanticTool[_DefaultPayload]):
    def __init__(self) -> None:
        super().__init__(name="default_values", input_model=_DefaultPayload)

    async def execute_model(self, *, context: ToolContext, arguments: _DefaultPayload):
        del context
        return {
            "count": arguments.count,
            "count_type": type(arguments.count).__name__,
            "enabled": arguments.enabled,
            "enabled_type": type(arguments.enabled).__name__,
            "label": arguments.label,
            "label_type": type(arguments.label).__name__,
        }


class _NestedDefaultTool(PydanticTool[_NestedDefaultPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="nested_default_values", input_model=_NestedDefaultPayload
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _NestedDefaultPayload
    ):
        del context
        return {
            "count": arguments.child.count,
            "count_type": type(arguments.child.count).__name__,
            "flag": arguments.child.flag,
            "flag_type": type(arguments.child.flag).__name__,
        }


class _NestedDefaultListTool(PydanticTool[_NestedDefaultListPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="nested_default_list_values", input_model=_NestedDefaultListPayload
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _NestedDefaultListPayload
    ):
        del context
        return {
            "items": [
                {
                    "count": item.count,
                    "count_type": type(item.count).__name__,
                    "flag": item.flag,
                    "flag_type": type(item.flag).__name__,
                }
                for item in arguments.items
            ]
        }


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
    context = ToolContext(caller=object())

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
    context = ToolContext(caller=object())

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
async def test_toolkit_full_validation_reports_multiple_extra_properties() -> None:
    toolkit = Toolkit(name="test", tools=[_AddTool()])
    context = ToolContext(caller=object())

    with pytest.raises(
        JsonSchemaValidationError,
        match="Additional properties are not allowed \\('x', 'y' were unexpected\\)",
    ):
        await toolkit.invoke(
            context=context,
            name="add",
            input=JsonContent(json={"a": 1, "b": 2, "x": 3, "y": 4}),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema", "payload", "message"),
    [
        (
            {
                "type": "object",
                "required": ["outer"],
                "additionalProperties": False,
                "properties": {
                    "outer": {
                        "type": "object",
                        "properties": {"a": {}},
                        "additionalProperties": False,
                    },
                },
            },
            {"outer": {"a": 1, "z": 2, "b": 3}},
            "Additional properties are not allowed \\('b', 'z' were unexpected\\)",
        ),
        (
            {
                "type": "object",
                "properties": {"fixed": {}},
                "patternProperties": {"^x_": {}},
                "additionalProperties": False,
            },
            {"fixed": 1, "x_ok": 2, "bad": 3, "also_bad": 4},
            "'also_bad', 'bad' do not match any of the regexes: '\\^x_'",
        ),
        (
            {
                "type": "object",
                "required": ["outer"],
                "additionalProperties": False,
                "properties": {
                    "outer": {
                        "type": "object",
                        "patternProperties": {"^a": {}},
                        "additionalProperties": False,
                    },
                },
            },
            {"outer": {"abc": 1, "bad": 2, "zzz": 3}},
            "'bad', 'zzz' do not match any of the regexes: '\\^a'",
        ),
        (
            {
                "type": "object",
                "required": ["items"],
                "additionalProperties": False,
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "patternProperties": {"^a": {}},
                            "additionalProperties": False,
                        },
                    },
                },
            },
            {"items": [{"abc": 1}, {"bad": 2, "zzz": 3}]},
            "'bad', 'zzz' do not match any of the regexes: '\\^a'",
        ),
        (
            {
                "type": "object",
                "patternProperties": {
                    "^outer": {
                        "type": "object",
                        "patternProperties": {"^a": {}},
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            },
            {"outer1": {"abc": 1, "bad": 2}},
            "'bad' does not match any of the regexes: '\\^a'",
        ),
    ],
)
async def test_toolkit_full_validation_matches_nested_additional_properties_wording(
    schema: dict[str, object],
    payload: dict[str, object],
    message: str,
) -> None:
    tool = FunctionTool(
        name="schema_probe",
        input_schema=schema,
        output_spec=ToolContentSpec(types=["json"], stream=False),
    )
    toolkit = Toolkit(name="test", tools=[tool])
    context = ToolContext(caller=object())

    with pytest.raises(JsonSchemaValidationError, match=message):
        await toolkit.invoke(
            context=context,
            name="schema_probe",
            input=JsonContent(json=payload),
        )


@pytest.mark.asyncio
async def test_toolkit_content_types_mode_allows_optional_nested_pydantic_fields():
    toolkit = Toolkit(
        name="test",
        tools=[count_mounts],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

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
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="nested_payload",
        input=JsonContent(json={"payload": {"value": "ok"}}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"value": "ok"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "count": "2",
                "enabled": "true",
                "ratio": "1.5",
                "ignored": "pydantic default extra ignore",
            },
            {"count": 2, "enabled": True, "ratio": 1.5},
        ),
        (
            {
                "count": "2.0",
                "enabled": 1.0,
                "ratio": True,
                "ignored": "pydantic default extra ignore",
            },
            {"count": 2, "enabled": True, "ratio": 1.0},
        ),
    ],
)
async def test_pydantic_tool_content_types_mode_uses_model_validate_coercion(
    payload: dict[str, object],
    expected: dict[str, object],
):
    toolkit = Toolkit(
        name="test",
        tools=[_CoercionTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="coerce",
        input=JsonContent(json=payload),
    )

    assert isinstance(result, JsonContent)
    assert result.json == expected


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_coerces_optional_scalars() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_OptionalCoercionTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="optional_coerce",
        input=JsonContent(json={"count": "2.0", "enabled": "yes", "ratio": "1.5"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"count": 2, "enabled": True, "ratio": 1.5}

    null_result = await toolkit.invoke(
        context=context,
        name="optional_coerce",
        input=JsonContent(json={"count": None, "enabled": None, "ratio": None}),
    )

    assert isinstance(null_result, JsonContent)
    assert null_result.json == {"count": None, "enabled": None, "ratio": None}


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_coerces_bytes_from_string() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_BytesTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="bytes",
        input=JsonContent(json={"data": "hé"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"data": [104, 195, 169]}


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_preserves_union_exact_string() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_UnionTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    string_result = await toolkit.invoke(
        context=context,
        name="union",
        input=JsonContent(json={"value": "2"}),
    )

    assert isinstance(string_result, JsonContent)
    assert string_result.json == {"type": "str", "value": "2"}

    int_result = await toolkit.invoke(
        context=context,
        name="union",
        input=JsonContent(json={"value": 2}),
    )

    assert isinstance(int_result, JsonContent)
    assert int_result.json == {"type": "int", "value": 2}


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_uses_first_coercible_union_branch() -> (
    None
):
    toolkit = Toolkit(
        name="test",
        tools=[_FloatIntUnionTool(), _BoolIntUnionTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    float_string_result = await toolkit.invoke(
        context=context,
        name="float_int_union",
        input=JsonContent(json={"value": "2"}),
    )

    assert isinstance(float_string_result, JsonContent)
    assert float_string_result.json == {"type": "float", "value": 2.0}

    float_bool_result = await toolkit.invoke(
        context=context,
        name="float_int_union",
        input=JsonContent(json={"value": True}),
    )

    assert isinstance(float_bool_result, JsonContent)
    assert float_bool_result.json == {"type": "float", "value": 1.0}

    bool_string_result = await toolkit.invoke(
        context=context,
        name="bool_int_union",
        input=JsonContent(json={"value": "1"}),
    )

    assert isinstance(bool_string_result, JsonContent)
    assert bool_string_result.json == {"type": "bool", "value": True}

    bool_int_result = await toolkit.invoke(
        context=context,
        name="bool_int_union",
        input=JsonContent(json={"value": 1}),
    )

    assert isinstance(bool_int_result, JsonContent)
    assert bool_int_result.json == {"type": "int", "value": 1}


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_applies_union_rules_in_arrays() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_UnionListTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="union_list",
        input=JsonContent(json={"values": ["2", 2]}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"types": ["str", "int"], "values": ["2", 2]}


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_coerces_dict_values() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_DictTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="dict_values",
        input=JsonContent(
            json={
                "counts": {"a": "2", "b": "3.0"},
                "values": {"x": "4", "y": 5},
            }
        ),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "count_types": {"a": "int", "b": "int"},
        "counts": {"a": 2, "b": 3},
        "value_types": {"x": "str", "y": "int"},
        "values": {"x": "4", "y": 5},
    }


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_coerces_pattern_dict_values() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_PatternDictTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="pattern_dict_values",
        input=JsonContent(json={"values": {"x_a": "2", "x_b": "3.0"}}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "types": {"x_a": "int", "x_b": "int"},
        "values": {"x_a": 2, "x_b": 3},
    }


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_coerces_tuple_values() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_TupleTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="tuple_values",
        input=JsonContent(json={"pair": ["2", "yes"], "variadic": ["1", "2.0"]}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "pair_types": ["int", "bool"],
        "pair": [2, True],
        "variadic_types": ["int", "int"],
        "variadic": [1, 2],
    }


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_coerces_constrained_numbers() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_ConstrainedTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="constrained_values",
        input=JsonContent(json={"count": "2.0", "ratio": True}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "count": 2,
        "count_type": "int",
        "ratio": 1.0,
        "ratio_type": "float",
    }

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="constrained_values",
            input=JsonContent(json={"count": "10", "ratio": "1.5"}),
        )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_preserves_literal_exactness() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_LiteralTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="literal_values",
        input=JsonContent(
            json={
                "int_value": 2,
                "bool_value": True,
                "str_value": "2",
                "choice": "1",
            }
        ),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "int_type": "int",
        "int_value": 2,
        "bool_type": "bool",
        "bool_value": True,
        "str_type": "str",
        "str_value": "2",
        "choice_type": "str",
        "choice": "1",
    }

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="literal_values",
            input=JsonContent(
                json={
                    "int_value": "2",
                    "bool_value": "true",
                    "str_value": "2",
                    "choice": "1",
                }
            ),
        )

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="literal_values",
            input=JsonContent(
                json={
                    "int_value": 3,
                    "bool_value": True,
                    "str_value": "2",
                    "choice": "1",
                }
            ),
        )


@pytest.mark.asyncio
async def test_pydantic_tool_none_mode_still_runs_model_validation() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_ConstrainedTool(), _LiteralTool()],
        validation_mode="none",
    )
    context = ToolContext(caller=object())

    constrained_result = await toolkit.invoke(
        context=context,
        name="constrained_values",
        input=JsonContent(json={"count": "2.0", "ratio": True}),
    )

    assert isinstance(constrained_result, JsonContent)
    assert constrained_result.json == {
        "count": 2,
        "count_type": "int",
        "ratio": 1.0,
        "ratio_type": "float",
    }

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="constrained_values",
            input=JsonContent(json={"count": "10", "ratio": "1.5"}),
        )

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="literal_values",
            input=JsonContent(
                json={
                    "int_value": "2",
                    "bool_value": "true",
                    "str_value": "2",
                    "choice": "1",
                }
            ),
        )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_coerces_nested_ref_model() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_NestedPydanticTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="nested_pydantic",
        input=JsonContent(json={"child": {"count": "2.0", "flag": "yes"}}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "count": 2,
        "count_type": "int",
        "flag": True,
        "flag_type": "bool",
    }

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="nested_pydantic",
            input=JsonContent(json={"child": {"count": "10", "flag": "yes"}}),
        )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_coerces_nested_ref_model_list() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_NestedPydanticListTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="nested_pydantic_list",
        input=JsonContent(
            json={
                "items": [
                    {"count": "2.0", "flag": "yes"},
                    {"count": 3, "flag": False},
                ]
            }
        ),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "items": [
            {"count": 2, "count_type": "int", "flag": True, "flag_type": "bool"},
            {"count": 3, "count_type": "int", "flag": False, "flag_type": "bool"},
        ]
    }

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="nested_pydantic_list",
            input=JsonContent(json={"items": [{"count": "10", "flag": "yes"}]}),
        )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_applies_model_defaults() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_DefaultTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    empty_result = await toolkit.invoke(
        context=context,
        name="default_values",
        input=JsonContent(json={}),
    )

    assert isinstance(empty_result, JsonContent)
    assert empty_result.json == {
        "count": 3,
        "count_type": "int",
        "enabled": True,
        "enabled_type": "bool",
        "label": "alpha",
        "label_type": "str",
    }

    partial_result = await toolkit.invoke(
        context=context,
        name="default_values",
        input=JsonContent(json={"count": "4"}),
    )

    assert isinstance(partial_result, JsonContent)
    assert partial_result.json == {
        "count": 4,
        "count_type": "int",
        "enabled": True,
        "enabled_type": "bool",
        "label": "alpha",
        "label_type": "str",
    }


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_applies_nested_model_defaults() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_NestedDefaultTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="nested_default_values",
        input=JsonContent(json={"child": {}}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "count": 3,
        "count_type": "int",
        "flag": True,
        "flag_type": "bool",
    }

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="nested_default_values",
            input=JsonContent(json={}),
        )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_applies_nested_model_list_defaults() -> (
    None
):
    toolkit = Toolkit(
        name="test",
        tools=[_NestedDefaultListTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="nested_default_list_values",
        input=JsonContent(json={"items": [{}]}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "items": [{"count": 3, "count_type": "int", "flag": True, "flag_type": "bool"}]
    }

    empty_list_result = await toolkit.invoke(
        context=context,
        name="nested_default_list_values",
        input=JsonContent(json={"items": []}),
    )

    assert isinstance(empty_list_result, JsonContent)
    assert empty_list_result.json == {"items": []}

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="nested_default_list_values",
            input=JsonContent(json={}),
        )


@pytest.mark.asyncio
async def test_toolkit_execute_uses_descriptive_span_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_tracer = _RecordedTracer()
    monkeypatch.setattr(toolkit_module, "tracer", recorded_tracer)
    toolkit = Toolkit(name="math-toolkit", tools=[_AddTool()])
    context = ToolContext(caller=object())

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
async def test_toolkit_execute_can_suppress_tool_call_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_tracer = _RecordedTracer()
    monkeypatch.setattr(toolkit_module, "tracer", recorded_tracer)
    toolkit = Toolkit(
        name="math-toolkit",
        tools=[_AddTool()],
        trace_tool_calls=False,
    )
    context = ToolContext(caller=object())

    result = await toolkit.execute(
        context=context,
        name="add",
        input=JsonContent(json={"a": 1, "b": 2}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"c": 3}
    assert recorded_tracer.spans == []


@pytest.mark.asyncio
async def test_toolkit_execute_respects_explicit_validation_mode() -> None:
    toolkit = Toolkit(name="test", tools=[_AddTool()])
    context = ToolContext(caller=object())

    result = await toolkit.execute(
        context=context,
        name="add",
        input=JsonContent(json={"a": "1", "b": 2, "ignored": True}),
        validation_mode="content_types",
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"c": 3}


@pytest.mark.asyncio
async def test_toolkit_full_validation_mode_rejects_optional_nested_pydantic_fields():
    toolkit = Toolkit(name="test", tools=[count_mounts])
    context = ToolContext(caller=object())

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
