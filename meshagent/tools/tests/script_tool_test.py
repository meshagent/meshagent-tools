import asyncio

import pytest

from meshagent.tools import ScriptTool, ToolContext


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
        self.exec_calls: list[dict[str, object]] = []
        self.next_exec = _FakeExec(stdout_chunks=[], stderr_chunks=[])

    async def list(self) -> list[_FakeContainer]:
        return [*self._running]

    async def run(self, **kwargs) -> str:
        self.run_calls.append(kwargs)
        container = _FakeContainer("container-1")
        self._running.append(container)
        return container.id

    async def exec(self, *, container_id: str, command, tty: bool):
        self.exec_calls.append(
            {
                "container_id": container_id,
                "command": command,
                "tty": tty,
            }
        )
        return self.next_exec


class _FakeRoom:
    def __init__(self) -> None:
        self.containers = _FakeContainers()


@pytest.mark.asyncio
async def test_script_tool_container_exec_truncates_success_output() -> None:
    room = _FakeRoom()
    room.containers.next_exec = _FakeExec(
        stdout_chunks=[b"abcdefghijk"],
        stderr_chunks=[],
        exit_code=0,
    )
    emitted: list[dict[str, object]] = []
    tool = ScriptTool(
        room=room,
        name="script",
        commands=["echo hi"],
        image="python:3.13",
        max_output_length=8,
    )

    result = await tool.execute(
        context=ToolContext(
            caller=object(),
            caller_context={"item_id": "tool-1"},
            event_handler=emitted.append,
        )
    )

    assert room.containers.exec_calls == [
        {
            "container_id": "container-1",
            "command": ["bash", "-c", "echo hi"],
            "tty": False,
        }
    ]
    assert result == {
        "outcome": {"type": "exit", "exit_code": 0},
        "stdout": "abcdefgh\n\n[output truncated after 8 characters]",
        "stderr": "",
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
async def test_script_tool_local_exec_truncates_success_output() -> None:
    room = _FakeRoom()
    emitted: list[dict[str, object]] = []
    tool = ScriptTool(
        room=room,
        name="script",
        commands=["printf 'abcdefghijk'"],
        image=None,
        max_output_length=8,
    )

    result = await tool.execute(
        context=ToolContext(
            caller=object(),
            caller_context={"item_id": "tool-1"},
            event_handler=emitted.append,
        )
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
