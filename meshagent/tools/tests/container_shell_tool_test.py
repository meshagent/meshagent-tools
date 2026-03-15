import asyncio

import pytest

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
