from __future__ import annotations

import codecs
from collections.abc import AsyncIterable
from typing import Literal

from .tool import ToolContext

DEFAULT_MAX_LOG_LINE_LENGTH = 2048


def output_truncation_notice(*, max_length: int) -> str:
    return f"[output truncated after {max_length} characters]"


class StreamOutputAccumulator:
    def __init__(
        self,
        *,
        context: ToolContext,
        item_id: str,
        source: Literal["stdout", "stderr"],
        encoding: str,
        max_length: int,
        max_log_line_length: int = DEFAULT_MAX_LOG_LINE_LENGTH,
    ) -> None:
        if max_length <= 0:
            raise ValueError("max_length must be greater than 0")
        if max_log_line_length <= 0:
            raise ValueError("max_log_line_length must be greater than 0")

        decoder_factory = codecs.getincrementaldecoder(encoding)
        self._decoder = decoder_factory(errors="replace")
        self._context = context
        self._item_id = item_id
        self._source = source
        self._max_length = max_length
        self._max_log_line_length = max_log_line_length
        self._result_parts: list[str] = []
        self._captured_length = 0
        self._truncated = False
        self._log_buffer = ""
        self._log_remaining = max_length
        self._log_notice_emitted = False
        self._finalized = False

    @property
    def truncated(self) -> bool:
        return self._truncated

    def _emit_output_lines(self, *, lines: list[str]) -> None:
        if self._item_id == "" or len(lines) == 0:
            return
        self._context.emit(
            {
                "type": "meshagent.handler.output",
                "item_id": self._item_id,
                "lines": [
                    {
                        "source": self._source,
                        "text": line,
                    }
                    for line in lines
                ],
            }
        )

    def _append_result_text(self, *, text: str) -> None:
        if text == "":
            return

        if self._captured_length >= self._max_length:
            self._truncated = True
            return

        remaining = self._max_length - self._captured_length
        if len(text) > remaining:
            self._result_parts.append(text[:remaining])
            self._captured_length = self._max_length
            self._truncated = True
            return

        self._result_parts.append(text)
        self._captured_length += len(text)

    def _append_log_piece(self, *, lines: list[str], piece: str) -> None:
        if self._log_notice_emitted:
            return

        if self._log_remaining <= 0:
            self._log_notice_emitted = True
            lines.append(output_truncation_notice(max_length=self._max_length))
            return

        if len(piece) <= self._log_remaining:
            lines.append(piece)
            self._log_remaining -= len(piece)
            return

        lines.append(piece[: self._log_remaining])
        self._log_remaining = 0
        self._log_notice_emitted = True
        lines.append(output_truncation_notice(max_length=self._max_length))

    def _drain_available_log_lines(self) -> None:
        lines: list[str] = []

        while True:
            newline_index = self._log_buffer.find("\n")
            if newline_index > self._max_log_line_length:
                piece = self._log_buffer[: self._max_log_line_length]
                self._log_buffer = self._log_buffer[self._max_log_line_length :]
                self._append_log_piece(lines=lines, piece=piece)
                if self._log_notice_emitted:
                    self._log_buffer = ""
                    break
                continue

            if newline_index >= 0:
                line = self._log_buffer[:newline_index]
                if line.endswith("\r"):
                    line = line[:-1]
                self._log_buffer = self._log_buffer[newline_index + 1 :]
                self._append_log_piece(lines=lines, piece=line)
                if self._log_notice_emitted:
                    self._log_buffer = ""
                    break
                continue

            if len(self._log_buffer) > self._max_log_line_length:
                piece = self._log_buffer[: self._max_log_line_length]
                self._log_buffer = self._log_buffer[self._max_log_line_length :]
                self._append_log_piece(lines=lines, piece=piece)
                if self._log_notice_emitted:
                    self._log_buffer = ""
                    break
                continue

            break

        self._emit_output_lines(lines=lines)

    def feed_chunk(self, chunk: bytes | bytearray | memoryview) -> None:
        if self._finalized:
            return

        if isinstance(chunk, memoryview):
            chunk = chunk.tobytes()
        elif isinstance(chunk, bytearray):
            chunk = bytes(chunk)

        text = self._decoder.decode(chunk, final=False)
        if text == "":
            return

        self._append_result_text(text=text)
        self._log_buffer += text
        self._drain_available_log_lines()

    def finish(self) -> str:
        if self._finalized:
            return self.render()

        tail = self._decoder.decode(b"", final=True)
        if tail != "":
            self._append_result_text(text=tail)
            self._log_buffer += tail

        self._flush_log_remainder()
        self._finalized = True
        return self.render()

    def _flush_log_remainder(self) -> None:
        if self._log_notice_emitted or self._log_buffer == "":
            self._log_buffer = ""
            return

        lines: list[str] = []
        while self._log_buffer != "":
            piece = self._log_buffer[: self._max_log_line_length]
            self._log_buffer = self._log_buffer[self._max_log_line_length :]
            self._append_log_piece(lines=lines, piece=piece)
            if self._log_notice_emitted:
                self._log_buffer = ""
                break

        self._emit_output_lines(lines=lines)

    def render(self) -> str:
        output = "".join(self._result_parts)
        if not self._truncated:
            return output

        notice = output_truncation_notice(max_length=self._max_length)
        if output == "":
            return notice
        return f"{output}\n\n{notice}"


async def collect_output_stream(
    *,
    stream: AsyncIterable[bytes],
    accumulator: StreamOutputAccumulator,
) -> None:
    async for chunk in stream:
        accumulator.feed_chunk(chunk)
