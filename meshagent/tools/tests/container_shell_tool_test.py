import asyncio

import pytest

import meshagent.tools.container_shell as container_shell_module
from meshagent.tools import ContainerShellTool, ToolContext


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
        self.exec_commands: list[list[str]] = []
        self.next_exec = _FakeExec(
            stdout_chunks=[b"line 1\npart", b"ial"],
            stderr_chunks=[b"warn 1\n"],
        )

    async def list(self) -> list[_FakeContainer]:
        return [*self._running]

    async def run(self, **kwargs) -> str:
        del kwargs
        container = _FakeContainer("container-1")
        self._running.append(container)
        return container.id

    async def exec(self, *, container_id: str, command, tty: bool):
        del container_id
        del tty
        self.exec_commands.append(command)
        return self.next_exec


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
