from typing import Any, Optional

import pytest

from meshagent.api import ErrorCode
from meshagent.api.messaging import JsonContent
from meshagent.api.room_server_client import (
    MemoryDeleteEntitiesResult,
    MemoryDeleteRelationshipsResult,
    MemoryEntityRecord,
    MemoryIngestResult,
    MemoryIngestStats,
    MemoryRecallItem,
    MemoryRecallRelationship,
    MemoryRecallResult,
    MemoryRelationshipSelector,
    RoomException,
)
from meshagent.tools import ToolContext
from meshagent.tools.memories import MemoriesToolkit


class _FakeMemoryClient:
    def __init__(self) -> None:
        self.upsert_nodes_calls: list[dict[str, Any]] = []
        self.ingest_text_calls: list[dict[str, Any]] = []
        self.recall_calls: list[dict[str, Any]] = []
        self.delete_entities_calls: list[dict[str, Any]] = []
        self.delete_relationships_calls: list[dict[str, Any]] = []
        self.query_calls: list[dict[str, Any]] = []
        self.drop_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []
        self.next_query_results: list[list[dict[str, Any]]] = []
        self.create_exception: Optional[Exception] = None
        self.recall_exception: Optional[Exception] = None

    async def upsert_nodes(
        self,
        *,
        name: str,
        records: list[MemoryEntityRecord],
        merge: bool = True,
        namespace: Optional[list[str]] = None,
    ) -> None:
        self.upsert_nodes_calls.append(
            {
                "name": name,
                "records": records,
                "merge": merge,
                "namespace": namespace,
            }
        )

    async def ingest_text(
        self,
        *,
        name: str,
        text: str,
        namespace: Optional[list[str]] = None,
        strategy: str = "heuristic",
        llm_model: Optional[str] = None,
        llm_temperature: Optional[float] = None,
    ) -> MemoryIngestResult:
        self.ingest_text_calls.append(
            {
                "name": name,
                "text": text,
                "namespace": namespace,
                "strategy": strategy,
                "llm_model": llm_model,
                "llm_temperature": llm_temperature,
            }
        )
        return MemoryIngestResult(
            name=name,
            stats=MemoryIngestStats(entities=1, relationships=0, sources=1),
            entity_ids=["memory-1", "person-1"],
        )

    async def recall(
        self,
        *,
        name: str,
        query: str,
        namespace: Optional[list[str]] = None,
        limit: int = 5,
        include_relationships: bool = True,
    ) -> MemoryRecallResult:
        if self.recall_exception is not None:
            raise self.recall_exception
        self.recall_calls.append(
            {
                "name": name,
                "query": query,
                "namespace": namespace,
                "limit": limit,
                "include_relationships": include_relationships,
            }
        )
        return MemoryRecallResult(
            name=name,
            query=query,
            items=[
                MemoryRecallItem(
                    entity_id="memory-1",
                    name="Memory 1",
                    entity_type="MEMORY",
                    context="Alice likes tea",
                    created_at="2026-01-01T00:00:00Z",
                    valid_at="2026-01-02T00:00:00Z",
                    score=2.0,
                    relationships=[
                        MemoryRecallRelationship(
                            source_entity_id="memory-1",
                            target_entity_id="person-1",
                            relationship_type="ABOUT",
                            description="About Alice",
                            created_at="2026-01-03T00:00:00Z",
                            valid_at="2026-01-04T00:00:00Z",
                            expired_at="2026-02-01T00:00:00Z",
                            invalid_at="2026-02-02T00:00:00Z",
                        )
                    ],
                ),
                MemoryRecallItem(
                    entity_id="person-1",
                    name="Alice",
                    entity_type="PERSON",
                    context="Alice person node",
                    created_at="2026-01-01T00:00:01Z",
                    valid_at="2026-01-02T00:00:01Z",
                    score=1.5,
                ),
            ],
        )

    async def delete_entities(
        self,
        *,
        name: str,
        entity_ids: list[str],
        namespace: Optional[list[str]] = None,
    ) -> MemoryDeleteEntitiesResult:
        self.delete_entities_calls.append(
            {
                "name": name,
                "entity_ids": entity_ids,
                "namespace": namespace,
            }
        )
        return MemoryDeleteEntitiesResult(
            name=name,
            deleted_entities=len(entity_ids),
            deleted_relationships=0,
        )

    async def delete_relationships(
        self,
        *,
        name: str,
        relationships: list[MemoryRelationshipSelector],
        namespace: Optional[list[str]] = None,
    ) -> MemoryDeleteRelationshipsResult:
        self.delete_relationships_calls.append(
            {
                "name": name,
                "relationships": relationships,
                "namespace": namespace,
            }
        )
        return MemoryDeleteRelationshipsResult(
            name=name,
            deleted_relationships=len(relationships),
        )

    async def query(
        self,
        *,
        name: str,
        statement: str,
        namespace: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        self.query_calls.append(
            {
                "name": name,
                "statement": statement,
                "namespace": namespace,
            }
        )
        if len(self.next_query_results) > 0:
            return self.next_query_results.pop(0)
        return []

    async def drop(
        self,
        *,
        name: str,
        namespace: Optional[list[str]] = None,
        ignore_missing: bool = False,
    ) -> None:
        self.drop_calls.append(
            {
                "name": name,
                "namespace": namespace,
                "ignore_missing": ignore_missing,
            }
        )

    async def create(
        self,
        *,
        name: str,
        namespace: Optional[list[str]] = None,
        overwrite: bool = False,
        ignore_exists: bool = False,
    ) -> None:
        if self.create_exception is not None:
            raise self.create_exception
        self.create_calls.append(
            {
                "name": name,
                "namespace": namespace,
                "overwrite": overwrite,
                "ignore_exists": ignore_exists,
            }
        )


class _FakeRoom:
    def __init__(self) -> None:
        self.memory = _FakeMemoryClient()


def test_memories_tool_schemas_remain_strict_for_openai() -> None:
    toolkit = MemoriesToolkit(memory_name="graph")
    required_by_tool = {
        tool.name: set((tool.input_schema or {}).get("required", []))
        for tool in toolkit.tools
    }

    assert required_by_tool["add_memory"] == {"memory"}
    assert required_by_tool["search_memories"] == {
        "query",
        "entity_type",
    }
    assert required_by_tool["get_recent_memories"] == {"limit", "entity_type"}
    assert required_by_tool["get_recent_relationships"] == {
        "limit",
        "entity_id",
        "relationship_type",
    }
    assert required_by_tool["get_entity"] == {"entity_id"}
    assert required_by_tool["delete_entity"] == {"entity_id"}
    assert required_by_tool["delete_relationship"] == {
        "source_entity_id",
        "target_entity_id",
        "relationship_type",
    }
    assert required_by_tool["delete_all_memories"] == {"confirm"}
    assert set(required_by_tool.keys()) == {
        "add_memory",
        "search_memories",
        "get_recent_memories",
        "get_recent_relationships",
        "get_entity",
        "delete_entity",
        "delete_relationship",
        "delete_all_memories",
    }


@pytest.mark.asyncio
async def test_add_memory_uses_llm_ingest_text() -> None:
    toolkit = MemoriesToolkit(
        memory_name="graph",
        namespace=["team"],
        llm_model="gpt-5.2",
        llm_temperature=0.2,
    )
    room = _FakeRoom()
    context = ToolContext(room=room, caller=object())

    result = await toolkit.execute(
        context=context,
        name="add_memory",
        input=JsonContent(json={"memory": "Alice likes tea"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json["name"] == "graph"
    assert result.json["strategy"] == "llm"
    assert result.json["stats"] == {"entities": 1, "relationships": 0, "sources": 1}
    assert result.json["entity_ids"] == ["memory-1", "person-1"]
    assert result.json["primary_entity_id"] == "memory-1"
    assert len(room.memory.ingest_text_calls) == 1
    call = room.memory.ingest_text_calls[0]
    assert call["name"] == "graph"
    assert call["namespace"] == ["team"]
    assert call["text"] == "Alice likes tea"
    assert call["strategy"] == "llm"
    assert call["llm_model"] == "gpt-5.2"
    assert call["llm_temperature"] == 0.2
    assert len(room.memory.recall_calls) == 0


@pytest.mark.asyncio
async def test_search_memories_creates_memory_store_when_missing() -> None:
    toolkit = MemoriesToolkit(memory_name="graph", namespace=["team"])
    room = _FakeRoom()
    context = ToolContext(room=room, caller=object())

    result = await toolkit.execute(
        context=context,
        name="search_memories",
        input=JsonContent(json={"query": "Alice"}),
    )

    assert isinstance(result, JsonContent)
    assert room.memory.create_calls == [
        {
            "name": "graph",
            "namespace": ["team"],
            "overwrite": False,
            "ignore_exists": True,
        }
    ]
    assert len(room.memory.recall_calls) == 1


@pytest.mark.asyncio
async def test_search_memories_ignores_create_permission_error() -> None:
    toolkit = MemoriesToolkit(memory_name="graph")
    room = _FakeRoom()
    room.memory.create_exception = RoomException(
        "you do not have permission to perform the requested action",
        code=ErrorCode.PERMISSION_DENIED,
    )
    context = ToolContext(room=room, caller=object())

    result = await toolkit.execute(
        context=context,
        name="search_memories",
        input=JsonContent(json={"query": "Alice"}),
    )

    assert isinstance(result, JsonContent)
    assert len(room.memory.recall_calls) == 1


@pytest.mark.asyncio
async def test_search_memories_returns_no_memories_yet_when_missing() -> None:
    toolkit = MemoriesToolkit(memory_name="graph")
    room = _FakeRoom()
    room.memory.recall_exception = RoomException(
        "memory does not exist: graph",
        code=ErrorCode.MEMORY_NOT_FOUND,
    )
    context = ToolContext(room=room, caller=object())

    result = await toolkit.execute(
        context=context,
        name="search_memories",
        input=JsonContent(json={"query": "Alice"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "query": "Alice",
        "memories": [],
        "message": "no memories yet",
    }


@pytest.mark.asyncio
async def test_search_memories_uses_recall_like_ask_button() -> None:
    toolkit = MemoriesToolkit(memory_name="graph", namespace=["team"])
    room = _FakeRoom()
    context = ToolContext(room=room, caller=object())

    result = await toolkit.execute(
        context=context,
        name="search_memories",
        input=JsonContent(json={"query": "Alice"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json["query"] == "Alice"
    assert len(result.json["memories"]) == 2
    assert result.json["memories"][0]["entity_id"] == "memory-1"
    assert result.json["memories"][0]["created_at"] == "2026-01-01T00:00:00Z"
    assert result.json["memories"][0]["valid_at"] == "2026-01-02T00:00:00Z"
    assert result.json["memories"][0]["relationships"][0]["created_at"] == (
        "2026-01-03T00:00:00Z"
    )
    assert result.json["memories"][0]["relationships"][0]["expired_at"] == (
        "2026-02-01T00:00:00Z"
    )
    assert len(room.memory.recall_calls) == 1
    recall_call = room.memory.recall_calls[0]
    assert recall_call["name"] == "graph"
    assert recall_call["namespace"] == ["team"]
    assert recall_call["query"] == "Alice"
    assert recall_call["limit"] == 20
    assert recall_call["include_relationships"] is True
    assert len(room.memory.query_calls) == 0


@pytest.mark.asyncio
async def test_search_memories_uses_custom_toolkit_search_limit() -> None:
    toolkit = MemoriesToolkit(memory_name="graph", search_limit=7)
    room = _FakeRoom()
    context = ToolContext(room=room, caller=object())

    result = await toolkit.execute(
        context=context,
        name="search_memories",
        input=JsonContent(json={"query": "Alice"}),
    )

    assert isinstance(result, JsonContent)
    assert len(room.memory.recall_calls) == 1
    recall_call = room.memory.recall_calls[0]
    assert recall_call["limit"] == 7
    assert recall_call["include_relationships"] is True


@pytest.mark.asyncio
async def test_get_recent_memories_uses_temporal_sorting() -> None:
    toolkit = MemoriesToolkit(memory_name="graph", namespace=["team"])
    room = _FakeRoom()
    room.memory.next_query_results = [
        [
            {
                "entity_id": "memory-1",
                "name": "Memory 1",
                "entity_type": "MEMORY",
                "memory": "Alice likes tea",
                "confidence": 0.9,
                "created_at": "2026-01-01T00:00:00Z",
                "valid_at": "2026-01-02T00:00:00Z",
            }
        ]
    ]
    context = ToolContext(room=room, caller=object())

    result = await toolkit.execute(
        context=context,
        name="get_recent_memories",
        input=JsonContent(json={"limit": 10, "entity_type": "MEMORY"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json["memories"] == [
        {
            "entity_id": "memory-1",
            "name": "Memory 1",
            "entity_type": "MEMORY",
            "memory": "Alice likes tea",
            "confidence": 0.9,
            "created_at": "2026-01-01T00:00:00Z",
            "valid_at": "2026-01-02T00:00:00Z",
        }
    ]
    assert len(room.memory.query_calls) == 1
    statement = room.memory.query_calls[0]["statement"]
    assert "ORDER BY e.valid_at DESC, e.created_at DESC, e.name" in statement
    assert "WHERE e.entity_type = 'MEMORY'" in statement
    assert room.memory.query_calls[0]["namespace"] == ["team"]


@pytest.mark.asyncio
async def test_get_recent_relationships_uses_temporal_sorting_and_filters() -> None:
    toolkit = MemoriesToolkit(memory_name="graph")
    room = _FakeRoom()
    room.memory.next_query_results = [
        [
            {
                "source_entity_id": "memory-1",
                "target_entity_id": "person-1",
                "source_entity_name": "Memory 1",
                "target_entity_name": "Alice",
                "relationship_type": "ABOUT",
                "description": "About Alice",
                "confidence": 0.8,
                "created_at": "2026-01-03T00:00:00Z",
                "valid_at": "2026-01-04T00:00:00Z",
                "expired_at": "2026-02-01T00:00:00Z",
                "invalid_at": "2026-02-02T00:00:00Z",
            }
        ]
    ]
    context = ToolContext(room=room, caller=object())

    result = await toolkit.execute(
        context=context,
        name="get_recent_relationships",
        input=JsonContent(
            json={
                "limit": 10,
                "entity_id": "memory-1",
                "relationship_type": "ABOUT",
            }
        ),
    )

    assert isinstance(result, JsonContent)
    assert result.json["relationships"] == [
        {
            "source_entity_id": "memory-1",
            "target_entity_id": "person-1",
            "source_entity_name": "Memory 1",
            "target_entity_name": "Alice",
            "relationship_type": "ABOUT",
            "description": "About Alice",
            "confidence": 0.8,
            "created_at": "2026-01-03T00:00:00Z",
            "valid_at": "2026-01-04T00:00:00Z",
            "expired_at": "2026-02-01T00:00:00Z",
            "invalid_at": "2026-02-02T00:00:00Z",
        }
    ]
    assert len(room.memory.query_calls) == 1
    statement = room.memory.query_calls[0]["statement"]
    assert (
        "WHERE (r.source_entity_id = 'memory-1' OR r.target_entity_id = 'memory-1') "
        "AND r.relationship_type = 'ABOUT'"
    ) in statement
    assert (
        "ORDER BY r.valid_at DESC, r.created_at DESC, r.relationship_type" in statement
    )


@pytest.mark.asyncio
async def test_get_entity_queries_by_entity_id() -> None:
    toolkit = MemoriesToolkit(memory_name="graph")
    room = _FakeRoom()
    room.memory.next_query_results = [
        [
            {
                "entity_id": "memory-1",
                "name": "Memory 1",
                "entity_type": "MEMORY",
                "memory": "Alice likes tea",
                "confidence": 0.9,
            }
        ]
    ]
    context = ToolContext(room=room, caller=object())

    result = await toolkit.execute(
        context=context,
        name="get_entity",
        input=JsonContent(json={"entity_id": "memory-1"}),
    )

    assert isinstance(result, JsonContent)
    entity = result.json["entity"]
    assert entity["entity_id"] == "memory-1"
    assert entity["memory"] == "Alice likes tea"
    assert len(room.memory.query_calls) == 1
    assert "WHERE e.entity_id = 'memory-1'" in room.memory.query_calls[0]["statement"]


@pytest.mark.asyncio
async def test_get_entity_returns_none_when_missing() -> None:
    toolkit = MemoriesToolkit(memory_name="graph")
    room = _FakeRoom()
    room.memory.next_query_results = [[]]
    context = ToolContext(room=room, caller=object())

    result = await toolkit.execute(
        context=context,
        name="get_entity",
        input=JsonContent(json={"entity_id": "memory-1"}),
    )

    assert isinstance(result, JsonContent)
    assert result.json["entity"] is None
    assert len(room.memory.query_calls) == 1


@pytest.mark.asyncio
async def test_delete_entity_uses_delete_entities_api() -> None:
    toolkit = MemoriesToolkit(memory_name="graph")
    room = _FakeRoom()
    context = ToolContext(room=room, caller=object())

    delete_result = await toolkit.execute(
        context=context,
        name="delete_entity",
        input=JsonContent(json={"entity_id": "memory-1"}),
    )

    assert isinstance(delete_result, JsonContent)
    assert delete_result.json == {
        "deleted": True,
        "entity_id": "memory-1",
        "deleted_entities": 1,
        "deleted_relationships": 0,
    }
    assert room.memory.delete_entities_calls == [
        {"name": "graph", "entity_ids": ["memory-1"], "namespace": None},
    ]


@pytest.mark.asyncio
async def test_delete_relationship_uses_delete_relationships_api() -> None:
    toolkit = MemoriesToolkit(memory_name="graph", namespace=["team"])
    room = _FakeRoom()
    context = ToolContext(room=room, caller=object())

    result = await toolkit.execute(
        context=context,
        name="delete_relationship",
        input=JsonContent(
            json={
                "source_entity_id": "memory-1",
                "target_entity_id": "person-1",
                "relationship_type": "ABOUT",
            }
        ),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {
        "deleted": True,
        "source_entity_id": "memory-1",
        "target_entity_id": "person-1",
        "relationship_type": "ABOUT",
        "deleted_relationships": 1,
    }
    assert len(room.memory.delete_relationships_calls) == 1
    call = room.memory.delete_relationships_calls[0]
    assert call["name"] == "graph"
    assert call["namespace"] == ["team"]
    assert len(call["relationships"]) == 1
    selector = call["relationships"][0]
    assert selector.source_entity_id == "memory-1"
    assert selector.target_entity_id == "person-1"
    assert selector.relationship_type == "ABOUT"


@pytest.mark.asyncio
async def test_delete_all_memories_requires_confirm_and_recreates() -> None:
    toolkit = MemoriesToolkit(memory_name="graph", namespace=["team"])
    room = _FakeRoom()
    context = ToolContext(room=room, caller=object())

    with pytest.raises(ValueError):
        await toolkit.execute(
            context=context,
            name="delete_all_memories",
            input=JsonContent(json={"confirm": False}),
        )

    result = await toolkit.execute(
        context=context,
        name="delete_all_memories",
        input=JsonContent(json={"confirm": True}),
    )

    assert isinstance(result, JsonContent)
    assert result.json == {"ok": True, "name": "graph"}
    assert room.memory.drop_calls == [
        {"name": "graph", "namespace": ["team"], "ignore_missing": True}
    ]
    assert room.memory.create_calls == [
        {
            "name": "graph",
            "namespace": ["team"],
            "overwrite": True,
            "ignore_exists": False,
        }
    ]
