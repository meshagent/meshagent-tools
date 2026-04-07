from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import shlex
from collections.abc import AsyncIterable
from dataclasses import dataclass
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from meshagent.api import RoomClient, RoomException, ToolContentSpec
from meshagent.api.specs.service import (
    ConfigMountSpec,
    ContainerMountSpec,
    FileStorageMountSpec,
    RoomStorageMountSpec,
)
from meshagent.tools.strict_schema import ensure_strict_json_schema

from ._shell_output import (
    DEFAULT_MAX_LOG_LINE_LENGTH,
    StreamOutputAccumulator,
    collect_output_stream,
)
from .tool import FunctionTool, ToolContext
from .toolkit import Toolkit, ToolkitBuilder

logger = logging.getLogger("container_shell_tool")

DEFAULT_MAX_OUTPUT_LENGTH = 50 * 1024
MAX_LOG_LINE_LENGTH = DEFAULT_MAX_LOG_LINE_LENGTH


DEFAULT_CONTAINER_MOUNT_SPEC = ContainerMountSpec(
    room=[RoomStorageMountSpec(path="/data")]
)


class _ContainerShellInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[str] = Field(min_length=1)
    max_output_length: int | None = None
    timeout_ms: int | None = None


class _ManagedContainerShellInput(_ContainerShellInput):
    container_id: str = Field(min_length=1)


class _ShellExitOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["exit"]
    exit_code: int


class _ShellTimeoutOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["timeout"]


_ShellCommandOutcome = Annotated[
    _ShellExitOutcome | _ShellTimeoutOutcome,
    Field(discriminator="type"),
]


class _ShellCommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: _ShellCommandOutcome
    stdout: str
    stderr: str


class _ShellExecutionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[_ShellCommandResult]


class ContainerEnvVar(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1)
    value: str


class _StartManagedContainerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: str | None = None
    mounts: ContainerMountSpec | None = None
    env: list[ContainerEnvVar] | None = None
    working_dir: str | None = None


class _ManagedContainerSelector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    container_id: str = Field(min_length=1)


class _ManagedContainerSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    container_id: str
    image: str
    working_dir: str | None = None
    mounts: ContainerMountSpec | None = None
    env: list[ContainerEnvVar] = Field(default_factory=list)


class _ListManagedContainersOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    containers: list[_ManagedContainerSummary]


class _StartManagedContainerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    container_id: str


class _StopManagedContainerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    container_id: str
    ok: bool = True


def _strict_model_schema(model_type: type[BaseModel]) -> dict:
    return ensure_strict_json_schema(model_type.model_json_schema())


def _json_output_spec(model_type: type[BaseModel]) -> ToolContentSpec:
    return ToolContentSpec(
        types=["json"],
        stream=False,
        schema=_strict_model_schema(model_type),
    )


def _item_id_from_context(context: ToolContext) -> str:
    caller_context = context.caller_context
    if caller_context is None:
        return ""

    item_id = caller_context.get("item_id")
    if isinstance(item_id, str):
        return item_id
    return ""


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


async def _await_output_tasks(*tasks: asyncio.Task[None] | None) -> None:
    pending = [task for task in tasks if task is not None]
    if len(pending) == 0:
        return

    results = await asyncio.gather(*pending, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception) and not isinstance(
            result, asyncio.CancelledError
        ):
            logger.debug("container shell output stream task failed", exc_info=result)


def _normalize_optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized == "":
        return None
    return normalized


def _merge_container_mounts(
    *,
    defaults: ContainerMountSpec | None,
    overrides: ContainerMountSpec | None,
) -> ContainerMountSpec | None:
    if defaults is None and overrides is None:
        return None
    if defaults is None:
        return overrides.model_copy(deep=True)
    if overrides is None:
        return defaults.model_copy(deep=True)

    room_mounts = [*(defaults.room or []), *(overrides.room or [])]
    project_mounts = [*(defaults.project or []), *(overrides.project or [])]
    image_mounts = [*(defaults.images or []), *(overrides.images or [])]
    file_mounts = [*(defaults.files or []), *(overrides.files or [])]
    empty_dir_mounts = [*(defaults.empty_dirs or []), *(overrides.empty_dirs or [])]
    config_mounts = [*(defaults.configs or []), *(overrides.configs or [])]

    return ContainerMountSpec(
        room=room_mounts or None,
        project=project_mounts or None,
        images=image_mounts or None,
        files=file_mounts or None,
        empty_dirs=empty_dir_mounts or None,
        configs=config_mounts or None,
    )


