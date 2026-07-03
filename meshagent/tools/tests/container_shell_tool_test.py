import asyncio

import pytest

import meshagent.tools.container_shell as container_shell_module
from meshagent.api.specs.service import ConfigMountSpec, ContainerMountSpec
from meshagent.tools import (
    ContainerShellTool,
    ContainerToolkit,
    JsonContent,
    ProcessShellTool,
    ToolContext,
)


class _FakeContainer:
    def __init__(self, container_id: str) -> None:
        self.id = container_id


class _FakeExec:
    def __init__(
        self,
        *,
        stdout_chunks: list[bytes],
        stderr_chunks: list[bytes],
        exit_code: int | None = 0,
    ) -> None:
        self._stdout_chunks = stdout_chunks
        self._stderr_chunks = stderr_chunks
        loop = asyncio.get_running_loop()
        self.result = loop.create_future()
        if exit_code is not None:
            self.result.set_result(exit_code)
        self.killed = False

    async def stdout(self):
        for chunk in self._stdout_chunks:
            await asyncio.sleep(0)
            yield chunk

    async def stderr(self):
        for chunk in self._stderr_chunks:
            await asyncio.sleep(0)
            yield chunk

    async def kill(self) -> None:
        self.killed = True


class _FakeContainers:
    def __init__(self) -> None:
        self._running: list[_FakeContainer] = []
        self.run_calls: list[dict[str, object]] = []
        self.exec_commands: list[list[str]] = []
        self.exec_calls: list[dict[str, object]] = []
        self.stop_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []
        self._next_container_id = 1
        self.next_exec = _FakeExec(
            stdout_chunks=[b"line 1\npart", b"ial"],
            stderr_chunks=[b"warn 1\n"],
        )

    async def list(self, all: bool | None = None) -> list[_FakeContainer]:
        del all
        return [*self._running]

    async def run(self, **kwargs) -> str:
        self.run_calls.append(kwargs)
        container = _FakeContainer(f"container-{self._next_container_id}")
        self._next_container_id += 1
        self._running.append(container)
        return container.id

    async def exec(self, *, container_id: str, command, tty: bool):
        self.exec_commands.append(command)
        self.exec_calls.append(
            {
                "container_id": container_id,
                "command": command,
                "tty": tty,
            }
        )
        return self.next_exec

    async def stop(self, *, container_id: str, force: bool = False) -> None:
        self.stop_calls.append({"container_id": container_id, "force": force})

    async def delete(self, *, container_id: str) -> None:
        self.delete_calls.append({"container_id": container_id})
        self._running = [
            container for container in self._running if container.id != container_id
        ]


class _FakeRoom:
    def __init__(self) -> None:
        self.containers = _FakeContainers()


def test_shell_execution_output_omits_exit_code_for_timeout() -> None:
    assert container_shell_module._shell_execution_output(
        results=[
            container_shell_module._shell_timeout_result(
                stdout="",
                stderr="",
            )
        ]
    ) == {
        "results": [
            {
                "outcome": {"type": "timeout"},
                "stdout": "",
                "stderr": "",
            }
        ]
    }


def test_merge_container_mounts_preserves_config_mounts() -> None:
    merged = container_shell_module._merge_container_mounts(
        defaults=ContainerMountSpec(configs=[ConfigMountSpec(path="/default-config")]),
        overrides=ContainerMountSpec(
            configs=[ConfigMountSpec(path="/override-config")]
        ),
    )

    assert merged is not None
    assert merged.model_dump(mode="json") == {
        "room": None,
        "images": None,
        "files": None,
        "empty_dirs": None,
        "configs": [
            {"path": "/default-config"},
            {"path": "/override-config"},
        ],
    }


