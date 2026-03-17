from meshagent.api import RoomClient
from .tool import ToolContext, FunctionTool
from .toolkit import Toolkit, ToolkitBuilder

from meshagent.api.specs.service import ContainerMountSpec, RoomStorageMountSpec
from typing import Literal, Optional
import os
import codecs

import logging
import asyncio
from pydantic import BaseModel

logger = logging.getLogger("container_shell_tool")

_SHELL_LOG_EVENT_TYPE = "meshagent.handler.output"
DEFAULT_MAX_OUTPUT_LENGTH = 50 * 1024
MAX_LOG_LINE_LENGTH = 2048


DEFAULT_CONTAINER_MOUNT_SPEC = ContainerMountSpec(
    room=[RoomStorageMountSpec(path="/data")]
)


def _output_truncation_notice(*, max_length: int) -> str:
    return f"[output truncated after {max_length} characters]"


class _BoundedTextResult:
    def __init__(self, *, max_length: int) -> None:
        self._max_length = max_length
        self._parts: list[str] = []
        self._captured_length = 0
        self._truncated = False

    def append(self, text: str) -> None:
        if text == "":
            return

        if self._captured_length >= self._max_length:
            self._truncated = True
            return

        remaining = self._max_length - self._captured_length
        if len(text) > remaining:
            self._parts.append(text[:remaining])
            self._captured_length = self._max_length
            self._truncated = True
            return

        self._parts.append(text)
        self._captured_length += len(text)

    def render(self) -> str:
        output = "".join(self._parts)
        if not self._truncated:
            return output

        notice = _output_truncation_notice(max_length=self._max_length)
        if output == "":
            return notice

        return f"{output}\n\n{notice}"


class _LiveLogBuffer:
    def __init__(
        self,
        *,
        context: ToolContext,
        item_id: str,
        source: Literal["stdout", "stderr"],
        max_length: int,
    ) -> None:
        self._context = context
        self._item_id = item_id
        self._source = source
        self._max_length = max_length
        self._remaining = max_length
        self._buffer = ""
        self._notice_emitted = False

    def _emit_lines(self, lines: list[str]) -> None:
        ContainerShellTool._emit_output_lines(
            context=self._context,
            item_id=self._item_id,
            source=self._source,
            lines=lines,
        )

    def _append_piece(self, *, lines: list[str], piece: str) -> None:
        if self._notice_emitted:
            return

        if self._remaining <= 0:
            self._notice_emitted = True
            lines.append(_output_truncation_notice(max_length=self._max_length))
            return

        if len(piece) <= self._remaining:
            lines.append(piece)
            self._remaining -= len(piece)
            return

        lines.append(piece[: self._remaining])
        self._remaining = 0
        self._notice_emitted = True
        lines.append(_output_truncation_notice(max_length=self._max_length))

    def _drain_available_lines(self) -> None:
        lines: list[str] = []

        while True:
            newline_index = self._buffer.find("\n")
            if newline_index > MAX_LOG_LINE_LENGTH:
                piece = self._buffer[:MAX_LOG_LINE_LENGTH]
                self._buffer = self._buffer[MAX_LOG_LINE_LENGTH:]
                self._append_piece(lines=lines, piece=piece)
                if self._notice_emitted:
                    self._buffer = ""
                    break
                continue

            if newline_index >= 0:
                line = self._buffer[:newline_index]
                if line.endswith("\r"):
                    line = line[:-1]
                self._buffer = self._buffer[newline_index + 1 :]
                self._append_piece(lines=lines, piece=line)
                if self._notice_emitted:
                    self._buffer = ""
                    break
                continue

            if len(self._buffer) > MAX_LOG_LINE_LENGTH:
                piece = self._buffer[:MAX_LOG_LINE_LENGTH]
                self._buffer = self._buffer[MAX_LOG_LINE_LENGTH:]
                self._append_piece(lines=lines, piece=piece)
                if self._notice_emitted:
                    self._buffer = ""
                    break
                continue

            break

        self._emit_lines(lines)

    def feed(self, text: str) -> None:
        if text == "" or self._notice_emitted:
            return

        self._buffer += text
        self._drain_available_lines()

    def flush(self) -> None:
        if self._notice_emitted or self._buffer == "":
            self._buffer = ""
            return

        lines: list[str] = []
        while self._buffer != "":
            piece = self._buffer[:MAX_LOG_LINE_LENGTH]
            self._buffer = self._buffer[MAX_LOG_LINE_LENGTH:]
            self._append_piece(lines=lines, piece=piece)
            if self._notice_emitted:
                self._buffer = ""
                break

        self._emit_lines(lines)


