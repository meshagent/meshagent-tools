import asyncio
import logging
import os
from collections.abc import AsyncIterable
from typing import Optional

from meshagent.api import RoomClient
from meshagent.api.specs.service import ContainerMountSpec, RoomStorageMountSpec
from meshagent.tools.tool import LocalRoomTool, ToolContext

from ._shell_output import (
    DEFAULT_MAX_LOG_LINE_LENGTH,
    StreamOutputAccumulator,
    collect_output_stream,
)

logger = logging.getLogger("script_tool")
MAX_LOG_LINE_LENGTH = DEFAULT_MAX_LOG_LINE_LENGTH


DEFAULT_CONTAINER_MOUNT_SPEC = ContainerMountSpec(
    room=[RoomStorageMountSpec(path="/data")]
)


async def _stream_reader_chunks(
    reader: asyncio.StreamReader | None,
) -> AsyncIterable[bytes]:
    if reader is None:
        return

    while True:
        chunk = await reader.read(4096)
        if chunk == b"":
            break
        yield chunk


async def _await_output_tasks(*tasks: asyncio.Task[None]) -> None:
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception) and not isinstance(
            result, asyncio.CancelledError
        ):
            logger.debug("script output stream task failed", exc_info=result)


class ScriptTool(LocalRoomTool):
    def __init__(
        self,
        *,
        room: RoomClient,
        name: str,
        commands: list[str],
        description: Optional[str] = None,
        title: Optional[str] = None,
        service_id: Optional[str] = None,
        working_dir: Optional[str] = None,
        image: Optional[str] = "python:3.13",
        mounts: Optional[ContainerMountSpec] = DEFAULT_CONTAINER_MOUNT_SPEC,
        env: Optional[dict[str, str]] = None,
        input_schema: Optional[dict] = None,
        max_output_length: int = 32000,
        timeout_ms: int = 30 * 60 * 1000,
    ):
        self.service_id = service_id
        self.working_dir = working_dir
        self.image = image
        self.mounts = mounts
        self._container_id = None
        self.env = env
        self.max_output_length = max_output_length
        self.timeout_ms = timeout_ms
        self.service_id = service_id
        self.commands = commands

        super().__init__(
            room=room,
            name=name,
            description=description,
            title=title,
            input_schema=input_schema
            or {
                "type": "object",
                "required": ["prompt"],
                "additionalProperties": False,
                "properties": {"prompt": {"type": "string"}},
            },
        )

    async def execute(
        self,
        context: ToolContext,
        **kwargs,
    ):
        merged_env = {**os.environ}

        results = []
        encoding = os.device_encoding(1) or "utf-8"
        item_id = ""
        if self.max_output_length <= 0:
            raise ValueError("max_output_length must be greater than 0")

        timeout = float(self.timeout_ms) / 1000.0 if self.timeout_ms else 20 * 1000.0

        if self.image is not None or self.service_id is not None:
            running = False

            if self._container_id:
                # make sure container is still running

                for c in await self.room.containers.list():
                    if c.id == self._container_id or (
                        self.service_id is not None and c.service_id == self.service_id
                    ):
                        running = True
                        break

            if not running:
                if self.service_id is not None:
                    env = {}

                    for k, v in kwargs.items():
                        env[k.upper()] = v

                        logger.info(
                            f"executing shell script in container with env {env}"
                        )

                    self._container_id = await self.room.containers.run_service(
                        service_id=self.service_id,
                        env=env,
                    )

                else:
                    self._container_id = await self.room.containers.run(
                        command="sleep infinity",
                        image=self.image,
                        mounts=self.mounts,
                        writable_root_fs=True,
                        env=self.env,
                    )

            container_id = self._container_id
            commands = self.commands
            logger.info(
                f"executing shell script in container {container_id} with timeout {timeout}: {commands}"
            )
            for line in commands:
                container_exec = None
                stdout_task: asyncio.Task[None] | None = None
                stderr_task: asyncio.Task[None] | None = None
                stdout = StreamOutputAccumulator(
                    context=context,
                    item_id=item_id,
                    source="stdout",
                    encoding=encoding,
                    max_length=self.max_output_length,
                    max_log_line_length=MAX_LOG_LINE_LENGTH,
                )
                stderr = StreamOutputAccumulator(
                    context=context,
                    item_id=item_id,
                    source="stderr",
                    encoding=encoding,
                    max_length=self.max_output_length,
                    max_log_line_length=MAX_LOG_LINE_LENGTH,
                )
                try:
                    # TODO: what if container start fails

                    container_exec = await self.room.containers.exec(
                        container_id=container_id,
                        command=["bash", "-c", line],
                        tty=False,
                    )

                    try:
                        stdout_task = asyncio.create_task(
                            collect_output_stream(
                                stream=container_exec.stdout(),
                                accumulator=stdout,
                            )
                        )
                        stderr_task = asyncio.create_task(
                            collect_output_stream(
                                stream=container_exec.stderr(),
                                accumulator=stderr,
                            )
                        )
                        async with asyncio.timeout(timeout):
                            exit_code = await container_exec.result
                            await _await_output_tasks(stdout_task, stderr_task)

                            return {
                                "outcome": {
                                    "type": "exit",
                                    "exit_code": exit_code,
                                },
                                "stdout": stdout.finish(),
                                "stderr": stderr.finish(),
                            }

                    except asyncio.TimeoutError:
                        logger.warning(f"The command timed out after {timeout}s")
                        if container_exec is not None:
                            await container_exec.kill()
                        if stdout_task is not None and stderr_task is not None:
                            await _await_output_tasks(stdout_task, stderr_task)

                        results.append(
                            {
                                "outcome": {"type": "timeout"},
                                "stdout": stdout.finish(),
                                "stderr": stderr.finish(),
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
                    break
        else:
            for line in self.commands:
                logger.info(f"executing command {line} with timeout: {timeout}s")

                # Spawn the process
                proc: asyncio.subprocess.Process | None = None
                stdout_task: asyncio.Task[None] | None = None
                stderr_task: asyncio.Task[None] | None = None
                stdout = StreamOutputAccumulator(
                    context=context,
                    item_id=item_id,
                    source="stdout",
                    encoding=encoding,
                    max_length=self.max_output_length,
                    max_log_line_length=MAX_LOG_LINE_LENGTH,
                )
                stderr = StreamOutputAccumulator(
                    context=context,
                    item_id=item_id,
                    source="stderr",
                    encoding=encoding,
                    max_length=self.max_output_length,
                    max_log_line_length=MAX_LOG_LINE_LENGTH,
                )
                try:
                    import shlex

                    proc = await asyncio.create_subprocess_shell(
                        shlex.join(["bash", "-c", line]),
                        cwd=self.working_dir or os.getcwd(),
                        env=merged_env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout_task = asyncio.create_task(
                        collect_output_stream(
                            stream=_stream_reader_chunks(proc.stdout),
                            accumulator=stdout,
                        )
                    )
                    stderr_task = asyncio.create_task(
                        collect_output_stream(
                            stream=_stream_reader_chunks(proc.stderr),
                            accumulator=stderr,
                        )
                    )
                    await asyncio.wait_for(proc.wait(), timeout=timeout)
                    await _await_output_tasks(stdout_task, stderr_task)
                except asyncio.TimeoutError:
                    logger.warning(f"The command timed out after {timeout}s")
                    if proc is not None:
                        proc.kill()  # send SIGKILL / TerminateProcess
                    if proc is not None:
                        await proc.wait()
                    if stdout_task is not None and stderr_task is not None:
                        await _await_output_tasks(stdout_task, stderr_task)

                    results.append(
                        {
                            "outcome": {"type": "timeout"},
                            "stdout": stdout.finish(),
                            "stderr": stderr.finish(),
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
                    break

                results.append(
                    {
                        "outcome": {
                            "type": "exit",
                            "exit_code": proc.returncode if proc is not None else 1,
                        },
                        "stdout": stdout.finish(),
                        "stderr": stderr.finish(),
                    }
                )

        return {"results": results}


async def get_script_tools(room: RoomClient):
    services = await room.services.list()

    st = []

    for service in services:
        if service.metadata.annotations is not None:
            type = service.metadata.annotations.get("meshagent.tool.type")
            commands_str = service.metadata.annotations.get("meshagent.tool.commands")
            tool_name = service.metadata.annotations.get(
                "meshagent.tool.name", service.metadata.name
            )
            description = service.metadata.annotations.get(
                "meshagent.tool.description", service.metadata.description
            )

            if type == "script" and tool_name is not None:
                if commands_str is not None:
                    commands = commands_str.split("\n")

                    st.append(
                        ScriptTool(
                            room=room,
                            name=tool_name,
                            description=description,
                            service_id=service.id,
                            commands=commands,
                        )
                    )

    return st
