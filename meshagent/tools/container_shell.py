from meshagent.api import RoomClient
from .tool import ToolContext, FunctionTool
from .toolkit import Toolkit, ToolkitBuilder

from meshagent.api.specs.service import ContainerMountSpec, RoomStorageMountSpec
from typing import Literal, Optional
import os

import logging
import asyncio
from pydantic import BaseModel

logger = logging.getLogger("container_shell_tool")

_SHELL_LOG_EVENT_TYPE = "meshagent.handler.output"


DEFAULT_CONTAINER_MOUNT_SPEC = ContainerMountSpec(
    room=[RoomStorageMountSpec(path="/data")]
)


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

    async def _drain_complete_log_lines(
        self,
        *,
        context: ToolContext,
        item_id: str,
        source: Literal["stdout", "stderr"],
        buffer: bytearray,
        encoding: str,
    ) -> None:
        lines: list[str] = []
        while True:
            newline_index = buffer.find(b"\n")
            if newline_index < 0:
                break
            raw_line = bytes(buffer[:newline_index])
            del buffer[: newline_index + 1]
            lines.append(raw_line.decode(encoding, errors="replace"))
        self._emit_output_lines(
            context=context,
            item_id=item_id,
            source=source,
            lines=lines,
        )

    def _emit_log_remainder(
        self,
        *,
        context: ToolContext,
        item_id: str,
        source: Literal["stdout", "stderr"],
        buffer: bytearray,
        encoding: str,
    ) -> None:
        if len(buffer) == 0:
            return
        self._emit_output_lines(
            context=context,
            item_id=item_id,
            source=source,
            lines=[bytes(buffer).decode(encoding, errors="replace")],
        )
        buffer.clear()

    async def _collect_output_stream(
        self,
        *,
        stream,
        context: ToolContext,
        item_id: str,
        source: Literal["stdout", "stderr"],
        total_buffer: bytearray,
        line_buffer: bytearray,
        encoding: str,
    ) -> None:
        async for chunk in stream:
            total_buffer.extend(chunk)
            line_buffer.extend(chunk)
            await self._drain_complete_log_lines(
                context=context,
                item_id=item_id,
                source=source,
                buffer=line_buffer,
                encoding=encoding,
            )

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

        left = max_output_length

        def limit(s: str):
            nonlocal left
            if left is not None:
                s = s[0:left]
                left -= len(s)
                return s
            else:
                return s

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

                stdout = bytearray()
                stderr = bytearray()
                stdout_lines = bytearray()
                stderr_lines = bytearray()
                stdout_task = None
                stderr_task = None

                try:
                    async with asyncio.timeout(timeout):
                        stdout_task = asyncio.create_task(
                            self._collect_output_stream(
                                stream=exec.stdout(),
                                context=context,
                                item_id=item_id,
                                source="stdout",
                                total_buffer=stdout,
                                line_buffer=stdout_lines,
                                encoding=encoding,
                            )
                        )
                        stderr_task = asyncio.create_task(
                            self._collect_output_stream(
                                stream=exec.stderr(),
                                context=context,
                                item_id=item_id,
                                source="stderr",
                                total_buffer=stderr,
                                line_buffer=stderr_lines,
                                encoding=encoding,
                            )
                        )
                        await asyncio.gather(stdout_task, stderr_task)
                        self._emit_log_remainder(
                            context=context,
                            item_id=item_id,
                            source="stdout",
                            buffer=stdout_lines,
                            encoding=encoding,
                        )
                        self._emit_log_remainder(
                            context=context,
                            item_id=item_id,
                            source="stderr",
                            buffer=stderr_lines,
                            encoding=encoding,
                        )

                        exit_code = await exec.result

                        results.append(
                            {
                                "outcome": {
                                    "type": "exit",
                                    "exit_code": exit_code,
                                },
                                "stdout": stdout.decode(),
                                "stderr": stderr.decode(),
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
                    self._emit_log_remainder(
                        context=context,
                        item_id=item_id,
                        source="stdout",
                        buffer=stdout_lines,
                        encoding=encoding,
                    )
                    self._emit_log_remainder(
                        context=context,
                        item_id=item_id,
                        source="stderr",
                        buffer=stderr_lines,
                        encoding=encoding,
                    )

                    results.append(
                        {
                            "outcome": {"type": "timeout"},
                            "stdout": limit(stdout.decode(encoding, errors="replace")),
                            "stderr": limit(stderr.decode(encoding, errors="replace")),
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