class _StreamOutputAccumulator:
    def __init__(
        self,
        *,
        context: ToolContext,
        item_id: str,
        source: Literal["stdout", "stderr"],
        encoding: str,
        max_length: int,
    ) -> None:
        decoder_factory = codecs.getincrementaldecoder(encoding)
        self._decoder = decoder_factory(errors="replace")
        self._result = _BoundedTextResult(max_length=max_length)
        self._logs = _LiveLogBuffer(
            context=context,
            item_id=item_id,
            source=source,
            max_length=max_length,
        )
        self._finalized = False

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

        self._result.append(text)
        self._logs.feed(text)

    def finish(self) -> str:
        if self._finalized:
            return self._result.render()

        tail = self._decoder.decode(b"", final=True)
        if tail != "":
            self._result.append(tail)
            self._logs.feed(tail)

        self._logs.flush()
        self._finalized = True
        return self._result.render()


class ContainerShellToolConfig(BaseModel):
    name: Literal["container_shell"] = "container_shell"


class ContainerShellToolkitBuilder(ToolkitBuilder):
    def __init__(
        self,
        *,
        name: str = "container_shell",
        working_dir: Optional[str] = None,
        image: Optional[str] = "python:3.13",
        mounts: Optional[ContainerMountSpec] = DEFAULT_CONTAINER_MOUNT_SPEC,
        env: Optional[dict[str, str]] = None,
    ):
        super().__init__(name=name, type=ContainerShellToolConfig)

        self.working_dir = working_dir
        self.image = image
        self.mounts = mounts
        self.env = env

    async def make(
        self, *, room: RoomClient, model: str, config: ContainerShellToolConfig
    ) -> Toolkit:
        return Toolkit(
            name=self.name,
            tools=[
                ContainerShellTool(
                    name=self.name,
                    working_dir=self.working_dir,
                    image=self.image,
                    mounts=self.mounts,
                    env=self.env,
                )
            ],
        )


