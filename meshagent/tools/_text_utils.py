from __future__ import annotations

import re

from meshagent.api import RoomException

DEFAULT_TOOL_MAX_LENGTH = 20000
TRUNCATION_NOTICE = "\n\n[results were truncated. Use offset to read more.]"


def validate_max_length(*, max_length: int, tool_name: str) -> int:
    if isinstance(max_length, bool) or not isinstance(max_length, int):
        raise ValueError(f"{tool_name} max_length must be an integer")
    if max_length <= 0:
        raise ValueError(f"{tool_name} max_length must be greater than 0")
    return max_length


def normalize_offset(*, value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoomException("offset must be null or a non-negative integer")
    if value < 0:
        raise RoomException("offset must be a non-negative integer")
    return value


def normalize_context_lines(*, value: object, parameter_name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoomException(f"{parameter_name} must be null or a non-negative integer")
    if value < 0:
        raise RoomException(f"{parameter_name} must be a non-negative integer")
    return value


def truncate_text(
    *,
    text: str,
    offset: int,
    max_length: int,
    truncation_notice: str = TRUNCATION_NOTICE,
) -> str:
    if offset >= len(text):
        return ""

    remaining = text[offset:]
    if len(remaining) <= max_length:
        return remaining

    keep_length = max_length - len(truncation_notice)
    if keep_length <= 0:
        return truncation_notice[:max_length]
    return remaining[:keep_length] + truncation_notice


def grep_text(
    *,
    text: str,
    pattern: str,
    start_line: int = 1,
    before: int = 0,
    after: int = 0,
) -> str:
    normalized_pattern = pattern.strip()
    if normalized_pattern == "":
        raise RoomException("pattern must not be empty")

    try:
        regex = re.compile(normalized_pattern)
    except re.error as ex:
        raise RoomException(f"invalid regular expression pattern: {ex}") from ex

    lines = text.splitlines()
    matched_indices: list[int] = []
    for index, line in enumerate(lines):
        if regex.search(line) is not None:
            matched_indices.append(index)

    if len(matched_indices) == 0:
        return "No matches found."

    included_indices: set[int] = set()
    for match_index in matched_indices:
        start = max(0, match_index - before)
        end = min(len(lines), match_index + after + 1)
        for line_index in range(start, end):
            included_indices.add(line_index)

    matched_set = set(matched_indices)
    ordered_indices = sorted(included_indices)
    results: list[str] = []
    previous_index: int | None = None
    for line_index in ordered_indices:
        if previous_index is not None and line_index > previous_index + 1:
            results.append("--")

        line_number = start_line + line_index
        separator = ":" if line_index in matched_set else "-"
        results.append(f"{line_number}{separator} {lines[line_index]}")
        previous_index = line_index

    return "\n".join(results)
