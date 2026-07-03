from dataclasses import dataclass

import pytest

from meshagent.tools import ToolContext
from meshagent.tools.discovery import ListTools


@dataclass
class _Participant:
    id: str


class _ToolkitDescription:
    def to_json(self):
        return {"name": "math", "tools": []}


class _Agents:
    def __init__(self):
        self.participant_ids: list[str] = []

    async def list_toolkits(self, *, participant_id: str):
        self.participant_ids.append(participant_id)
        return [_ToolkitDescription()]


class _Room:
    def __init__(self):
        self.agents = _Agents()


@pytest.mark.asyncio
async def test_list_tools_uses_on_behalf_participant_without_stdout(capsys) -> None:
    room = _Room()
    tool = ListTools(room=room)
    context = ToolContext(
        caller=_Participant(id="caller-1"),
        on_behalf_of=_Participant(id="target-1"),
    )

    result = await tool.execute(context)

    assert room.agents.participant_ids == ["target-1"]
    assert result == {"toolkits": [{"name": "math", "tools": []}]}
    assert capsys.readouterr().out == ""
