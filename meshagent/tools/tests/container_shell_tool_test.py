import asyncio

import pytest

import meshagent.tools.container_shell as container_shell_module
from meshagent.tools import (
    ContainerShellTool,
    ContainerToolkit,
    JsonContent,
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
        exit_code: int = 0,
    ) -> None:
        self._stdout_chunks = stdout_chunks
        self._stderr_chunks = stderr_chunks
        loop = asyncio.get_running_loop()
        self.result = loop.create_future()
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


@pytest.mark.asyncio
async def test_container_shell_tool_emits_live_output_events() -> None:
    room = _FakeRoom()
    emitted: list[dict] = []
    tool = ContainerShellTool(working_dir="/workspace")

    result = await tool.execute(
        context=ToolContext(
            room=room,
            caller=object(),
            caller_context={"item_id": "tool-1"},
            event_handler=emitted.append,
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
    tool = ContainerShellTool()

    result = await tool.execute(
        context=ToolContext(
            room=room,
            caller=object(),
            caller_context={"item_id": "tool-1"},
            event_handler=emitted.append,
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
    tool = ContainerShellTool()

    result = await tool.execute(
        context=ToolContext(
            room=room,
            caller=object(),
            caller_context={"item_id": "tool-1"},
            event_handler=emitted.append,
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
async def test_container_toolkit_manages_container_lifecycle() -> None:
    room = _FakeRoom()
    toolkit = ContainerToolkit(
        working_dir="/workspace",
        default_image="python:3.13",
        env={"BASE": "1"},
    )
    context = ToolContext(room=room, caller=object())

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
async def test_container_toolkit_rejects_unmanaged_containers() -> None:
    room = _FakeRoom()
    toolkit = ContainerToolkit(default_image="python:3.13")
    context = ToolContext(room=room, caller=object())

    with pytest.raises(Exception, match="not managed by this toolkit"):
        await toolkit.invoke(
            context=context,
            name="run_in_container",
            input=JsonContent(
                json={"container_id": "container-404", "commands": ["echo hi"]}
            ),
        )