@pytest.mark.asyncio
async def test_container_shell_validation_errors_are_source_neutral() -> None:
    tool = ProcessShellTool()
    context = ToolContext(caller=None)

    with pytest.raises(ValueError, match="_ContainerShellInput: commands is required"):
        container_shell_module._validate_container_shell_input(  # type: ignore[attr-defined]
            container_shell_module._ContainerShellInput,  # type: ignore[attr-defined]
            {},
        )

    with pytest.raises(
        ValueError, match="_ContainerShellInput: commands must be a list"
    ):
        await tool.execute(context, commands=None)  # type: ignore[arg-type]

    with pytest.raises(
        ValueError,
        match="_ContainerShellInput: commands must contain at least one command",
    ):
        await tool.execute(context, commands=[])

    with pytest.raises(
        ValueError,
        match="_ContainerShellInput: commands.0 must be a string",
    ):
        await tool.execute(context, commands=[1])  # type: ignore[list-item]

    with pytest.raises(
        ValueError,
        match="_ContainerShellInput: timeout_ms must be an integer",
    ):
        await tool.execute(context, commands=["echo"], timeout_ms=1.2)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_managed_container_validation_errors_are_source_neutral() -> None:
    room = _FakeRoom()
    toolkit = ContainerToolkit(room=room)  # type: ignore[arg-type]
    context = ToolContext(caller=None)

    start_tool = next(tool for tool in toolkit.tools if tool.name == "start_container")
    with pytest.raises(
        ValueError,
        match="_StartManagedContainerInput: env.0.key must be non-empty",
    ):
        await start_tool.execute(
            context,
            env=[{"key": "", "value": "v"}],  # type: ignore[list-item]
        )

    stop_tool = next(
        tool for tool in toolkit.tools if tool.name == "stop_managed_container"
    )
    with pytest.raises(
        ValueError,
        match="_ManagedContainerSelector: container_id must be non-empty",
    ):
        await stop_tool.execute(context, container_id="")

    run_tool = next(tool for tool in toolkit.tools if tool.name == "run_in_container")
    with pytest.raises(
        ValueError,
        match="_ManagedContainerShellInput: container_id must be a string",
    ):
        await run_tool.execute(context, commands=["echo hi"], container_id=None)


@pytest.mark.asyncio
async def test_container_shell_tool_emits_live_output_events() -> None:
    room = _FakeRoom()
    emitted: list[dict] = []
    tool = ContainerShellTool(room=room, working_dir="/workspace")

    result = await tool.execute(
        context=ToolContext(
            caller=object(),
            event_handler=lambda event: emitted.append({**event, "item_id": "tool-1"}),
        ),
        commands=["printf 'hello\\n'"],
    )

    assert room.containers.exec_commands == [
        ["bash", "-lc", "cd /workspace && printf 'hello\\n'"]
    ]
    assert result == {
        "results": [
            {
                "outcome": {"type": "exit", "exit_code": 0},
                "stdout": "line 1\npartial",
                "stderr": "warn 1\n",
            }
        ]
    }
    assert len(emitted) == 3
    assert {
        tuple((line["source"], line["text"]) for line in event["lines"])
        for event in emitted
    } == {
        (("stdout", "line 1"),),
        (("stdout", "partial"),),
        (("stderr", "warn 1"),),
    }
    for event in emitted:
        assert event["type"] == "meshagent.handler.output"
        assert event["item_id"] == "tool-1"


