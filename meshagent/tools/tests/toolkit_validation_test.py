import pytest
from jsonschema import ValidationError as JsonSchemaValidationError
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)

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


class _IntBoolUnionPayload(BaseModel):
    value: int | bool


class _BoolStrUnionPayload(BaseModel):
    value: bool | str


class _UnionListPayload(BaseModel):
    values: list[int | str]


class _ConstrainedListPayload(BaseModel):
    values: Annotated[list[int], Field(min_length=2, max_length=3)]


class _SetPayload(BaseModel):
    values: set[int]


class _FrozenSetPayload(BaseModel):
    values: frozenset[int]


class _DictPayload(BaseModel):
    counts: dict[str, int]
    values: dict[str, int | str]


class _ConstrainedDictPayload(BaseModel):
    values: Annotated[dict[str, int], Field(min_length=1, max_length=2)]


class _PatternDictPayload(BaseModel):
    values: dict[Annotated[str, Field(pattern="^x_")], int]


class _TuplePayload(BaseModel):
    pair: tuple[int, bool]
    variadic: tuple[int, ...]


class _AliasPayload(BaseModel):
    count_value: int = Field(alias="countValue")


class _ValidationAliasPayload(BaseModel):
    count_value: int = Field(
        validation_alias="inputCount", serialization_alias="outputCount"
    )


class _AliasChoicesPayload(BaseModel):
    count_value: int = Field(
        validation_alias=AliasChoices("primaryCount", "legacyCount")
    )


class _AliasPathPayload(BaseModel):
    count_value: int = Field(validation_alias=AliasPath("payload", "count"))


class _AliasPathIndexPayload(BaseModel):
    count_value: int = Field(validation_alias=AliasPath("items", 0, "count"))


class _PopulateByNamePayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    count_value: int = Field(alias="countValue")


class _PopulateByNameForbidPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    count_value: int = Field(alias="countValue")


class _PopulateByNameAllowPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    count_value: int = Field(alias="countValue")


class _ConstrainedPayload(BaseModel):
    count: Annotated[int, Field(ge=1, le=5)]
    ratio: Annotated[float, Field(gt=0, lt=2)]


class _MultipleOfPayload(BaseModel):
    count: Annotated[int, Field(multiple_of=3)]
    ratio: Annotated[float, Field(multiple_of=0.5)]


class _LiteralPayload(BaseModel):
    int_value: Literal[2]
    bool_value: Literal[True]
    str_value: Literal["2"]
    choice: Literal[1, "1"]


class _Color(Enum):
    RED = "red"
    BLUE = "blue"


class _EnumPayload(BaseModel):
    color: _Color


