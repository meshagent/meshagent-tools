from meshagent.api import RoomClient
from meshagent.tools import Toolkit, ToolContext, Tool

from meshagent.agents.adapter import (
    ToolkitBuilder,
)

from meshagent.api.specs.service import ContainerMountSpec, RoomStorageMountSpec
from typing import Literal
import os
from typing import Optional

import logging
import asyncio
from pydantic import BaseModel

logger = logging.getLogger("script_tool")


DEFAULT_CONTAINER_MOUNT_SPEC = ContainerMountSpec(
    room=[RoomStorageMountSpec(path="/data")]
)


class ScriptToolConfig(BaseModel):
    name: Literal["script"] = "script"
    service_id: Optional[str] = None
    commands: list[str] = None
    description: Optional[str] = None
    title: Optional[str] = None
    tool_name: str


class ScriptToolkitBuilder(ToolkitBuilder):
    def __init__(
        self,
        *,
        name: str = "script",
        commands: Optional[list[str]] = None,
        working_directory: Optional[str] = None,
        image: Optional[str] = "python:3.13",
        mounts: Optional[ContainerMountSpec] = DEFAULT_CONTAINER_MOUNT_SPEC,
        input_schema: Optional[dict] = None,
    ):
        super().__init__(name=name, type=ScriptToolConfig)

        self.working_directory = working_directory
        self.image = image
        self.mounts = mounts
        self.commands = commands
        self.input_schema = input_schema

    async def make(self, *, room: RoomClient, model: str, config: ScriptToolConfig):
        return Toolkit(
            name=self.name,
            tools=[
                ScriptTool(
                    name=config.tool_name,
                    description=config.description,
                    title=config.title,
                    service_id=config.service_id,
                    working_directory=self.working_directory,
                    image=self.image,
                    commands=self.commands or config.commands,
                    mounts=self.mounts,
                    input_schema=self.input_schema,
                )
            ],
        )


class ScriptTool(Tool):
    def __init__(
        self,
        *,
        name: str,
        commands: list[str],
        description: Optional[str] = None,
        title: Optional[str] = None,
        service_id: Optional[str] = None,
        working_directory: Optional[str] = None,
        image: Optional[str] = "python:3.13",
        mounts: Optional[ContainerMountSpec] = DEFAULT_CONTAINER_MOUNT_SPEC,
        env: Optional[dict[str, str]] = None,
        input_schema: Optional[dict] = None,
        max_output_length: int = 32000,
        timeout_ms: int = 30 * 60 * 1000,
    ):
        self.service_id = service_id
        self.working_directory = working_directory
        self.image = image
        self.mounts = mounts
        self._container_id = None
        self.env = env
        self.max_output_length = max_output_length
        self.timeout_ms = timeout_ms
        self.service_id = service_id
        self.commands = commands

        super().__init__(
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

        left = self.max_output_length

        def limit(s: str):
            nonlocal left
            if left is not None:
                s = s[0:left]
                left -= len(s)
                return s
            else:
                return s

        timeout = float(self.timeout_ms) / 1000.0 if self.timeout_ms else 20 * 1000.0

        if self.image is not None or self.service_id is not None:
            running = False

            if self._container_id:
                # make sure container is still running

                for c in await context.room.containers.list():
                    if c.id == self._container_id or (
                        self.service_id is not None and c.service_id == self.service_id
                    ):
                        running = True

            if not running:
                if self.service_id is not None:
                    env = {}

                    for k, v in kwargs.items():
                        env[k.upper()] = v

                        logger.info(
                            f"executing shell script in container with env {env}"
                        )

                    self._container_id = await context.room.containers.run_service(
                        service_id=self.service_id,
                        env=env,
                    )

                else:
                    self._container_id = await context.room.containers.run(
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
            import shlex

            for line in commands:
                try:
                    # TODO: what if container start fails

                    exec = await context.room.containers.exec(
                        container_id=container_id,
                        command=shlex.join(["bash", "-c", line]),
                        tty=False,
                    )

                    stdout = bytearray()
                    stderr = bytearray()

                    try:
                        async with asyncio.timeout(timeout):
                            async for se in exec.stderr():
                                stderr.extend(se)

                            async for so in exec.stdout():
                                stdout.extend(so)

                            exit_code = await exec.result

                            return {
                                "outcome": {
                                    "type": "exit",
                                    "exit_code": exit_code,
                                },
                                "stdout": stdout.decode(),
                                "stderr": stderr.decode(),
                            }

                    except asyncio.TimeoutError:
                        logger.info(f"The command timed out after {timeout}s")
                        await exec.kill()

                        results.append(
                            {
                                "outcome": {"type": "timeout"},
                                "stdout": limit(
                                    stdout.decode(encoding, errors="replace")
                                ),
                                "stderr": limit(
                                    stderr.decode(encoding, errors="replace")
                                ),
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
                try:
                    import shlex

                    proc = await asyncio.create_subprocess_shell(
                        shlex.join(["bash", "-c", line]),
                        cwd=self.working_directory or os.getcwd(),
                        env=merged_env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )

                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    logger.info(f"The command timed out after {timeout}s")
                    proc.kill()  # send SIGKILL / TerminateProcess

                    stdout, stderr = await proc.communicate()

                    results.append(
                        {
                            "outcome": {"type": "timeout"},
                            "stdout": limit(stdout.decode(encoding, errors="replace")),
                            "stderr": limit(stderr.decode(encoding, errors="replace")),
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
                            "exit_code": proc.returncode,
                        },
                        "stdout": limit(stdout.decode(encoding, errors="replace")),
                        "stderr": limit(stderr.decode(encoding, errors="replace")),
                    }
                )

        return {"results": results}


async def get_script_tools(room: RoomClient):
    services = await room.services.list()

    st = []

    for service in services:
        if service.metadata.annotations is not None:
            print("X")
            type = service.metadata.annotations.get("meshagent.tool.type")
            print(type)
            commands_str = service.metadata.annotations.get("meshagent.tool.commands")
            print(commands_str)
            tool_name = service.metadata.annotations.get(
                "meshagent.tool.name", service.metadata.name
            )
            description = service.metadata.annotations.get(
                "meshagent.tool.description", service.metadata.description
            )

            if type == "script" and tool_name is not None:
                print("FOUND")
                if commands_str is not None:
                    commands = commands_str.split("\n")

                    st.append(
                        ScriptTool(
                            name=tool_name,
                            description=description,
                            service_id=service.id,
                            commands=commands,
                        )
                    )

    return st