@pytest.mark.asyncio
async def test_container_shell_tool_config_mounts_expand_runtime_files(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text('{"name":"assistant"}')
    members_path = tmp_path / "members.json"
    members_path.write_text('{"members":[]}')
    monkeypatch.setenv("MESHAGENT_SPEC_PATH", str(spec_path))
    monkeypatch.setenv("MESHAGENT_MEMBERS_PATH", str(members_path))

    room = _FakeRoom()
    tool = ContainerShellTool(
        room=room,
        image="busybox:latest",
        mounts=ContainerMountSpec(configs=[ConfigMountSpec(path="/var/run/meshagent")]),
    )

    await tool.execute(
        context=ToolContext(
            caller=object(),
        ),
        commands=["pwd"],
    )

    assert len(room.containers.run_calls) == 1
    run_call = room.containers.run_calls[0]
    mounts = run_call["mounts"]
    assert isinstance(mounts, ContainerMountSpec)
    assert mounts.configs is None
    assert mounts.files is not None
    assert mounts.model_dump(mode="json")["files"] == [
        {
            "path": "/var/run/meshagent/spec.json",
            "text": '{"name":"assistant"}',
            "read_only": True,
        },
        {
            "path": "/var/run/meshagent/members.json",
            "text": '{"members":[]}',
            "read_only": True,
        },
    ]
    assert run_call["env"] == {
        "MESHAGENT_SPEC_PATH": "/var/run/meshagent/spec.json",
        "MESHAGENT_MEMBERS_PATH": "/var/run/meshagent/members.json",
    }


@pytest.mark.asyncio
async def test_container_shell_tool_timeout_omits_exit_code() -> None:
    room = _FakeRoom()
    timed_out_exec = _FakeExec(
        stdout_chunks=[b"line 1\n"],
        stderr_chunks=[b"warn 1\n"],
        exit_code=None,
    )
    room.containers.next_exec = timed_out_exec
    tool = ContainerShellTool(room=room, working_dir="/workspace")

    result = await tool.execute(
        context=ToolContext(
            caller=object(),
        ),
        commands=["sleep 5"],
        timeout_ms=10,
    )

    assert result == {
        "results": [
            {
                "outcome": {"type": "timeout"},
                "stdout": "line 1\n",
                "stderr": "warn 1\n",
            }
        ]
    }
    assert timed_out_exec.killed is True


@pytest.mark.asyncio
async def test_container_shell_tool_stop_stops_and_deletes_cached_container() -> None:
    room = _FakeRoom()
    tool = ContainerShellTool(room=room)

    await tool.execute(
        context=ToolContext(
            caller=object(),
        ),
        commands=["printf 'hello\\n'"],
    )

    await tool.stop(room=room)
    await tool.stop(room=room)

    assert room.containers.stop_calls == [
        {"container_id": "container-1", "force": True}
    ]
    assert room.containers.delete_calls == [{"container_id": "container-1"}]


@pytest.mark.asyncio
async def test_container_shell_tool_truncates_success_output_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(container_shell_module, "DEFAULT_MAX_OUTPUT_LENGTH", 8)

    room = _FakeRoom()
    room.containers.next_exec = _FakeExec(
        stdout_chunks=[b"abcdefghijk"],
        stderr_chunks=[],
    )
    emitted: list[dict] = []
    tool = ContainerShellTool(room=room)

    result = await tool.execute(
        context=ToolContext(
            caller=object(),
            event_handler=lambda event: emitted.append({**event, "item_id": "tool-1"}),
        ),
        commands=["cat /tmp/large.txt"],
    )

    assert result == {
        "results": [
            {
                "outcome": {"type": "exit", "exit_code": 0},
                "stdout": "abcdefgh\n\n[output truncated after 8 characters]",
                "stderr": "",
            }
        ]
    }
    assert emitted == [
        {
            "type": "meshagent.handler.output",
            "item_id": "tool-1",
            "lines": [
                {"source": "stdout", "text": "abcdefgh"},
                {
                    "source": "stdout",
                    "text": "[output truncated after 8 characters]",
                },
            ],
        }
    ]


@pytest.mark.asyncio
async def test_container_shell_tool_chunks_long_single_log_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(container_shell_module, "MAX_LOG_LINE_LENGTH", 4)
    monkeypatch.setattr(container_shell_module, "DEFAULT_MAX_OUTPUT_LENGTH", 32)

    room = _FakeRoom()
    room.containers.next_exec = _FakeExec(
        stdout_chunks=[b"abcdef\n"],
        stderr_chunks=[],
    )
    emitted: list[dict] = []
    tool = ContainerShellTool(room=room)

    result = await tool.execute(
        context=ToolContext(
            caller=object(),
            event_handler=lambda event: emitted.append({**event, "item_id": "tool-1"}),
        ),
        commands=["cat /tmp/app.js"],
    )

    assert result == {
        "results": [
            {
                "outcome": {"type": "exit", "exit_code": 0},
                "stdout": "abcdef\n",
                "stderr": "",
            }
        ]
    }
    assert emitted == [
        {
            "type": "meshagent.handler.output",
            "item_id": "tool-1",
            "lines": [
                {"source": "stdout", "text": "abcd"},
                {"source": "stdout", "text": "ef"},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_process_shell_tool_uses_working_dir_and_env(
    tmp_path,
) -> None:
    emitted: list[dict] = []
    tool = ProcessShellTool(
        working_dir=str(tmp_path),
        env={"EXAMPLE_VAR": "hello"},
    )

    result = await tool.execute(
        context=ToolContext(
            caller=object(),  # type: ignore[arg-type]
            event_handler=lambda event: emitted.append({**event, "item_id": "tool-1"}),
        ),
        commands=['printf \'%s|%s\' "$PWD" "$EXAMPLE_VAR"'],
    )

    assert result == {
        "results": [
            {
                "outcome": {"type": "exit", "exit_code": 0},
                "stdout": f"{tmp_path}|hello",
                "stderr": "",
            }
        ]
    }
    assert emitted == [
        {
            "type": "meshagent.handler.output",
            "item_id": "tool-1",
            "lines": [{"source": "stdout", "text": f"{tmp_path}|hello"}],
        }
    ]


@pytest.mark.asyncio
async def test_process_shell_tool_truncates_success_output_by_default() -> None:
    emitted: list[dict] = []
    tool = ProcessShellTool()

    result = await tool.execute(
        context=ToolContext(
            caller=object(),  # type: ignore[arg-type]
            event_handler=lambda event: emitted.append({**event, "item_id": "tool-1"}),
        ),
        commands=["printf 'abcdefghijk'"],
        max_output_length=8,
    )

    assert result == {
        "results": [
            {
                "outcome": {"type": "exit", "exit_code": 0},
                "stdout": "abcdefgh\n\n[output truncated after 8 characters]",
                "stderr": "",
            }
        ]
    }
    assert emitted == [
        {
            "type": "meshagent.handler.output",
            "item_id": "tool-1",
            "lines": [
                {"source": "stdout", "text": "abcdefgh"},
                {
                    "source": "stdout",
                    "text": "[output truncated after 8 characters]",
                },
            ],
        }
    ]


@pytest.mark.asyncio
async def test_container_toolkit_manages_container_lifecycle() -> None:
    room = _FakeRoom()
    toolkit = ContainerToolkit(
        room=room,
        working_dir="/workspace",
        default_image="python:3.13",
        env={"BASE": "1"},
    )
    context = ToolContext(caller=object())

    start_result = await toolkit.invoke(
        context=context,
        name="start_container",
        input=JsonContent(json={"env": [{"key": "USER", "value": "2"}]}),
    )

    assert isinstance(start_result, JsonContent)
    assert start_result.json == {"container_id": "container-1"}
    assert room.containers.run_calls == [
        {
            "command": "sleep infinity",
            "image": "python:3.13",
            "working_dir": "/workspace",
            "mounts": container_shell_module.DEFAULT_CONTAINER_MOUNT_SPEC,
            "writable_root_fs": True,
            "env": {"USER": "2", "BASE": "1"},
        }
    ]

    list_result = await toolkit.invoke(
        context=context,
        name="list_managed_containers",
        input=JsonContent(json={}),
    )

    assert isinstance(list_result, JsonContent)
    assert list_result.json == {
        "containers": [
            {
                "container_id": "container-1",
                "image": "python:3.13",
                "working_dir": "/workspace",
                "mounts": container_shell_module.DEFAULT_CONTAINER_MOUNT_SPEC.model_dump(
                    mode="json"
                ),
                "env": [
                    {"key": "BASE", "value": "1"},
                    {"key": "USER", "value": "2"},
                ],
            }
        ]
    }

    room.containers.next_exec = _FakeExec(
        stdout_chunks=[b"ok\n"],
        stderr_chunks=[],
        exit_code=0,
    )
    run_result = await toolkit.invoke(
        context=context,
        name="run_in_container",
        input=JsonContent(
            json={
                "container_id": "container-1",
                "commands": ["echo hi"],
                "timeout_ms": 500,
            }
        ),
    )

    assert isinstance(run_result, JsonContent)
    assert run_result.json == {
        "results": [
            {
                "outcome": {"type": "exit", "exit_code": 0},
                "stdout": "ok\n",
                "stderr": "",
            }
        ]
    }
    assert room.containers.exec_calls == [
        {
            "container_id": "container-1",
            "command": ["bash", "-lc", "cd /workspace && echo hi"],
            "tty": False,
        }
    ]

    stop_result = await toolkit.invoke(
        context=context,
        name="stop_managed_container",
        input=JsonContent(json={"container_id": "container-1"}),
    )

    assert isinstance(stop_result, JsonContent)
    assert stop_result.json == {"container_id": "container-1", "ok": True}
    assert room.containers.stop_calls == [
        {"container_id": "container-1", "force": True}
    ]
    assert room.containers.delete_calls == [{"container_id": "container-1"}]

    final_list_result = await toolkit.invoke(
        context=context,
        name="list_managed_containers",
        input=JsonContent(json={}),
    )

    assert isinstance(final_list_result, JsonContent)
    assert final_list_result.json == {"containers": []}


@pytest.mark.asyncio
async def test_container_toolkit_start_container_preserves_default_config_mounts(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text('{"name":"assistant"}')
    monkeypatch.setenv("MESHAGENT_SPEC_PATH", str(spec_path))
    monkeypatch.delenv("MESHAGENT_MEMBERS_PATH", raising=False)

    room = _FakeRoom()
    toolkit = ContainerToolkit(
        room=room,
        default_image="busybox:latest",
        mounts=ContainerMountSpec(configs=[ConfigMountSpec(path="/var/run/meshagent")]),
    )

    container_id = await toolkit._manager.start_container(
        room=room,
        image=None,
        mounts=ContainerMountSpec(
            room=[
                {
                    "path": "/workspace",
                    "subpath": "/src",
                    "read_only": False,
                }
            ]
        ),
        env=None,
        working_dir=None,
    )

    assert container_id == "container-1"
    assert len(room.containers.run_calls) == 1
    run_call = room.containers.run_calls[0]
    mounts = run_call["mounts"]
    assert isinstance(mounts, ContainerMountSpec)
    assert mounts.room is not None
    assert mounts.room[0].path == "/workspace"
    assert mounts.files is not None
    assert mounts.model_dump(mode="json")["files"] == [
        {
            "path": "/var/run/meshagent/spec.json",
            "text": '{"name":"assistant"}',
            "read_only": True,
        }
    ]
    assert run_call["env"] == {"MESHAGENT_SPEC_PATH": "/var/run/meshagent/spec.json"}
    record = await toolkit._manager.require_container(container_id=container_id)
    assert record.mounts is not None
    assert record.mounts.configs == [ConfigMountSpec(path="/var/run/meshagent")]


@pytest.mark.asyncio
async def test_container_toolkit_rejects_unmanaged_containers() -> None:
    room = _FakeRoom()
    toolkit = ContainerToolkit(room=room, default_image="python:3.13")
    context = ToolContext(caller=object())

    with pytest.raises(Exception, match="not managed by this toolkit"):
        await toolkit.invoke(
            context=context,
            name="run_in_container",
            input=JsonContent(
                json={"container_id": "container-404", "commands": ["echo hi"]}
            ),
        )