class ContainerShellTool(FunctionTool):
    def __init__(
        self,
        *,
        name: str = "container_shell",
        description: Optional[str] = None,
        title: Optional[str] = None,
        working_dir: Optional[str] = None,
        image: Optional[str] = "python:3.13",
        mounts: Optional[ContainerMountSpec] = DEFAULT_CONTAINER_MOUNT_SPEC,
        env: Optional[dict[str, str]] = None,
    ):
        self.working_dir = working_dir
        self.image = image
        self.mounts = mounts
        self._container_id = None
        self.env = env

        super().__init__(
            name=name,
            description=description
            or "execute shell commands in a container and return the result",
            title=title,
            input_schema={
                "type": "object",
                "required": ["commands"],
                "additionalProperties": False,
                "properties": {
                    "commands": {"type": "array", "items": {"type": "string"}},
                    "max_output_length": {"type": "integer"},
                    "timeout_ms": {"type": "integer"},
                },
            },
        )

    @staticmethod
    def _item_id_from_context(context: ToolContext) -> str:
        caller_context = context.caller_context
        if isinstance(caller_context, dict):
            item_id = caller_context.get("item_id")
            if isinstance(item_id, str):
                return item_id
        return ""

    @staticmethod
    def _emit_output_lines(
        *,
        context: ToolContext,
        item_id: str,
        source: Literal["stdout", "stderr"],
        lines: list[str],
    ) -> None:
        if item_id == "" or len(lines) == 0:
            return
        context.emit(
            {
                "type": _SHELL_LOG_EVENT_TYPE,
                "item_id": item_id,
                "lines": [
                    {
                        "source": source,
                        "text": line,
                    }
                    for line in lines
                ],
            }
        )

    async def _collect_output_stream(
        self,
        *,
        stream,
        accumulator: _StreamOutputAccumulator,
    ) -> None:
        async for chunk in stream:
            accumulator.feed_chunk(chunk)

    async def execute(
        self,
        context: ToolContext,
        **kwargs,
    ):
        commands = kwargs.get("commands") or []
        max_output_length = kwargs.get("max_output_length")
        timeout_ms = kwargs.get("timeout_ms")

        if not commands:
            raise Exception("commands is required")

        if self.image is None:
            raise Exception("container_shell requires an image")

        results = []
        encoding = os.device_encoding(1) or "utf-8"
        item_id = self._item_id_from_context(context)
        effective_max_output_length = (
            int(max_output_length)
            if max_output_length is not None
            else DEFAULT_MAX_OUTPUT_LENGTH
        )
        if effective_max_output_length <= 0:
            raise ValueError("max_output_length must be greater than 0")

        timeout = float(timeout_ms) / 1000.0 if timeout_ms else 20.0

        running = False

        if self._container_id:
            for c in await context.room.containers.list():
                if c.id == self._container_id:
                    running = True

        if not running:
            self._container_id = await context.room.containers.run(
                command="sleep infinity",
                image=self.image,
                mounts=self.mounts,
                writable_root_fs=True,
                env=self.env,
            )

        container_id = self._container_id

        try:
            logger.info(
                "executing shell commands in container %s with timeout %s: %s",
                container_id,
                timeout,
                commands,
            )
            import shlex

            for command in commands:
                command_to_run = command
                if self.working_dir:
                    command_to_run = f"cd {shlex.quote(self.working_dir)} && {command}"
                exec = await context.room.containers.exec(
                    container_id=container_id,
                    command=["bash", "-lc", command_to_run],
                    tty=False,
                )

                stdout = _StreamOutputAccumulator(
                    context=context,
                    item_id=item_id,
                    source="stdout",
                    encoding=encoding,
                    max_length=effective_max_output_length,
                )
                stderr = _StreamOutputAccumulator(
                    context=context,
                    item_id=item_id,
                    source="stderr",
                    encoding=encoding,
                    max_length=effective_max_output_length,
                )
                stdout_task = None
                stderr_task = None

                try:
                    async with asyncio.timeout(timeout):
                        stdout_task = asyncio.create_task(
                            self._collect_output_stream(
                                stream=exec.stdout(),
                                accumulator=stdout,
                            )
                        )
                        stderr_task = asyncio.create_task(
                            self._collect_output_stream(
                                stream=exec.stderr(),
                                accumulator=stderr,
                            )
                        )
                        await asyncio.gather(stdout_task, stderr_task)

                        exit_code = await exec.result

                        results.append(
                            {
                                "outcome": {
                                    "type": "exit",
                                    "exit_code": exit_code,
                                },
                                "stdout": stdout.finish(),
                                "stderr": stderr.finish(),
                            }
                        )

                except asyncio.TimeoutError:
                    logger.info("The command timed out after %ss", timeout)
                    await exec.kill()
                    if stdout_task is not None:
                        stdout_task.cancel()
                    if stderr_task is not None:
                        stderr_task.cancel()
                    if stdout_task is not None or stderr_task is not None:
                        await asyncio.gather(
                            *[
                                task
                                for task in (stdout_task, stderr_task)
                                if task is not None
                            ],
                            return_exceptions=True,
                        )

                    results.append(
                        {
                            "outcome": {"type": "timeout"},
                            "stdout": stdout.finish(),
                            "stderr": stderr.finish(),
                        }
                    )
                    break

                except Exception as ex:
                    if stdout_task is not None:
                        stdout_task.cancel()
                    if stderr_task is not None:
                        stderr_task.cancel()
                    if stdout_task is not None or stderr_task is not None:
                        await asyncio.gather(
                            *[
                                task
                                for task in (stdout_task, stderr_task)
                                if task is not None
                            ],
                            return_exceptions=True,
                        )
                    results.append(
                        {
                            "outcome": {
                                "type": "exit",
                                "exit_code": 1,
                            },
                            "stdout": "",
                            "stderr": f"{ex}",
                        }
                    )
                    stdout.finish()
                    stderr.finish()
                    break

        except Exception as ex:
            results.append(
                {
                    "outcome": {
                        "type": "exit",
                        "exit_code": 1,
                    },
                    "stdout": "",
                    "stderr": f"{ex}",
                }
            )

        return {"results": results}