class _EnumValuesPayload(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    color: _Color


class _CatPayload(BaseModel):
    pet_type: Literal["cat"]
    lives: int


class _DogPayload(BaseModel):
    pet_type: Literal["dog"]
    bark: bool


class _DiscriminatedUnionPayload(BaseModel):
    pet: Annotated[_CatPayload | _DogPayload, Field(discriminator="pet_type")]


class _NestedChildPayload(BaseModel):
    count: Annotated[int, Field(ge=1, le=5)]
    flag: bool


class _NestedPydanticPayload(BaseModel):
    child: _NestedChildPayload


class _NestedAllowExtraChildPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    count: int


class _NestedAllowExtraPayload(BaseModel):
    child: _NestedAllowExtraChildPayload


class _RootListPayload(RootModel[list[int]]):
    pass


class _NestedPydanticListPayload(BaseModel):
    items: list[_NestedChildPayload]


class _DefaultPayload(BaseModel):
    count: int = 3
    enabled: bool = True
    label: str = "alpha"
    made: list[int] = Field(default_factory=lambda: [1, 2])


class _UnvalidatedDefaultPayload(BaseModel):
    count: int = "3"


class _ValidateDefaultPayload(BaseModel):
    count: int = Field("3", validate_default=True)


class _NestedDefaultChildPayload(BaseModel):
    count: int = 3
    flag: bool = True


class _NestedDefaultPayload(BaseModel):
    child: _NestedDefaultChildPayload


class _NestedDefaultListPayload(BaseModel):
    items: list[_NestedDefaultChildPayload]


class _ForbidExtraPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int


class _AllowExtraPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    count: int


class _StrictPayload(BaseModel):
    model_config = ConfigDict(strict=True)

    count: int
    enabled: bool
    ratio: float


class _StrictFieldPayload(BaseModel):
    strict_count: Annotated[int, Field(strict=True)]
    loose_count: int
    strict_enabled: Annotated[bool, Field(strict=True)]
    loose_enabled: bool
    strict_ratio: Annotated[float, Field(strict=True)]
    loose_ratio: float


class _NestedStrictFieldChildPayload(BaseModel):
    strict_count: Annotated[int, Field(strict=True)]
    loose_count: int


class _NestedStrictFieldPayload(BaseModel):
    child: _NestedStrictFieldChildPayload


class _FormattedPayload(BaseModel):
    day: date
    moment: datetime
    clock: time
    ident: UUID


class _TimeEdgePayload(BaseModel):
    clock: time


class _NaiveDatetimePayload(BaseModel):
    moment: datetime


class _DecimalPayload(BaseModel):
    amount: Decimal


class _StringPayload(BaseModel):
    value: str


class _CoerceNumberStringPayload(BaseModel):
    value: Annotated[str, Field(coerce_numbers_to_str=True)]


class _StripStringPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    value: str
    code: Annotated[str, Field(min_length=2)]


class _LowerStringPayload(BaseModel):
    model_config = ConfigDict(str_to_lower=True)

    value: str
    code: Annotated[str, Field(pattern="^abc$")]


class _UpperStringPayload(BaseModel):
    model_config = ConfigDict(str_to_upper=True)

    value: str
    code: Annotated[str, Field(pattern="^ABC$")]


class _ConstrainedStringPayload(BaseModel):
    code: Annotated[str, Field(min_length=2, max_length=4, pattern="^x[0-9]+$")]


class _CustomValidatorPayload(BaseModel):
    code: str

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        if not value.startswith("x-"):
            raise ValueError("code must start with x-")
        return value.removeprefix("x-").upper()

    @field_validator("code")
    @classmethod
    def append_suffix(cls, value: str) -> str:
        return f"{value}-OK"


class _BeforeCustomValidatorPayload(BaseModel):
    code: str

    @model_validator(mode="before")
    @classmethod
    def map_legacy_code(cls, value: object) -> object:
        if isinstance(value, dict) and "legacy_code" in value:
            return {"code": value["legacy_code"]}
        return value

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.upper()


class _FieldBeforeCustomValidatorPayload(BaseModel):
    code: str

    @field_validator("code", mode="before")
    @classmethod
    def coerce_int_code(cls, value: object) -> object:
        if isinstance(value, int):
            return f"x-{value}"
        return value

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.upper()


class _FieldValidatorDefaultPayload(BaseModel):
    skipped: str = "x-skip"
    validated: str = Field("x-run", validate_default=True)

    @field_validator("skipped", "validated", mode="before")
    @classmethod
    def uppercase_default(cls, value: object) -> object:
        if isinstance(value, str):
            return value.upper()
        return value

    @field_validator("skipped", "validated")
    @classmethod
    def append_after(cls, value: str) -> str:
        return f"{value}-AFTER"


class _PlainFieldValidatorPayload(BaseModel):
    code: str

    @field_validator("code", mode="plain")
    @classmethod
    def extract_code(cls, value: object) -> object:
        if isinstance(value, dict):
            return f"x-{value['code']}"
        return value

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.upper()


class _AfterCustomValidatorPayload(BaseModel):
    left: int
    right: int
    total: int = 0

    @field_validator("left")
    @classmethod
    def increment_left(cls, value: int) -> int:
        return value + 1

    @model_validator(mode="after")
    def fill_total(self):
        self.total = self.left + self.right
        if self.total < 10:
            raise ValueError("total must be at least 10")
        return self


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


class _IntBoolUnionTool(PydanticTool[_IntBoolUnionPayload]):
    def __init__(self) -> None:
        super().__init__(name="int_bool_union", input_model=_IntBoolUnionPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _IntBoolUnionPayload
    ):
        del context
        return {"type": type(arguments.value).__name__, "value": arguments.value}


class _BoolStrUnionTool(PydanticTool[_BoolStrUnionPayload]):
    def __init__(self) -> None:
        super().__init__(name="bool_str_union", input_model=_BoolStrUnionPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _BoolStrUnionPayload
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


class _ConstrainedListTool(PydanticTool[_ConstrainedListPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="constrained_list_values", input_model=_ConstrainedListPayload
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _ConstrainedListPayload
    ):
        del context
        return {
            "types": [type(value).__name__ for value in arguments.values],
            "values": arguments.values,
        }


class _SetTool(PydanticTool[_SetPayload]):
    def __init__(self) -> None:
        super().__init__(name="set_values", input_model=_SetPayload)

    async def execute_model(self, *, context: ToolContext, arguments: _SetPayload):
        del context
        values = sorted(arguments.values)
        return {
            "types": [type(value).__name__ for value in values],
            "values": values,
        }


class _FrozenSetTool(PydanticTool[_FrozenSetPayload]):
    def __init__(self) -> None:
        super().__init__(name="frozenset_values", input_model=_FrozenSetPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _FrozenSetPayload
    ):
        del context
        values = sorted(arguments.values)
        return {
            "type": type(arguments.values).__name__,
            "types": [type(value).__name__ for value in values],
            "values": values,
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


class _ConstrainedDictTool(PydanticTool[_ConstrainedDictPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="constrained_dict_values", input_model=_ConstrainedDictPayload
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _ConstrainedDictPayload
    ):
        del context
        return {
            "types": {
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


class _AliasTool(PydanticTool[_AliasPayload]):
    def __init__(self) -> None:
        super().__init__(name="alias_values", input_model=_AliasPayload)

    async def execute_model(self, *, context: ToolContext, arguments: _AliasPayload):
        del context
        return {
            "count_value": arguments.count_value,
            "count_type": type(arguments.count_value).__name__,
        }


class _ValidationAliasTool(PydanticTool[_ValidationAliasPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="validation_alias_values", input_model=_ValidationAliasPayload
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _ValidationAliasPayload
    ):
        del context
        return {
            "count_value": arguments.count_value,
            "count_type": type(arguments.count_value).__name__,
        }


class _AliasChoicesTool(PydanticTool[_AliasChoicesPayload]):
    def __init__(self) -> None:
        super().__init__(name="alias_choices_values", input_model=_AliasChoicesPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _AliasChoicesPayload
    ):
        del context
        return {
            "count_value": arguments.count_value,
            "count_type": type(arguments.count_value).__name__,
        }


class _AliasPathTool(PydanticTool[_AliasPathPayload]):
    def __init__(self) -> None:
        super().__init__(name="alias_path_values", input_model=_AliasPathPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _AliasPathPayload
    ):
        del context
        return {
            "count_value": arguments.count_value,
            "count_type": type(arguments.count_value).__name__,
        }


class _AliasPathIndexTool(PydanticTool[_AliasPathIndexPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="alias_path_index_values", input_model=_AliasPathIndexPayload
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _AliasPathIndexPayload
    ):
        del context
        return {
            "count_value": arguments.count_value,
            "count_type": type(arguments.count_value).__name__,
        }


class _PopulateByNameTool(PydanticTool[_PopulateByNamePayload]):
    def __init__(self) -> None:
        super().__init__(
            name="populate_by_name_values", input_model=_PopulateByNamePayload
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _PopulateByNamePayload
    ):
        del context
        return {
            "count_value": arguments.count_value,
            "count_type": type(arguments.count_value).__name__,
        }


class _PopulateByNameForbidTool(PydanticTool[_PopulateByNameForbidPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="populate_by_name_forbid_values",
            input_model=_PopulateByNameForbidPayload,
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _PopulateByNameForbidPayload
    ):
        del context
        return {
            "count_value": arguments.count_value,
            "count_type": type(arguments.count_value).__name__,
        }


class _PopulateByNameAllowTool(PydanticTool[_PopulateByNameAllowPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="populate_by_name_allow_values",
            input_model=_PopulateByNameAllowPayload,
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _PopulateByNameAllowPayload
    ):
        del context
        return {
            "count_value": arguments.count_value,
            "count_type": type(arguments.count_value).__name__,
            "extras": arguments.model_extra,
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


class _MultipleOfTool(PydanticTool[_MultipleOfPayload]):
    def __init__(self) -> None:
        super().__init__(name="multiple_of_values", input_model=_MultipleOfPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _MultipleOfPayload
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


class _EnumTool(PydanticTool[_EnumPayload]):
    def __init__(self) -> None:
        super().__init__(name="enum_values", input_model=_EnumPayload)

    async def execute_model(self, *, context: ToolContext, arguments: _EnumPayload):
        del context
        return {
            "color_type": type(arguments.color).__name__,
            "color_name": arguments.color.name,
            "color_value": arguments.color.value,
        }


class _EnumValuesTool(PydanticTool[_EnumValuesPayload]):
    def __init__(self) -> None:
        super().__init__(name="enum_raw_values", input_model=_EnumValuesPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _EnumValuesPayload
    ):
        del context
        return {
            "color_type": type(arguments.color).__name__,
            "color": arguments.color,
        }


class _DiscriminatedUnionTool(PydanticTool[_DiscriminatedUnionPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="discriminated_union_values",
            input_model=_DiscriminatedUnionPayload,
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _DiscriminatedUnionPayload
    ):
        del context
        pet = arguments.pet
        if isinstance(pet, _CatPayload):
            return {
                "pet_type": type(pet).__name__,
                "tag": pet.pet_type,
                "lives": pet.lives,
                "lives_type": type(pet.lives).__name__,
            }
        return {
            "pet_type": type(pet).__name__,
            "tag": pet.pet_type,
            "bark": pet.bark,
            "bark_type": type(pet.bark).__name__,
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


class _NestedAllowExtraTool(PydanticTool[_NestedAllowExtraPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="nested_allow_extra", input_model=_NestedAllowExtraPayload
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _NestedAllowExtraPayload
    ):
        del context
        return {
            "count": arguments.child.count,
            "count_type": type(arguments.child.count).__name__,
            "extras": arguments.child.model_extra,
        }


class _RootListTool(PydanticTool[_RootListPayload]):
    def __init__(self) -> None:
        super().__init__(name="root_list_values", input_model=_RootListPayload)

    async def execute_model(self, *, context: ToolContext, arguments: _RootListPayload):
        del context
        return {"values": arguments.root}


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
            "made": arguments.made,
            "made_types": [type(item).__name__ for item in arguments.made],
        }


class _UnvalidatedDefaultTool(PydanticTool[_UnvalidatedDefaultPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="unvalidated_default_values",
            input_model=_UnvalidatedDefaultPayload,
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _UnvalidatedDefaultPayload
    ):
        del context
        count: Any = arguments.count
        return {
            "count": count,
            "count_type": type(count).__name__,
        }


class _ValidateDefaultTool(PydanticTool[_ValidateDefaultPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="validate_default_values",
            input_model=_ValidateDefaultPayload,
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _ValidateDefaultPayload
    ):
        del context
        return {
            "count": arguments.count,
            "count_type": type(arguments.count).__name__,
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


class _ForbidExtraTool(PydanticTool[_ForbidExtraPayload]):
    def __init__(self) -> None:
        super().__init__(name="forbid_extra", input_model=_ForbidExtraPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _ForbidExtraPayload
    ):
        del context
        return {
            "count": arguments.count,
            "count_type": type(arguments.count).__name__,
        }


class _AllowExtraTool(PydanticTool[_AllowExtraPayload]):
    def __init__(self) -> None:
        super().__init__(name="allow_extra", input_model=_AllowExtraPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _AllowExtraPayload
    ):
        del context
        return {
            "count": arguments.count,
            "count_type": type(arguments.count).__name__,
            "extras": arguments.model_extra,
        }


class _StrictTool(PydanticTool[_StrictPayload]):
    def __init__(self) -> None:
        super().__init__(name="strict_values", input_model=_StrictPayload)

    async def execute_model(self, *, context: ToolContext, arguments: _StrictPayload):
        del context
        return {
            "count": arguments.count,
            "count_type": type(arguments.count).__name__,
            "enabled": arguments.enabled,
            "enabled_type": type(arguments.enabled).__name__,
            "ratio": arguments.ratio,
            "ratio_type": type(arguments.ratio).__name__,
        }


class _StrictFieldTool(PydanticTool[_StrictFieldPayload]):
    def __init__(self) -> None:
        super().__init__(name="strict_field_values", input_model=_StrictFieldPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _StrictFieldPayload
    ):
        del context
        return {
            "strict_count": arguments.strict_count,
            "strict_count_type": type(arguments.strict_count).__name__,
            "loose_count": arguments.loose_count,
            "loose_count_type": type(arguments.loose_count).__name__,
            "strict_enabled": arguments.strict_enabled,
            "strict_enabled_type": type(arguments.strict_enabled).__name__,
            "loose_enabled": arguments.loose_enabled,
            "loose_enabled_type": type(arguments.loose_enabled).__name__,
            "strict_ratio": arguments.strict_ratio,
            "strict_ratio_type": type(arguments.strict_ratio).__name__,
            "loose_ratio": arguments.loose_ratio,
            "loose_ratio_type": type(arguments.loose_ratio).__name__,
        }


class _NestedStrictFieldTool(PydanticTool[_NestedStrictFieldPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="nested_strict_field_values",
            input_model=_NestedStrictFieldPayload,
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _NestedStrictFieldPayload
    ):
        del context
        return {
            "strict_count": arguments.child.strict_count,
            "strict_count_type": type(arguments.child.strict_count).__name__,
            "loose_count": arguments.child.loose_count,
            "loose_count_type": type(arguments.child.loose_count).__name__,
        }


class _FormattedTool(PydanticTool[_FormattedPayload]):
    def __init__(self) -> None:
        super().__init__(name="formatted_values", input_model=_FormattedPayload)

    async def execute_model(
        self, *, context: ToolContext, arguments: _FormattedPayload
    ):
        del context
        return {
            "day_type": type(arguments.day).__name__,
            "day": arguments.day.isoformat(),
            "moment_type": type(arguments.moment).__name__,
            "moment": arguments.moment.isoformat(),
            "clock_type": type(arguments.clock).__name__,
            "clock": arguments.clock.isoformat(),
            "ident_type": type(arguments.ident).__name__,
            "ident": str(arguments.ident),
        }


class _TimeEdgeTool(PydanticTool[_TimeEdgePayload]):
    def __init__(self) -> None:
        super().__init__(name="time_edge_values", input_model=_TimeEdgePayload)

    async def execute_model(self, *, context: ToolContext, arguments: _TimeEdgePayload):
        del context
        return {
            "clock_type": type(arguments.clock).__name__,
            "clock": arguments.clock.isoformat(),
        }


class _NaiveDatetimeTool(PydanticTool[_NaiveDatetimePayload]):
    def __init__(self) -> None:
        super().__init__(
            name="naive_datetime_values", input_model=_NaiveDatetimePayload
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _NaiveDatetimePayload
    ):
        del context
        return {
            "moment_type": type(arguments.moment).__name__,
            "moment": arguments.moment.isoformat(),
            "tzinfo": None
            if arguments.moment.tzinfo is None
            else str(arguments.moment.tzinfo),
        }


class _DecimalTool(PydanticTool[_DecimalPayload]):
    def __init__(self) -> None:
        super().__init__(name="decimal_values", input_model=_DecimalPayload)

    async def execute_model(self, *, context: ToolContext, arguments: _DecimalPayload):
        del context
        return {
            "amount_type": type(arguments.amount).__name__,
            "amount": str(arguments.amount),
        }


class _StringTool(PydanticTool[_StringPayload]):
    def __init__(self) -> None:
        super().__init__(name="string_values", input_model=_StringPayload)

    async def execute_model(self, *, context: ToolContext, arguments: _StringPayload):
        del context
        return {
            "value_type": type(arguments.value).__name__,
            "value": arguments.value,
        }


class _CoerceNumberStringTool(PydanticTool[_CoerceNumberStringPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="coerce_number_string_values",
            input_model=_CoerceNumberStringPayload,
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _CoerceNumberStringPayload
    ):
        del context
        return {
            "value_type": type(arguments.value).__name__,
            "value": arguments.value,
        }


class _StripStringTool(PydanticTool[_StripStringPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="strip_string_values",
            input_model=_StripStringPayload,
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _StripStringPayload
    ):
        del context
        return {
            "value_type": type(arguments.value).__name__,
            "value": arguments.value,
            "code_type": type(arguments.code).__name__,
            "code": arguments.code,
        }


class _LowerStringTool(PydanticTool[_LowerStringPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="lower_string_values",
            input_model=_LowerStringPayload,
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _LowerStringPayload
    ):
        del context
        return {
            "value_type": type(arguments.value).__name__,
            "value": arguments.value,
            "code_type": type(arguments.code).__name__,
            "code": arguments.code,
        }


class _UpperStringTool(PydanticTool[_UpperStringPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="upper_string_values",
            input_model=_UpperStringPayload,
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _UpperStringPayload
    ):
        del context
        return {
            "value_type": type(arguments.value).__name__,
            "value": arguments.value,
            "code_type": type(arguments.code).__name__,
            "code": arguments.code,
        }


class _ConstrainedStringTool(PydanticTool[_ConstrainedStringPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="constrained_string_values", input_model=_ConstrainedStringPayload
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _ConstrainedStringPayload
    ):
        del context
        return {
            "code_type": type(arguments.code).__name__,
            "code": arguments.code,
        }


class _CustomValidatorTool(PydanticTool[_CustomValidatorPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="custom_validator_values", input_model=_CustomValidatorPayload
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _CustomValidatorPayload
    ):
        del context
        return {
            "code_type": type(arguments.code).__name__,
            "code": arguments.code,
        }


class _BeforeCustomValidatorTool(PydanticTool[_BeforeCustomValidatorPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="before_custom_validator_values",
            input_model=_BeforeCustomValidatorPayload,
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _BeforeCustomValidatorPayload
    ):
        del context
        return {
            "code_type": type(arguments.code).__name__,
            "code": arguments.code,
        }


class _FieldBeforeCustomValidatorTool(PydanticTool[_FieldBeforeCustomValidatorPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="field_before_custom_validator_values",
            input_model=_FieldBeforeCustomValidatorPayload,
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _FieldBeforeCustomValidatorPayload
    ):
        del context
        return {
            "code_type": type(arguments.code).__name__,
            "code": arguments.code,
        }


class _FieldValidatorDefaultTool(PydanticTool[_FieldValidatorDefaultPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="field_validator_default_values",
            input_model=_FieldValidatorDefaultPayload,
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _FieldValidatorDefaultPayload
    ):
        del context
        return {
            "skipped": arguments.skipped,
            "validated": arguments.validated,
        }


class _PlainFieldValidatorTool(PydanticTool[_PlainFieldValidatorPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="plain_field_validator_values",
            input_model=_PlainFieldValidatorPayload,
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _PlainFieldValidatorPayload
    ):
        del context
        return {
            "code_type": type(arguments.code).__name__,
            "code": arguments.code,
        }


class _AfterCustomValidatorTool(PydanticTool[_AfterCustomValidatorPayload]):
    def __init__(self) -> None:
        super().__init__(
            name="after_custom_validator_values",
            input_model=_AfterCustomValidatorPayload,
        )

    async def execute_model(
        self, *, context: ToolContext, arguments: _AfterCustomValidatorPayload
    ):
        del context
        return {
            "left": arguments.left,
            "right": arguments.right,
            "total": arguments.total,
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
async def test_pydantic_tool_content_types_mode_preserves_union_exact_bool_and_string() -> (
    None
):
    toolkit = Toolkit(
        name="test",
        tools=[_IntBoolUnionTool(), _BoolStrUnionTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    exact_bool_result = await toolkit.invoke(
        context=context,
        name="int_bool_union",
        input=JsonContent(json={"value": True}),
    )

    assert isinstance(exact_bool_result, JsonContent)
    assert exact_bool_result.json == {"type": "bool", "value": True}

    int_bool_string_result = await toolkit.invoke(
        context=context,
        name="int_bool_union",
        input=JsonContent(json={"value": "1"}),
    )

    assert isinstance(int_bool_string_result, JsonContent)
    assert int_bool_string_result.json == {"type": "int", "value": 1}

    exact_string_result = await toolkit.invoke(
        context=context,
        name="bool_str_union",
        input=JsonContent(json={"value": "true"}),
    )

    assert isinstance(exact_string_result, JsonContent)
    assert exact_string_result.json == {"type": "str", "value": "true"}

    bool_str_number_result = await toolkit.invoke(
        context=context,
        name="bool_str_union",
        input=JsonContent(json={"value": 1}),
    )

    assert isinstance(bool_str_number_result, JsonContent)
    assert bool_str_number_result.json == {"type": "bool", "value": True}


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
async def test_pydantic_tool_content_types_mode_validates_list_constraints() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_ConstrainedListTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    for values, expected in [
        (["1", 2], [1, 2]),
        (["1", "2", "3"], [1, 2, 3]),
    ]:
        result = await toolkit.invoke(
            context=context,
            name="constrained_list_values",
            input=JsonContent(json={"values": values}),
        )

        assert isinstance(result, JsonContent)
        assert result.json == {
            "types": ["int" for _ in expected],
            "values": expected,
        }

    for bad_values in ([1], [1, 2, 3, 4], ["x", 2], "12"):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="constrained_list_values",
                input=JsonContent(json={"values": bad_values}),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_dedupes_sets_after_item_coercion() -> (
    None
):
    toolkit = Toolkit(
        name="test",
        tools=[_SetTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="set_values",
        input=JsonContent(json={"values": ["1", 1, 2]}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"types": ["int", "int"], "values": [1, 2]}

    empty_result = await toolkit.invoke(
        context=context,
        name="set_values",
        input=JsonContent(json={"values": []}),
    )

    assert isinstance(empty_result, JsonContent)
    assert empty_result.json == {"types": [], "values": []}

    for bad_values in (["x"], "12"):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="set_values",
                input=JsonContent(json={"values": bad_values}),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_dedupes_frozensets_after_item_coercion() -> (
    None
):
    toolkit = Toolkit(
        name="test",
        tools=[_FrozenSetTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="frozenset_values",
        input=JsonContent(json={"values": ["1", 1, 2]}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "type": "frozenset",
        "types": ["int", "int"],
        "values": [1, 2],
    }

    empty_result = await toolkit.invoke(
        context=context,
        name="frozenset_values",
        input=JsonContent(json={"values": []}),
    )

    assert isinstance(empty_result, JsonContent)
    assert empty_result.json == {"type": "frozenset", "types": [], "values": []}

    for bad_values in (["x"], "12"):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="frozenset_values",
                input=JsonContent(json={"values": bad_values}),
            )


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
async def test_pydantic_tool_content_types_mode_validates_dict_constraints() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_ConstrainedDictTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    for values, expected in [
        ({"a": "1"}, {"a": 1}),
        ({"a": "1", "b": 2}, {"a": 1, "b": 2}),
    ]:
        result = await toolkit.invoke(
            context=context,
            name="constrained_dict_values",
            input=JsonContent(json={"values": values}),
        )

        assert isinstance(result, JsonContent)
        assert result.json == {
            "types": {key: "int" for key in expected},
            "values": expected,
        }

    for bad_values in ({}, {"a": 1, "b": 2, "c": 3}, {"a": "x"}, [["a", 1]]):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="constrained_dict_values",
                input=JsonContent(json={"values": bad_values}),
            )


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

    invalid_inputs = [
        {"pair": ["2"], "variadic": []},
        {"pair": ["2", "yes", 3], "variadic": []},
        {"pair": ["x", "yes"], "variadic": []},
        {"pair": "12", "variadic": []},
    ]
    for input_json in invalid_inputs:
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="tuple_values",
                input=JsonContent(json=input_json),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_uses_alias_field_names() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_AliasTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="alias_values",
        input=JsonContent(json={"countValue": "3"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"count_value": 3, "count_type": "int"}

    both_result = await toolkit.invoke(
        context=context,
        name="alias_values",
        input=JsonContent(json={"countValue": "3", "count_value": "4"}),
    )

    assert isinstance(both_result, JsonContent)
    assert both_result.json == {"count_value": 3, "count_type": "int"}

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="alias_values",
            input=JsonContent(json={"count_value": "4"}),
        )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_uses_validation_aliases() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_ValidationAliasTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="validation_alias_values",
        input=JsonContent(json={"inputCount": "3"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"count_value": 3, "count_type": "int"}

    both_result = await toolkit.invoke(
        context=context,
        name="validation_alias_values",
        input=JsonContent(json={"inputCount": "3", "count_value": "4"}),
    )

    assert isinstance(both_result, JsonContent)
    assert both_result.json == {"count_value": 3, "count_type": "int"}

    for input_json in ({"count_value": "4"}, {"outputCount": "4"}):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="validation_alias_values",
                input=JsonContent(json=input_json),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_uses_alias_choices() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_AliasChoicesTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    primary_result = await toolkit.invoke(
        context=context,
        name="alias_choices_values",
        input=JsonContent(json={"primaryCount": "3"}),
    )
    assert isinstance(primary_result, JsonContent)
    assert primary_result.json == {"count_value": 3, "count_type": "int"}

    legacy_result = await toolkit.invoke(
        context=context,
        name="alias_choices_values",
        input=JsonContent(json={"legacyCount": "4"}),
    )
    assert isinstance(legacy_result, JsonContent)
    assert legacy_result.json == {"count_value": 4, "count_type": "int"}

    both_result = await toolkit.invoke(
        context=context,
        name="alias_choices_values",
        input=JsonContent(json={"primaryCount": "3", "legacyCount": "4"}),
    )
    assert isinstance(both_result, JsonContent)
    assert both_result.json == {"count_value": 3, "count_type": "int"}

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="alias_choices_values",
            input=JsonContent(json={"count_value": "5"}),
        )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_uses_alias_paths() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_AliasPathTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="alias_path_values",
        input=JsonContent(json={"payload": {"count": "3"}}),
    )
    assert isinstance(result, JsonContent)
    assert result.json == {"count_value": 3, "count_type": "int"}

    path_wins_result = await toolkit.invoke(
        context=context,
        name="alias_path_values",
        input=JsonContent(json={"payload": {"count": "3"}, "count_value": "4"}),
    )
    assert isinstance(path_wins_result, JsonContent)
    assert path_wins_result.json == {"count_value": 3, "count_type": "int"}

    for input_json in (
        {"count_value": "4"},
        {"payload": {}},
        {"payload": {"count": "bad"}},
    ):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="alias_path_values",
                input=JsonContent(json=input_json),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_uses_alias_path_indexes() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_AliasPathIndexTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="alias_path_index_values",
        input=JsonContent(json={"items": [{"count": "3"}]}),
    )
    assert isinstance(result, JsonContent)
    assert result.json == {"count_value": 3, "count_type": "int"}

    path_wins_result = await toolkit.invoke(
        context=context,
        name="alias_path_index_values",
        input=JsonContent(json={"items": [{"count": "3"}], "count_value": "4"}),
    )
    assert isinstance(path_wins_result, JsonContent)
    assert path_wins_result.json == {"count_value": 3, "count_type": "int"}

    for input_json in (
        {"count_value": "4"},
        {"items": []},
        {"items": [{"count": "bad"}]},
    ):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="alias_path_index_values",
                input=JsonContent(json=input_json),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_populates_aliases_by_name() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[
            _PopulateByNameTool(),
            _PopulateByNameForbidTool(),
            _PopulateByNameAllowTool(),
        ],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    field_name_result = await toolkit.invoke(
        context=context,
        name="populate_by_name_values",
        input=JsonContent(json={"count_value": "4"}),
    )
    assert isinstance(field_name_result, JsonContent)
    assert field_name_result.json == {"count_value": 4, "count_type": "int"}

    both_result = await toolkit.invoke(
        context=context,
        name="populate_by_name_values",
        input=JsonContent(json={"countValue": "3", "count_value": "4"}),
    )
    assert isinstance(both_result, JsonContent)
    assert both_result.json == {"count_value": 3, "count_type": "int"}

    forbid_field_name_result = await toolkit.invoke(
        context=context,
        name="populate_by_name_forbid_values",
        input=JsonContent(json={"count_value": "4"}),
    )
    assert isinstance(forbid_field_name_result, JsonContent)
    assert forbid_field_name_result.json == {"count_value": 4, "count_type": "int"}

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="populate_by_name_forbid_values",
            input=JsonContent(json={"countValue": "3", "count_value": "4"}),
        )

    allow_field_name_result = await toolkit.invoke(
        context=context,
        name="populate_by_name_allow_values",
        input=JsonContent(json={"count_value": "4"}),
    )
    assert isinstance(allow_field_name_result, JsonContent)
    assert allow_field_name_result.json == {
        "count_value": 4,
        "count_type": "int",
        "extras": {},
    }

    allow_both_result = await toolkit.invoke(
        context=context,
        name="populate_by_name_allow_values",
        input=JsonContent(json={"countValue": "3", "count_value": "4"}),
    )
    assert isinstance(allow_both_result, JsonContent)
    assert allow_both_result.json == {
        "count_value": 3,
        "count_type": "int",
        "extras": {"count_value": "4"},
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
async def test_pydantic_tool_content_types_mode_validates_multiple_of_numbers() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_MultipleOfTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="multiple_of_values",
        input=JsonContent(json={"count": "6", "ratio": "1.5"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "count": 6,
        "count_type": "int",
        "ratio": 1.5,
        "ratio_type": "float",
    }

    for payload in (
        {"count": "7", "ratio": "1.5"},
        {"count": "6", "ratio": "1.25"},
        {"count": "x", "ratio": "1.5"},
    ):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="multiple_of_values",
                input=JsonContent(json=payload),
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
async def test_pydantic_tool_content_types_mode_preserves_enum_behavior() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_EnumTool(), _EnumValuesTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    enum_result = await toolkit.invoke(
        context=context,
        name="enum_values",
        input=JsonContent(json={"color": "red"}),
    )

    assert isinstance(enum_result, JsonContent)
    assert enum_result.json == {
        "color_type": "_Color",
        "color_name": "RED",
        "color_value": "red",
    }

    raw_result = await toolkit.invoke(
        context=context,
        name="enum_raw_values",
        input=JsonContent(json={"color": "red"}),
    )

    assert isinstance(raw_result, JsonContent)
    assert raw_result.json == {"color_type": "str", "color": "red"}

    for name in ("enum_values", "enum_raw_values"):
        for bad_color in ("RED", 1):
            with pytest.raises(Exception):
                await toolkit.invoke(
                    context=context,
                    name=name,
                    input=JsonContent(json={"color": bad_color}),
                )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_coerces_discriminated_unions() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_DiscriminatedUnionTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    cat_result = await toolkit.invoke(
        context=context,
        name="discriminated_union_values",
        input=JsonContent(json={"pet": {"pet_type": "cat", "lives": "9"}}),
    )

    assert isinstance(cat_result, JsonContent)
    assert cat_result.json == {
        "pet_type": "_CatPayload",
        "tag": "cat",
        "lives": 9,
        "lives_type": "int",
    }

    dog_result = await toolkit.invoke(
        context=context,
        name="discriminated_union_values",
        input=JsonContent(json={"pet": {"pet_type": "dog", "bark": "yes"}}),
    )

    assert isinstance(dog_result, JsonContent)
    assert dog_result.json == {
        "pet_type": "_DogPayload",
        "tag": "dog",
        "bark": True,
        "bark_type": "bool",
    }

    invalid_inputs = [
        {"pet": {"pet_type": "cat", "bark": True}},
        {"pet": {"pet_type": "dog", "lives": 9}},
        {"pet": {"pet_type": "bird", "lives": 1}},
        {"pet": {"lives": 1}},
    ]
    for input_json in invalid_inputs:
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="discriminated_union_values",
                input=JsonContent(json=input_json),
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
async def test_pydantic_tool_content_types_mode_preserves_nested_allowed_extra() -> (
    None
):
    toolkit = Toolkit(
        name="test",
        tools=[_NestedAllowExtraTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="nested_allow_extra",
        input=JsonContent(json={"child": {"count": "3", "extra": "raw"}}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "count": 3,
        "count_type": "int",
        "extras": {"extra": "raw"},
    }

    for input_json in (
        {"child": {"count": "bad", "extra": "raw"}},
        {"child": {"extra": "raw"}},
    ):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="nested_allow_extra",
                input=JsonContent(json=input_json),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_preserves_root_model_boundary() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_RootListTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    with pytest.raises(Exception, match="requires JSON object input"):
        await toolkit.invoke(
            context=context,
            name="root_list_values",
            input=JsonContent(json=[1, "2"]),
        )

    with pytest.raises(Exception, match="Input should be a valid list"):
        await toolkit.invoke(
            context=context,
            name="root_list_values",
            input=JsonContent(json={}),
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
        "made": [1, 2],
        "made_types": ["int", "int"],
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
        "made": [1, 2],
        "made_types": ["int", "int"],
    }

    supplied_result = await toolkit.invoke(
        context=context,
        name="default_values",
        input=JsonContent(json={"made": ["3", "4.0"]}),
    )

    assert isinstance(supplied_result, JsonContent)
    assert supplied_result.json == {
        "count": 3,
        "count_type": "int",
        "enabled": True,
        "enabled_type": "bool",
        "label": "alpha",
        "label_type": "str",
        "made": [3, 4],
        "made_types": ["int", "int"],
    }


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_preserves_unvalidated_defaults() -> (
    None
):
    toolkit = Toolkit(
        name="test",
        tools=[_UnvalidatedDefaultTool(), _ValidateDefaultTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    raw_default_result = await toolkit.invoke(
        context=context,
        name="unvalidated_default_values",
        input=JsonContent(json={}),
    )

    assert isinstance(raw_default_result, JsonContent)
    assert raw_default_result.json == {"count": "3", "count_type": "str"}

    explicit_result = await toolkit.invoke(
        context=context,
        name="unvalidated_default_values",
        input=JsonContent(json={"count": "4"}),
    )

    assert isinstance(explicit_result, JsonContent)
    assert explicit_result.json == {"count": 4, "count_type": "int"}

    validated_default_result = await toolkit.invoke(
        context=context,
        name="validate_default_values",
        input=JsonContent(json={}),
    )

    assert isinstance(validated_default_result, JsonContent)
    assert validated_default_result.json == {"count": 3, "count_type": "int"}


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
async def test_pydantic_tool_content_types_mode_rejects_forbidden_extra_fields() -> (
    None
):
    toolkit = Toolkit(
        name="test",
        tools=[_ForbidExtraTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="forbid_extra",
        input=JsonContent(json={"count": "2"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"count": 2, "count_type": "int"}

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="forbid_extra",
            input=JsonContent(json={"count": "2", "extra": 1}),
        )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_preserves_allowed_extra_fields() -> (
    None
):
    toolkit = Toolkit(
        name="test",
        tools=[_AllowExtraTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="allow_extra",
        input=JsonContent(json={"count": "2", "extra": 1, "label": "raw"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "count": 2,
        "count_type": "int",
        "extras": {"extra": 1, "label": "raw"},
    }

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="allow_extra",
            input=JsonContent(json={"count": "x", "extra": 1}),
        )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_respects_strict_models() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_StrictTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="strict_values",
        input=JsonContent(json={"count": 3, "enabled": True, "ratio": 1}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "count": 3,
        "count_type": "int",
        "enabled": True,
        "enabled_type": "bool",
        "ratio": 1.0,
        "ratio_type": "float",
    }

    invalid_inputs = [
        {"count": "3", "enabled": True, "ratio": 1.5},
        {"count": True, "enabled": True, "ratio": 1.5},
        {"count": 3, "enabled": "true", "ratio": 1.5},
        {"count": 3, "enabled": True, "ratio": "1.5"},
    ]
    for input_json in invalid_inputs:
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="strict_values",
                input=JsonContent(json=input_json),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_respects_strict_fields() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_StrictFieldTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="strict_field_values",
        input=JsonContent(
            json={
                "strict_count": 3,
                "loose_count": "4",
                "strict_enabled": True,
                "loose_enabled": "yes",
                "strict_ratio": 1,
                "loose_ratio": "2.5",
            }
        ),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "strict_count": 3,
        "strict_count_type": "int",
        "loose_count": 4,
        "loose_count_type": "int",
        "strict_enabled": True,
        "strict_enabled_type": "bool",
        "loose_enabled": True,
        "loose_enabled_type": "bool",
        "strict_ratio": 1.0,
        "strict_ratio_type": "float",
        "loose_ratio": 2.5,
        "loose_ratio_type": "float",
    }

    invalid_inputs = [
        {
            "strict_count": "3",
            "loose_count": "4",
            "strict_enabled": True,
            "loose_enabled": "yes",
            "strict_ratio": 1,
            "loose_ratio": "2.5",
        },
        {
            "strict_count": True,
            "loose_count": "4",
            "strict_enabled": True,
            "loose_enabled": "yes",
            "strict_ratio": 1,
            "loose_ratio": "2.5",
        },
        {
            "strict_count": 3,
            "loose_count": "4",
            "strict_enabled": "true",
            "loose_enabled": "yes",
            "strict_ratio": 1,
            "loose_ratio": "2.5",
        },
        {
            "strict_count": 3,
            "loose_count": "4",
            "strict_enabled": True,
            "loose_enabled": "yes",
            "strict_ratio": "1.5",
            "loose_ratio": "2.5",
        },
    ]
    for input_json in invalid_inputs:
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="strict_field_values",
                input=JsonContent(json=input_json),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_respects_nested_strict_fields() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_NestedStrictFieldTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="nested_strict_field_values",
        input=JsonContent(json={"child": {"strict_count": 3, "loose_count": "4"}}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "strict_count": 3,
        "strict_count_type": "int",
        "loose_count": 4,
        "loose_count_type": "int",
    }

    invalid_inputs = [
        {"child": {"strict_count": "3", "loose_count": "4"}},
        {"child": {"strict_count": True, "loose_count": "4"}},
    ]
    for input_json in invalid_inputs:
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="nested_strict_field_values",
                input=JsonContent(json=input_json),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_parses_formatted_scalars() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_FormattedTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="formatted_values",
        input=JsonContent(
            json={
                "day": "2026-07-02",
                "moment": "2026-07-02T03:04:05Z",
                "clock": "03:04:05",
                "ident": "12345678-1234-5678-1234-567812345678",
            }
        ),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "day_type": "date",
        "day": "2026-07-02",
        "moment_type": "datetime",
        "moment": "2026-07-02T03:04:05+00:00",
        "clock_type": "time",
        "clock": "03:04:05",
        "ident_type": "UUID",
        "ident": "12345678-1234-5678-1234-567812345678",
    }

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="formatted_values",
            input=JsonContent(
                json={
                    "day": "not-a-date",
                    "moment": "2026-07-02T03:04:05Z",
                    "clock": "03:04:05",
                    "ident": "12345678-1234-5678-1234-567812345678",
                }
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "day",
    [
        "2026-07-02T00:00:00",
        "2026-07-02T00:00:00Z",
        1782950400,
        1782950400.0,
        "1782950400",
    ],
)
async def test_pydantic_tool_content_types_mode_coerces_date_edges(
    day: object,
) -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_FormattedTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="formatted_values",
        input=JsonContent(
            json={
                "day": day,
                "moment": "2026-07-02T03:04:05Z",
                "clock": "03:04:05",
                "ident": "12345678-1234-5678-1234-567812345678",
            }
        ),
    )

    assert isinstance(result, JsonContent)
    assert result.json["day"] == "2026-07-02"
    assert result.json["day_type"] == "date"

    for bad_day in ("2026-07-02T12:00:00Z", True):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="formatted_values",
                input=JsonContent(
                    json={
                        "day": bad_day,
                        "moment": "2026-07-02T03:04:05Z",
                        "clock": "03:04:05",
                        "ident": "12345678-1234-5678-1234-567812345678",
                    }
                ),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_coerces_time_without_seconds() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_FormattedTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="formatted_values",
        input=JsonContent(
            json={
                "day": "2026-07-02",
                "moment": "2026-07-02T03:04:05Z",
                "clock": "03:04",
                "ident": "12345678-1234-5678-1234-567812345678",
            }
        ),
    )

    assert isinstance(result, JsonContent)
    assert result.json["clock"] == "03:04:00"
    assert result.json["clock_type"] == "time"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("03:04:05Z", "03:04:05+00:00"),
        ("03:04:05+00:00", "03:04:05+00:00"),
        ("03:04:05.123456Z", "03:04:05.123456+00:00"),
        (11045, "03:04:05+00:00"),
        (11045.123456, "03:04:05.123456+00:00"),
    ],
)
async def test_pydantic_tool_content_types_mode_coerces_time_offsets_and_seconds(
    clock: object,
    expected: str,
) -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_TimeEdgeTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="time_edge_values",
        input=JsonContent(json={"clock": clock}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"clock_type": "time", "clock": expected}

    for bad_clock in (86400, -1, "11045"):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="time_edge_values",
                input=JsonContent(json={"clock": bad_clock}),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (1782961445, "2026-07-02T03:04:05+00:00"),
        (1782961445.123456, "2026-07-02T03:04:05.123456+00:00"),
        ("1782961445", "2026-07-02T03:04:05+00:00"),
    ],
)
async def test_pydantic_tool_content_types_mode_coerces_datetime_timestamps(
    moment: object,
    expected: str,
) -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_FormattedTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="formatted_values",
        input=JsonContent(
            json={
                "day": "2026-07-02",
                "moment": moment,
                "clock": "03:04:05",
                "ident": "12345678-1234-5678-1234-567812345678",
            }
        ),
    )

    assert isinstance(result, JsonContent)
    assert result.json["moment"] == expected
    assert result.json["moment_type"] == "datetime"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        ("2026-07-02 03:04:05", "2026-07-02T03:04:05"),
        ("2026-07-02 03:04:05.123456", "2026-07-02T03:04:05.123456"),
        ("2026-07-02T03:04:05", "2026-07-02T03:04:05"),
        ("2026-07-02T03:04:05.123456", "2026-07-02T03:04:05.123456"),
    ],
)
async def test_pydantic_tool_content_types_mode_preserves_naive_datetimes(
    moment: str,
    expected: str,
) -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_NaiveDatetimeTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="naive_datetime_values",
        input=JsonContent(json={"moment": moment}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "moment_type": "datetime",
        "moment": expected,
        "tzinfo": None,
    }

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="naive_datetime_values",
            input=JsonContent(json={"moment": "not-a-datetime"}),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"amount": "12.30"}, {"amount_type": "Decimal", "amount": "12.30"}),
        ({"amount": 12.3}, {"amount_type": "Decimal", "amount": "12.3"}),
        ({"amount": 12}, {"amount_type": "Decimal", "amount": "12"}),
    ],
)
async def test_pydantic_tool_content_types_mode_parses_decimal_scalars(
    payload: dict[str, object],
    expected: dict[str, str],
) -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_DecimalTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="decimal_values",
        input=JsonContent(json=payload),
    )

    assert isinstance(result, JsonContent)
    assert result.json == expected

    for bad_amount in ("NaN", "Infinity", True, False):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="decimal_values",
                input=JsonContent(json={"amount": bad_amount}),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_preserves_string_exactness() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_StringTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="string_values",
        input=JsonContent(json={"value": "alpha"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"value_type": "str", "value": "alpha"}

    for bad_value in (2, 2.5, True, None):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="string_values",
                input=JsonContent(json={"value": bad_value}),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_coerces_numbers_to_strings() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_CoerceNumberStringTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    for value, expected in (("x", "x"), (3, "3"), (3.5, "3.5")):
        result = await toolkit.invoke(
            context=context,
            name="coerce_number_string_values",
            input=JsonContent(json={"value": value}),
        )

        assert isinstance(result, JsonContent)
        assert result.json == {"value_type": "str", "value": expected}

    for bad_value in (True, False, None, [], {"a": 1}):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="coerce_number_string_values",
                input=JsonContent(json={"value": bad_value}),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_strips_string_whitespace() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_StripStringTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="strip_string_values",
        input=JsonContent(json={"value": "  Alpha  ", "code": "  xy  "}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "value_type": "str",
        "value": "Alpha",
        "code_type": "str",
        "code": "xy",
    }

    invalid_inputs = [
        {"value": 3, "code": "  xy  "},
        {"value": " ok ", "code": " x "},
        {"value": " ok ", "code": "   "},
    ]
    for input_json in invalid_inputs:
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="strip_string_values",
                input=JsonContent(json=input_json),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_normalizes_string_case() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_LowerStringTool(), _UpperStringTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    lower_result = await toolkit.invoke(
        context=context,
        name="lower_string_values",
        input=JsonContent(json={"value": "AbC", "code": "abc"}),
    )
    assert isinstance(lower_result, JsonContent)
    assert lower_result.json == {
        "value_type": "str",
        "value": "abc",
        "code_type": "str",
        "code": "abc",
    }

    upper_result = await toolkit.invoke(
        context=context,
        name="upper_string_values",
        input=JsonContent(json={"value": "AbC", "code": "ABC"}),
    )
    assert isinstance(upper_result, JsonContent)
    assert upper_result.json == {
        "value_type": "str",
        "value": "ABC",
        "code_type": "str",
        "code": "ABC",
    }

    invalid_inputs = [
        ("lower_string_values", {"value": "abc", "code": "AbC"}),
        ("lower_string_values", {"value": 3, "code": "abc"}),
        ("upper_string_values", {"value": "abc", "code": "AbC"}),
        ("upper_string_values", {"value": 3, "code": "ABC"}),
    ]
    for name, input_json in invalid_inputs:
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name=name,
                input=JsonContent(json=input_json),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_validates_string_constraints() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_ConstrainedStringTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    for code in ("x1", "x123"):
        result = await toolkit.invoke(
            context=context,
            name="constrained_string_values",
            input=JsonContent(json={"code": code}),
        )

        assert isinstance(result, JsonContent)
        assert result.json == {"code_type": "str", "code": code}

    for bad_code in ("x", "x1234", "y12", 12):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="constrained_string_values",
                input=JsonContent(json={"code": bad_code}),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_runs_custom_validators() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_CustomValidatorTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="custom_validator_values",
        input=JsonContent(json={"code": "x-alpha"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"code_type": "str", "code": "ALPHA-OK"}

    for bad_code in ("alpha", 12):
        with pytest.raises(Exception):
            await toolkit.invoke(
                context=context,
                name="custom_validator_values",
                input=JsonContent(json={"code": bad_code}),
            )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_runs_before_custom_validators() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_BeforeCustomValidatorTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="before_custom_validator_values",
        input=JsonContent(json={"legacy_code": "alpha"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"code_type": "str", "code": "ALPHA"}

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="before_custom_validator_values",
            input=JsonContent(json={"other": "alpha"}),
        )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_runs_field_before_custom_validators() -> (
    None
):
    toolkit = Toolkit(
        name="test",
        tools=[_FieldBeforeCustomValidatorTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="field_before_custom_validator_values",
        input=JsonContent(json={"code": 7}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"code_type": "str", "code": "X-7"}

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="field_before_custom_validator_values",
            input=JsonContent(json={"code": {"bad": "value"}}),
        )


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_runs_field_validators_on_validated_defaults() -> (
    None
):
    toolkit = Toolkit(
        name="test",
        tools=[_FieldValidatorDefaultTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    omitted_result = await toolkit.invoke(
        context=context,
        name="field_validator_default_values",
        input=JsonContent(json={}),
    )

    assert isinstance(omitted_result, JsonContent)
    assert omitted_result.json == {
        "skipped": "x-skip",
        "validated": "X-RUN-AFTER",
    }

    explicit_result = await toolkit.invoke(
        context=context,
        name="field_validator_default_values",
        input=JsonContent(json={"skipped": "x-explicit", "validated": "x-explicit"}),
    )

    assert isinstance(explicit_result, JsonContent)
    assert explicit_result.json == {
        "skipped": "X-EXPLICIT-AFTER",
        "validated": "X-EXPLICIT-AFTER",
    }


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_runs_plain_field_validators() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_PlainFieldValidatorTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="plain_field_validator_values",
        input=JsonContent(json={"code": {"code": 7}}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"code_type": "str", "code": "X-7"}


@pytest.mark.asyncio
async def test_pydantic_tool_content_types_mode_runs_after_custom_validators() -> None:
    toolkit = Toolkit(
        name="test",
        tools=[_AfterCustomValidatorTool()],
        validation_mode="content_types",
    )
    context = ToolContext(caller=object())

    result = await toolkit.invoke(
        context=context,
        name="after_custom_validator_values",
        input=JsonContent(json={"left": "4", "right": "5"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"left": 5, "right": 5, "total": 10}

    with pytest.raises(Exception):
        await toolkit.invoke(
            context=context,
            name="after_custom_validator_values",
            input=JsonContent(json={"left": "1", "right": "2"}),
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