def _runtime_config_source(*, env_var_name: str) -> tuple[str, str] | None:
    source_path = _normalize_optional_string(os.getenv(env_var_name))
    if source_path is None:
        return None

    try:
        with open(source_path, "r", encoding="utf-8") as source_file:
            return source_path, source_file.read()
    except OSError as exc:
        raise RoomException(
            f"unable to read {env_var_name} from {source_path}: {exc}"
        ) from exc


def _config_mount_target_path(*, mount: ConfigMountSpec, filename: str) -> str:
    base_path = mount.path.rstrip("/") or "/"
    return posixpath.join(base_path, filename)


def _expand_runtime_config_mounts(
    *,
    mounts: ContainerMountSpec | None,
) -> tuple[ContainerMountSpec | None, dict[str, str]]:
    if mounts is None:
        return None, {}

    expanded_mounts = mounts.model_copy(deep=True)
    config_mounts = expanded_mounts.configs or []
    if len(config_mounts) == 0:
        return expanded_mounts, {}

    spec_source = _runtime_config_source(env_var_name="MESHAGENT_SPEC_PATH")
    members_source = _runtime_config_source(env_var_name="MESHAGENT_MEMBERS_PATH")
    if spec_source is None and members_source is None:
        raise RoomException(
            "container config mounts require MESHAGENT_SPEC_PATH or "
            "MESHAGENT_MEMBERS_PATH in the current environment"
        )

    file_mounts = list(expanded_mounts.files or [])
    file_targets = {file_mount.path for file_mount in file_mounts}
    env_updates: dict[str, str] = {}

    for index, config_mount in enumerate(config_mounts):
        if spec_source is not None:
            spec_target = _config_mount_target_path(
                mount=config_mount,
                filename="spec.json",
            )
            if spec_target not in file_targets:
                file_mounts.append(
                    FileStorageMountSpec(
                        path=spec_target,
                        text=spec_source[1],
                        read_only=True,
                    )
                )
                file_targets.add(spec_target)
            if index == 0:
                env_updates["MESHAGENT_SPEC_PATH"] = spec_target

        if members_source is not None:
            members_target = _config_mount_target_path(
                mount=config_mount,
                filename="members.json",
            )
            if members_target not in file_targets:
                file_mounts.append(
                    FileStorageMountSpec(
                        path=members_target,
                        text=members_source[1],
                        read_only=True,
                    )
                )
                file_targets.add(members_target)
            if index == 0:
                env_updates["MESHAGENT_MEMBERS_PATH"] = members_target

    expanded_mounts.files = file_mounts or None
    expanded_mounts.configs = None
    return expanded_mounts, env_updates


def _container_env_dict(
    *,
    defaults: dict[str, str] | None,
    entries: list[ContainerEnvVar] | None,
) -> dict[str, str]:
    result: dict[str, str] = {}
    if defaults is not None:
        for key, value in defaults.items():
            result[key] = value
    if entries is not None:
        for entry in entries:
            result[entry.key] = entry.value

    return result


def _container_env_entries(env: dict[str, str]) -> list[ContainerEnvVar]:
    return [
        ContainerEnvVar(key=key, value=value)
        for key, value in sorted(env.items(), key=lambda item: item[0])
    ]


