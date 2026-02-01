from meshagent.api import RoomClient
from .tool import ToolContext, Tool
from .toolkit import Toolkit, ToolkitBuilder

from meshagent.api.specs.service import ContainerMountSpec, RoomStorageMountSpec
from typing import Literal, Optional
import os

import logging
import asyncio
from pydantic import BaseModel

logger = logging.getLogger("container_shell_tool")


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
        working_directory: Optional[str] = None,
        image: Optional[str] = "python:3.13",
        mounts: Optional[ContainerMountSpec] = DEFAULT_CONTAINER_MOUNT_SPEC,
        env: Optional[dict[str, str]] = None,
    ):
        super().__init__(name=name, type=ContainerShellToolConfig)

        self.working_directory = working_directory
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
                    working_directory=self.working_directory,
                    image=self.image,
                    mounts=self.mounts,
                    env=self.env,
                )
            ],
        )


class ContainerShellTool(Tool):
    def __init__(
        self,
        *,
        name: str = "container_shell",
        description: Optional[str] = None,
        title: Optional[str] = None,
        working_directory: Optional[str] = None,
        image: Optional[str] = "python:3.13",
        mounts: Optional[ContainerMountSpec] = DEFAULT_CONTAINER_MOUNT_SPEC,
        env: Optional[dict[str, str]] = None,
    ):
        self.working_directory = working_directory
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

        left = max_output_length

        def limit(s: str):
            nonlocal left
            if left is not None:
                s = s[0:left]
                left -= len(s)
                return s
            else:
                return s

        timeout = float(timeout_ms) / 1000.0 if timeout_ms else 20 * 1000.0

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
                if self.working_directory:
                    command_to_run = (
                        f"cd {shlex.quote(self.working_directory)} && {command}"
                    )
                exec = await context.room.containers.exec(
                    container_id=container_id,
                    command=shlex.join(["bash", "-lc", command_to_run]),
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