def _shell_exit_result(
    *,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> _ShellCommandResult:
    return _ShellCommandResult(
        outcome=_ShellExitOutcome(type="exit", exit_code=exit_code),
        stdout=stdout,
        stderr=stderr,
    )


def _shell_timeout_result(
    *,
    stdout: str,
    stderr: str,
) -> _ShellCommandResult:
    return _ShellCommandResult(
        outcome=_ShellTimeoutOutcome(type="timeout"),
        stdout=stdout,
        stderr=stderr,
    )


def _shell_error_result(*, error: Exception) -> _ShellCommandResult:
    return _shell_exit_result(exit_code=1, stdout="", stderr=str(error))


def _shell_execution_output(
    *,
    results: list[_ShellCommandResult],
) -> dict[str, list[dict[str, object]]]:
    payload = _ShellExecutionOutput(results=results).model_dump(mode="json")
    return payload


@dataclass(frozen=True, slots=True)
class _ManagedContainerRecord:
    container_id: str
    image: str
    working_dir: str | None
    mounts: ContainerMountSpec | None
    env: dict[str, str]


class _ManagedContainerManager:
    def __init__(
        self,
        *,
        default_image: str | None = "meshagent/python:default",
        default_mounts: ContainerMountSpec | None = DEFAULT_CONTAINER_MOUNT_SPEC,
        default_env: dict[str, str] | None = None,
        default_working_dir: str | None = None,
    ) -> None:
        self.default_image = default_image
        self.default_mounts = (
            default_mounts.model_copy(deep=True) if default_mounts is not None else None
        )
        self.default_env = dict(default_env) if default_env is not None else {}
        self.default_working_dir = default_working_dir
        self._containers: dict[str, _ManagedContainerRecord] = {}
        self._lock = asyncio.Lock()

    async def list_containers(self) -> list[_ManagedContainerRecord]:
        async with self._lock:
            return [
                self._containers[container_id]
                for container_id in sorted(self._containers.keys())
            ]

    async def require_container(self, *, container_id: str) -> _ManagedContainerRecord:
        async with self._lock:
            record = self._containers.get(container_id)
        if record is None:
            raise RoomException(
                f"container is not managed by this toolkit: {container_id}"
            )
        return record

    async def start_container(
        self,
        *,
        room: RoomClient,
        image: str | None,
        mounts: ContainerMountSpec | None,
        env: list[ContainerEnvVar] | None,
        working_dir: str | None,
    ) -> str:
        resolved_image = _normalize_optional_string(
            image
        ) or _normalize_optional_string(self.default_image)
        if resolved_image is None:
            raise RoomException("start_container requires an image")

        resolved_mounts = _merge_container_mounts(
            defaults=self.default_mounts,
            overrides=mounts,
        )
        resolved_env = _container_env_dict(defaults=self.default_env, entries=env)
        run_mounts, runtime_config_env = _expand_runtime_config_mounts(
            mounts=resolved_mounts
        )
        for key, value in runtime_config_env.items():
            resolved_env.setdefault(key, value)
        resolved_working_dir = _normalize_optional_string(
            working_dir
        ) or _normalize_optional_string(self.default_working_dir)

        container_id = await room.containers.run(
            command="sleep infinity",
            image=resolved_image,
            working_dir=resolved_working_dir,
            mounts=run_mounts,
            writable_root_fs=True,
            env=resolved_env,
        )

        record = _ManagedContainerRecord(
            container_id=container_id,
            image=resolved_image,
            working_dir=resolved_working_dir,
            mounts=resolved_mounts.model_copy(deep=True)
            if resolved_mounts is not None
            else None,
            env=dict(resolved_env),
        )
        async with self._lock:
            self._containers[container_id] = record
        return container_id

    async def stop_container(self, *, room: RoomClient, container_id: str) -> None:
        await self.require_container(container_id=container_id)

        try:
            await room.containers.stop(container_id=container_id, force=True)
        except Exception:
            logger.info(
                "failed to stop managed container %s before delete",
                container_id,
                exc_info=True,
            )

        await room.containers.delete(container_id=container_id)
        async with self._lock:
            self._containers.pop(container_id, None)

    async def stop_all(self, *, room: RoomClient) -> None:
        for record in await self.list_containers():
            try:
                await self.stop_container(room=room, container_id=record.container_id)
            except Exception:
                logger.warning(
                    "failed to clean up managed container %s",
                    record.container_id,
                    exc_info=True,
                )


class BaseContainerShellTool(FunctionTool):
    def __init__(
        self,
        *,
        name: str,
        input_model: type[BaseModel],
        description: Optional[str] = None,
        title: Optional[str] = None,
        working_dir: Optional[str] = None,
    ) -> None:
        self.working_dir = working_dir
        self._input_model = input_model

        super().__init__(
            name=name,
            description=description
            or "execute shell commands in a container and return the result",
            title=title,
            input_schema=_strict_model_schema(input_model),
            output_spec=_json_output_spec(_ShellExecutionOutput),
        )

    async def get_container_id(
        self,
        context: ToolContext,
        *,
        container_id: str | None = None,
    ) -> str:
        raise NotImplementedError

    async def get_working_dir(
        self,
        context: ToolContext,
        *,
        container_id: str,
    ) -> str | None:
        del context
        del container_id
        return self.working_dir

    async def execute(
        self,
        context: ToolContext,
        *,
        commands: list[str],
        max_output_length: int | None = None,
        timeout_ms: int | None = None,
        container_id: str | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        payload: dict[str, object] = {
            "commands": commands,
            "max_output_length": max_output_length,
            "timeout_ms": timeout_ms,
        }
        if issubclass(self._input_model, _ManagedContainerShellInput):
            payload["container_id"] = container_id

        parsed = self._input_model.model_validate(payload)

        parsed_container_id: str | None = None
        if isinstance(parsed, _ManagedContainerShellInput):
            parsed_container_id = parsed.container_id

        effective_max_output_length = (
            parsed.max_output_length
            if parsed.max_output_length is not None
            else DEFAULT_MAX_OUTPUT_LENGTH
        )
        if effective_max_output_length <= 0:
            raise ValueError("max_output_length must be greater than 0")

        timeout = float(parsed.timeout_ms) / 1000.0 if parsed.timeout_ms else 60.0
        active_container_id = await self.get_container_id(
            context,
            container_id=parsed_container_id,
        )
        command_working_dir = await self.get_working_dir(
            context,
            container_id=active_container_id,
        )

        results: list[_ShellCommandResult] = []
        encoding = os.device_encoding(1) or "utf-8"
        item_id = _item_id_from_context(context)

        try:
            logger.info(
                "executing shell commands in container %s with timeout %s: %s",
                active_container_id,
                timeout,
                parsed.commands,
            )

            for command in parsed.commands:
                command_to_run = command
                if command_working_dir is not None:
                    command_to_run = (
                        f"cd {shlex.quote(command_working_dir)} && {command}"
                    )

                container_exec = await context.room.containers.exec(
                    container_id=active_container_id,
                    command=["bash", "-lc", command_to_run],
                    tty=False,
                )

                stdout = StreamOutputAccumulator(
                    context=context,
                    item_id=item_id,
                    source="stdout",
                    encoding=encoding,
                    max_length=effective_max_output_length,
                    max_log_line_length=MAX_LOG_LINE_LENGTH,
                )
                stderr = StreamOutputAccumulator(
                    context=context,
                    item_id=item_id,
                    source="stderr",
                    encoding=encoding,
                    max_length=effective_max_output_length,
                    max_log_line_length=MAX_LOG_LINE_LENGTH,
                )
                stdout_task: asyncio.Task[None] | None = None
                stderr_task: asyncio.Task[None] | None = None

                try:
                    async with asyncio.timeout(timeout):
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
                        await _await_output_tasks(stdout_task, stderr_task)

                        exit_code = await container_exec.result
                        results.append(
                            _shell_exit_result(
                                exit_code=exit_code,
                                stdout=stdout.finish(),
                                stderr=stderr.finish(),
                            )
                        )
                except asyncio.TimeoutError:
                    logger.warning("the command timed out after %ss", timeout)
                    await container_exec.kill()
                    if stdout_task is not None:
                        stdout_task.cancel()
                    if stderr_task is not None:
                        stderr_task.cancel()
                    await _await_output_tasks(stdout_task, stderr_task)

                    results.append(
                        _shell_timeout_result(
                            stdout=stdout.finish(),
                            stderr=stderr.finish(),
                        )
                    )
                    break
                except Exception as exc:
                    if stdout_task is not None:
                        stdout_task.cancel()
                    if stderr_task is not None:
                        stderr_task.cancel()
                    await _await_output_tasks(stdout_task, stderr_task)

                    stdout.finish()
                    stderr.finish()
                    results.append(_shell_error_result(error=exc))
                    break
        except Exception as exc:
            results.append(_shell_error_result(error=exc))

        return _shell_execution_output(results=results)


class ContainerShellToolConfig(BaseModel):
    name: Literal["container_shell"] = "container_shell"


class ContainerShellToolkitBuilder(ToolkitBuilder):
    def __init__(
        self,
        *,
        name: str = "container_shell",
        working_dir: Optional[str] = None,
        image: Optional[str] = "meshagent/python:default",
        mounts: Optional[ContainerMountSpec] = DEFAULT_CONTAINER_MOUNT_SPEC,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(name=name, type=ContainerShellToolConfig)
        self.working_dir = working_dir
        self.image = image
        self.mounts = mounts
        self.env = env

    async def make(
        self,
        *,
        room: RoomClient,
        model: str,
        config: ContainerShellToolConfig,
    ) -> Toolkit:
        del room
        del model
        del config
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


class ContainerShellTool(BaseContainerShellTool):
    def __init__(
        self,
        *,
        name: str = "container_shell",
        description: Optional[str] = None,
        title: Optional[str] = None,
        working_dir: Optional[str] = None,
        image: Optional[str] = "meshagent/python:default",
        mounts: Optional[ContainerMountSpec] = DEFAULT_CONTAINER_MOUNT_SPEC,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self.image = image
        self.mounts = mounts
        self.env = env
        self._container_id: str | None = None

        super().__init__(
            name=name,
            input_model=_ContainerShellInput,
            description=description,
            title=title,
            working_dir=working_dir,
        )

    async def get_container_id(
        self,
        context: ToolContext,
        *,
        container_id: str | None = None,
    ) -> str:
        del container_id
        if self.image is None:
            raise RoomException("container_shell requires an image")

        is_running = False
        if self._container_id is not None:
            for container in await context.room.containers.list():
                if container.id == self._container_id:
                    is_running = True
                    break

        if not is_running:
            run_mounts, runtime_config_env = _expand_runtime_config_mounts(
                mounts=self.mounts
            )
            env = dict(self.env or {})
            for key, value in runtime_config_env.items():
                env.setdefault(key, value)
            self._container_id = await context.room.containers.run(
                command="sleep infinity",
                image=self.image,
                mounts=run_mounts,
                writable_root_fs=True,
                env=env or None,
            )

        return self._container_id

    async def stop(self, *, room: RoomClient) -> None:
        container_id = self._container_id
        if container_id is None:
            return

        self._container_id = None

        try:
            await room.containers.stop(container_id=container_id, force=True)
        except Exception as ex:
            logger.warning(
                "unable to stop cached shell container %s", container_id, exc_info=ex
            )

        try:
            await room.containers.delete(container_id=container_id)
        except Exception as ex:
            logger.warning(
                "unable to delete cached shell container %s",
                container_id,
                exc_info=ex,
            )


class ProcessShellTool(FunctionTool):
    def __init__(
        self,
        *,
        name: str = "process_shell",
        description: Optional[str] = None,
        title: Optional[str] = None,
        working_dir: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self.working_dir = working_dir
        self.env = env

        super().__init__(
            name=name,
            description=description
            or "execute shell commands in a local process and return the result",
            title=title,
            input_schema=_strict_model_schema(_ContainerShellInput),
            output_spec=_json_output_spec(_ShellExecutionOutput),
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        commands: list[str],
        max_output_length: int | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        parsed = _ContainerShellInput.model_validate(
            {
                "commands": commands,
                "max_output_length": max_output_length,
                "timeout_ms": timeout_ms,
            }
        )

        effective_max_output_length = (
            parsed.max_output_length
            if parsed.max_output_length is not None
            else DEFAULT_MAX_OUTPUT_LENGTH
        )
        if effective_max_output_length <= 0:
            raise ValueError("max_output_length must be greater than 0")

        timeout = float(parsed.timeout_ms) / 1000.0 if parsed.timeout_ms else 60.0
        merged_env = {**os.environ}
        if self.env is not None:
            merged_env.update(self.env)

        results: list[_ShellCommandResult] = []
        encoding = os.device_encoding(1) or "utf-8"
        item_id = _item_id_from_context(context)

        for command in parsed.commands:
            logger.info(
                "executing shell commands in local process with timeout %s: %s",
                timeout,
                command,
            )

            proc: asyncio.subprocess.Process | None = None
            stdout_task: asyncio.Task[None] | None = None
            stderr_task: asyncio.Task[None] | None = None
            stdout = StreamOutputAccumulator(
                context=context,
                item_id=item_id,
                source="stdout",
                encoding=encoding,
                max_length=effective_max_output_length,
                max_log_line_length=MAX_LOG_LINE_LENGTH,
            )
            stderr = StreamOutputAccumulator(
                context=context,
                item_id=item_id,
                source="stderr",
                encoding=encoding,
                max_length=effective_max_output_length,
                max_log_line_length=MAX_LOG_LINE_LENGTH,
            )

            try:
                proc = await asyncio.create_subprocess_shell(
                    shlex.join(["bash", "-c", command]),
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
                logger.warning("the command timed out after %ss", timeout)
                if proc is not None:
                    proc.kill()
                    await proc.wait()
                await _await_output_tasks(stdout_task, stderr_task)

                results.append(
                    _shell_timeout_result(
                        stdout=stdout.finish(),
                        stderr=stderr.finish(),
                    )
                )
                break
            except Exception as exc:
                if proc is not None and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                if stdout_task is not None:
                    stdout_task.cancel()
                if stderr_task is not None:
                    stderr_task.cancel()
                await _await_output_tasks(stdout_task, stderr_task)

                stdout.finish()
                stderr.finish()
                results.append(_shell_error_result(error=exc))
                break

            results.append(
                _shell_exit_result(
                    exit_code=proc.returncode if proc is not None else 1,
                    stdout=stdout.finish(),
                    stderr=stderr.finish(),
                )
            )

        return _shell_execution_output(results=results)


class ContainerToolkitConfig(BaseModel):
    name: Literal["container"] = "container"


class ContainerToolkitBuilder(ToolkitBuilder):
    def __init__(
        self,
        *,
        name: str = "container",
        working_dir: Optional[str] = None,
        image: Optional[str] = "meshagent/python:default",
        mounts: Optional[ContainerMountSpec] = DEFAULT_CONTAINER_MOUNT_SPEC,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(name=name, type=ContainerToolkitConfig)
        self.working_dir = working_dir
        self.image = image
        self.mounts = mounts
        self.env = env

    async def make(
        self,
        *,
        room: RoomClient,
        model: str,
        config: ContainerToolkitConfig,
    ) -> Toolkit:
        del room
        del model
        del config
        return ContainerToolkit(
            name=self.name,
            working_dir=self.working_dir,
            default_image=self.image,
            mounts=self.mounts,
            env=self.env,
        )


class _ListManagedContainersTool(FunctionTool):
    def __init__(self, *, manager: _ManagedContainerManager) -> None:
        self._manager = manager
        super().__init__(
            name="list_managed_containers",
            title="list managed containers",
            description="list the containers currently managed by this toolkit",
            input_schema={
                "type": "object",
                "required": [],
                "additionalProperties": False,
                "properties": {},
            },
            output_spec=_json_output_spec(_ListManagedContainersOutput),
        )

    async def execute(self, context: ToolContext) -> dict[str, list[dict[str, object]]]:
        del context
        containers = [
            _ManagedContainerSummary(
                container_id=record.container_id,
                image=record.image,
                working_dir=record.working_dir,
                mounts=record.mounts.model_copy(deep=True)
                if record.mounts is not None
                else None,
                env=_container_env_entries(record.env),
            ).model_dump(mode="json")
            for record in await self._manager.list_containers()
        ]
        return {"containers": containers}


class _StartManagedContainerTool(FunctionTool):
    def __init__(self, *, manager: _ManagedContainerManager) -> None:
        self._manager = manager
        super().__init__(
            name="start_container",
            title="start container",
            description="start a new shell container and add it to this toolkit",
            input_schema=_strict_model_schema(_StartManagedContainerInput),
            output_spec=_json_output_spec(_StartManagedContainerOutput),
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        image: str | None = None,
        mounts: ContainerMountSpec | None = None,
        env: list[ContainerEnvVar] | None = None,
        working_dir: str | None = None,
    ) -> dict[str, str]:
        parsed = _StartManagedContainerInput.model_validate(
            {
                "image": image,
                "mounts": mounts,
                "env": env,
                "working_dir": working_dir,
            }
        )
        container_id = await self._manager.start_container(
            room=context.room,
            image=parsed.image,
            mounts=parsed.mounts,
            env=parsed.env,
            working_dir=parsed.working_dir,
        )
        return {"container_id": container_id}


class _StopManagedContainerTool(FunctionTool):
    def __init__(self, *, manager: _ManagedContainerManager) -> None:
        self._manager = manager
        super().__init__(
            name="stop_managed_container",
            title="stop managed container",
            description="stop and delete a container managed by this toolkit",
            input_schema=_strict_model_schema(_ManagedContainerSelector),
            output_spec=_json_output_spec(_StopManagedContainerOutput),
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        container_id: str,
    ) -> dict[str, object]:
        parsed = _ManagedContainerSelector.model_validate(
            {"container_id": container_id}
        )
        await self._manager.stop_container(
            room=context.room,
            container_id=parsed.container_id,
        )
        return {"container_id": parsed.container_id, "ok": True}


class _RunInContainerTool(BaseContainerShellTool):
    def __init__(self, *, manager: _ManagedContainerManager) -> None:
        self._manager = manager
        super().__init__(
            name="run_in_container",
            input_model=_ManagedContainerShellInput,
            title="run in container",
            description=(
                "execute shell commands in a container managed by this toolkit"
            ),
        )

    async def get_container_id(
        self,
        context: ToolContext,
        *,
        container_id: str | None = None,
    ) -> str:
        del context
        if container_id is None:
            raise RoomException("container_id is required")
        record = await self._manager.require_container(container_id=container_id)
        return record.container_id

    async def get_working_dir(
        self,
        context: ToolContext,
        *,
        container_id: str,
    ) -> str | None:
        del context
        record = await self._manager.require_container(container_id=container_id)
        return record.working_dir


class ContainerToolkit(Toolkit):
    def __init__(
        self,
        *,
        name: str = "container",
        working_dir: Optional[str] = None,
        default_image: Optional[str] = "meshagent/python:default",
        mounts: Optional[ContainerMountSpec] = DEFAULT_CONTAINER_MOUNT_SPEC,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self.default_image = default_image
        self.default_mounts = (
            mounts.model_copy(deep=True) if mounts is not None else None
        )
        self.default_env = dict(env) if env is not None else {}
        self.default_working_dir = working_dir
        self._manager = _ManagedContainerManager(
            default_image=default_image,
            default_mounts=mounts,
            default_env=env,
            default_working_dir=working_dir,
        )

        super().__init__(
            name=name,
            tools=[
                _ListManagedContainersTool(manager=self._manager),
                _StartManagedContainerTool(manager=self._manager),
                _StopManagedContainerTool(manager=self._manager),
                _RunInContainerTool(manager=self._manager),
            ],
        )

    async def stop_all(self, *, room: RoomClient) -> None:
        await self._manager.stop_all(room=room)
