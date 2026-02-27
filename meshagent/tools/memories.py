from typing import Any, Literal, Optional

from meshagent.api.room_server_client import (
    MemoryRelationshipSelector,
    MemoryRecallItem,
    RoomException,
    RoomClient,
)

from .config import ToolkitConfig
from .hosting import RemoteToolkit, Toolkit
from .strict_schema import ensure_strict_json_schema
from .tool import FunctionTool
from .toolkit import ToolContext, ToolkitBuilder


def _escape_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _entity_from_entity_row(row: dict[str, Any]) -> dict[str, Any]:
    raw_entity_id = row.get("entity_id")
    entity_id = (
        raw_entity_id
        if isinstance(raw_entity_id, str)
        else (str(raw_entity_id) if raw_entity_id is not None else "")
    )

    out: dict[str, Any] = {
        "entity_id": entity_id,
        "name": row.get("name"),
        "entity_type": row.get("entity_type"),
        "memory": row.get("memory"),
        "confidence": row.get("confidence"),
        "created_at": row.get("created_at"),
        "valid_at": row.get("valid_at"),
    }

    return out


def _relationship_from_relationship_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_entity_id": row.get("source_entity_id"),
        "target_entity_id": row.get("target_entity_id"),
        "source_entity_name": row.get("source_entity_name"),
        "target_entity_name": row.get("target_entity_name"),
        "relationship_type": row.get("relationship_type"),
        "description": row.get("description"),
        "confidence": row.get("confidence"),
        "created_at": row.get("created_at"),
        "valid_at": row.get("valid_at"),
        "expired_at": row.get("expired_at"),
        "invalid_at": row.get("invalid_at"),
    }


def _entity_from_recall_item(item: MemoryRecallItem) -> dict[str, Any]:
    return {
        "entity_id": item.entity_id,
        "name": item.name,
        "entity_type": item.entity_type,
        "memory": item.context,
        "confidence": item.confidence,
        "created_at": item.created_at,
        "valid_at": item.valid_at,
        "score": item.score,
        "relationships": [*(rel.model_dump(mode="json") for rel in item.relationships)],
    }


class _MemoriesTool(FunctionTool):
    def __init__(
        self,
        *,
        memory_name: str,
        namespace: Optional[list[str]] = None,
        name: str,
        title: str,
        description: str,
        input_schema: dict,
    ):
        super().__init__(
            name=name,
            title=title,
            description=description,
            input_schema=ensure_strict_json_schema(input_schema),
        )
        self.memory_name = memory_name
        self.namespace = [*namespace] if namespace is not None else None

    def _memory_namespace(self) -> Optional[list[str]]:
        if self.namespace is None:
            return None
        return [*self.namespace]

    async def _ensure_memory_location(self, context: ToolContext) -> None:
        try:
            await context.room.memory.create(
                name=self.memory_name,
                namespace=self._memory_namespace(),
                overwrite=False,
                ignore_exists=True,
            )
        except RoomException as ex:
            message = str(ex).lower()
            if "you do not have permission to perform the requested action" in message:
                return
            raise

    @staticmethod
    def _build_entity_query_statement(
        *,
        where_clause: Optional[str],
        limit: int,
    ) -> str:
        statement = "MATCH (e:Entity) "
        if where_clause is not None and where_clause.strip() != "":
            statement = f"{statement}WHERE {where_clause.strip()} "
        return (
            f"{statement}RETURN e.entity_id as entity_id, "
            "e.name as name, "
            "e.entity_type as entity_type, "
            "e.context as memory, "
            "e.confidence as confidence "
            "ORDER BY e.name "
            f"LIMIT {limit}"
        )

    @staticmethod
    def _build_recent_entity_query_statement(
        *,
        where_clause: Optional[str],
        limit: int,
    ) -> str:
        statement = "MATCH (e:Entity) "
        if where_clause is not None and where_clause.strip() != "":
            statement = f"{statement}WHERE {where_clause.strip()} "
        return (
            f"{statement}RETURN e.entity_id as entity_id, "
            "e.name as name, "
            "e.entity_type as entity_type, "
            "e.context as memory, "
            "e.confidence as confidence, "
            "e.created_at as created_at, "
            "e.valid_at as valid_at "
            "ORDER BY e.valid_at DESC, e.created_at DESC, e.name "
            f"LIMIT {limit}"
        )

    @staticmethod
    def _build_relationship_query_statement(
        *,
        where_clause: Optional[str],
        limit: int,
    ) -> str:
        statement = "MATCH (r:RELATIONSHIP) "
        if where_clause is not None and where_clause.strip() != "":
            statement = f"{statement}WHERE {where_clause.strip()} "
        return (
            f"{statement}RETURN r.source_entity_id as source_entity_id, "
            "r.target_entity_id as target_entity_id, "
            "r.source_entity_name as source_entity_name, "
            "r.target_entity_name as target_entity_name, "
            "r.relationship_type as relationship_type, "
            "r.description as description, "
            "r.confidence as confidence, "
            "r.created_at as created_at, "
            "r.valid_at as valid_at, "
            "r.expired_at as expired_at, "
            "r.invalid_at as invalid_at "
            "ORDER BY r.valid_at DESC, r.created_at DESC, "
            "r.relationship_type "
            f"LIMIT {limit}"
        )

    async def _query_entities(
        self,
        context: ToolContext,
        *,
        where_clause: Optional[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be >= 1")

        await self._ensure_memory_location(context)

        statement = self._build_entity_query_statement(
            where_clause=where_clause,
            limit=limit,
        )
        try:
            return await context.room.memory.query(
                name=self.memory_name,
                namespace=self._memory_namespace(),
                statement=statement,
            )
        except RoomException as ex:
            message = str(ex).lower()
            if (
                "dataset 'entity' not found" in message
                or "table has no data" in message
            ):
                return []
            raise

    async def _query_relationships(
        self,
        context: ToolContext,
        *,
        where_clause: Optional[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be >= 1")

        await self._ensure_memory_location(context)

        statement = self._build_relationship_query_statement(
            where_clause=where_clause,
            limit=limit,
        )
        try:
            return await context.room.memory.query(
                name=self.memory_name,
                namespace=self._memory_namespace(),
                statement=statement,
            )
        except RoomException as ex:
            message = str(ex).lower()
            if (
                "dataset 'relationship' not found" in message
                or "table has no data" in message
            ):
                return []
            raise


class AddMemoryTool(_MemoriesTool):
    def __init__(
        self,
        *,
        memory_name: str,
        namespace: Optional[list[str]] = None,
        llm_model: Optional[str] = None,
        llm_temperature: Optional[float] = None,
    ):
        super().__init__(
            memory_name=memory_name,
            namespace=namespace,
            name="add_memory",
            title="add memory",
            description="Ingest text into memory using LLM extraction.",
            input_schema={
                "type": "object",
                "required": ["memory"],
                "additionalProperties": False,
                "properties": {
                    "memory": {
                        "type": "string",
                        "description": "Memory content.",
                    },
                },
            },
        )
        self.llm_model = llm_model
        self.llm_temperature = llm_temperature

    async def execute(
        self,
        context: ToolContext,
        *,
        memory: str,
    ):
        await self._ensure_memory_location(context)
        ingest_result = await context.room.memory.ingest_text(
            name=self.memory_name,
            namespace=self._memory_namespace(),
            text=memory,
            strategy="llm",
            llm_model=self.llm_model,
            llm_temperature=self.llm_temperature,
        )

        entity_ids = [*ingest_result.entity_ids]

        return {
            "name": ingest_result.name,
            "strategy": "llm",
            "stats": ingest_result.stats.model_dump(mode="json"),
            "entity_ids": entity_ids,
            "primary_entity_id": entity_ids[0] if len(entity_ids) > 0 else None,
        }


class SearchMemoriesTool(_MemoriesTool):
    def __init__(
        self,
        *,
        memory_name: str,
        namespace: Optional[list[str]] = None,
        recall_limit: int = 20,
    ):
        if recall_limit < 1:
            raise ValueError("recall_limit must be >= 1")
        super().__init__(
            memory_name=memory_name,
            namespace=namespace,
            name="search_memories",
            title="search memories",
            description=(
                "Semantic search over memory entities. Always returns up to "
                f"{recall_limit} memories with relationships."
            ),
            input_schema={
                "type": "object",
                "required": ["query"],
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string", "description": "Search query text."},
                    "entity_type": {
                        "type": ["string", "null"],
                        "description": "Optional entity_type filter.",
                    },
                },
            },
        )
        self._recall_limit = recall_limit

    async def execute(
        self,
        context: ToolContext,
        *,
        query: str,
        entity_type: Optional[str] = None,
    ):
        await self._ensure_memory_location(context)
        if query.strip() == "":
            return {"query": query, "memories": []}

        result = await context.room.memory.recall(
            name=self.memory_name,
            namespace=self._memory_namespace(),
            query=query,
            limit=self._recall_limit,
            include_relationships=True,
        )

        memories = list[dict[str, Any]]()
        normalized_entity_type = (
            entity_type.strip().upper()
            if isinstance(entity_type, str) and entity_type.strip() != ""
            else None
        )
        for item in result.items:
            if (
                normalized_entity_type is not None
                and item.entity_type.upper() != normalized_entity_type
            ):
                continue
            memories.append(_entity_from_recall_item(item))

        return {"query": query, "memories": memories}


class GetRecentMemoriesTool(_MemoriesTool):
    def __init__(self, *, memory_name: str, namespace: Optional[list[str]] = None):
        super().__init__(
            memory_name=memory_name,
            namespace=namespace,
            name="get_recent_memories",
            title="get recent memories",
            description="List most recent memory entities by valid_at/created_at.",
            input_schema={
                "type": "object",
                "required": [],
                "additionalProperties": False,
                "properties": {
                    "limit": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": 1000,
                        "description": "Max number of results.",
                    },
                    "entity_type": {
                        "type": ["string", "null"],
                        "description": "Optional entity_type filter.",
                    },
                },
            },
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        limit: Optional[int] = 50,
        entity_type: Optional[str] = None,
    ):
        if limit is None:
            limit = 50

        await self._ensure_memory_location(context)

        where_clause = None
        if isinstance(entity_type, str) and entity_type.strip() != "":
            escaped = _escape_query_value(entity_type.strip())
            where_clause = f"e.entity_type = '{escaped}'"

        statement = self._build_recent_entity_query_statement(
            where_clause=where_clause,
            limit=limit,
        )
        try:
            rows = await context.room.memory.query(
                name=self.memory_name,
                namespace=self._memory_namespace(),
                statement=statement,
            )
        except RoomException as ex:
            message = str(ex).lower()
            if (
                "dataset 'entity' not found" in message
                or "table has no data" in message
            ):
                rows = []
            else:
                raise
        return {"memories": [*(_entity_from_entity_row(row) for row in rows)]}


class GetRecentRelationshipsTool(_MemoriesTool):
    def __init__(self, *, memory_name: str, namespace: Optional[list[str]] = None):
        super().__init__(
            memory_name=memory_name,
            namespace=namespace,
            name="get_recent_relationships",
            title="get recent relationships",
            description=(
                "List most recent relationship edges by valid_at/created_at, "
                "with optional filters."
            ),
            input_schema={
                "type": "object",
                "required": [],
                "additionalProperties": False,
                "properties": {
                    "limit": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": 1000,
                        "description": "Max number of relationships.",
                    },
                    "entity_id": {
                        "type": ["string", "null"],
                        "description": "Optional entity_id filter (source or target).",
                    },
                    "relationship_type": {
                        "type": ["string", "null"],
                        "description": "Optional relationship_type filter.",
                    },
                },
            },
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        limit: Optional[int] = 50,
        entity_id: Optional[str] = None,
        relationship_type: Optional[str] = None,
    ):
        if limit is None:
            limit = 50

        where_clauses = list[str]()
        if isinstance(entity_id, str) and entity_id.strip() != "":
            escaped_entity_id = _escape_query_value(entity_id.strip())
            where_clauses.append(
                "("
                f"r.source_entity_id = '{escaped_entity_id}' OR "
                f"r.target_entity_id = '{escaped_entity_id}'"
                ")"
            )
        if isinstance(relationship_type, str) and relationship_type.strip() != "":
            escaped_relationship_type = _escape_query_value(relationship_type.strip())
            where_clauses.append(f"r.relationship_type = '{escaped_relationship_type}'")

        where_clause = " AND ".join(where_clauses) if len(where_clauses) > 0 else None
        rows = await self._query_relationships(
            context=context,
            where_clause=where_clause,
            limit=limit,
        )
        return {
            "relationships": [
                *(_relationship_from_relationship_row(row) for row in rows)
            ]
        }


class GetMemoriesTool(_MemoriesTool):
    def __init__(self, *, memory_name: str, namespace: Optional[list[str]] = None):
        super().__init__(
            memory_name=memory_name,
            namespace=namespace,
            name="get_memories",
            title="get memories",
            description="List memory entities.",
            input_schema={
                "type": "object",
                "required": [],
                "additionalProperties": False,
                "properties": {
                    "limit": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": 1000,
                        "description": "Max number of results.",
                    },
                    "entity_type": {
                        "type": ["string", "null"],
                        "description": "Filter by entity_type. Defaults to MEMORY.",
                    },
                },
            },
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        limit: Optional[int] = 50,
        entity_type: Optional[str] = "MEMORY",
    ):
        if limit is None:
            limit = 50
        if entity_type is None:
            entity_type = "MEMORY"

        where_clause = None
        if isinstance(entity_type, str) and entity_type.strip() != "":
            escaped = _escape_query_value(entity_type.strip())
            where_clause = f"e.entity_type = '{escaped}'"

        rows = await self._query_entities(
            context=context,
            where_clause=where_clause,
            limit=limit,
        )
        return {"memories": [*(_entity_from_entity_row(row) for row in rows)]}


class GetEntityTool(_MemoriesTool):
    def __init__(self, *, memory_name: str, namespace: Optional[list[str]] = None):
        super().__init__(
            memory_name=memory_name,
            namespace=namespace,
            name="get_entity",
            title="get entity",
            description="Get one entity by entity_id.",
            input_schema={
                "type": "object",
                "required": ["entity_id"],
                "additionalProperties": False,
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "Target entity_id.",
                    }
                },
            },
        )

    async def execute(self, context: ToolContext, *, entity_id: str):
        escaped_entity_id = _escape_query_value(entity_id)
        rows = await self._query_entities(
            context=context,
            where_clause=f"e.entity_id = '{escaped_entity_id}'",
            limit=1,
        )
        if len(rows) == 0:
            return {"entity": None}
        return {"entity": _entity_from_entity_row(rows[0])}


class DeleteEntityTool(_MemoriesTool):
    def __init__(self, *, memory_name: str, namespace: Optional[list[str]] = None):
        super().__init__(
            memory_name=memory_name,
            namespace=namespace,
            name="delete_entity",
            title="delete entity",
            description="Delete one entity by entity_id returned by search_memories.",
            input_schema={
                "type": "object",
                "required": ["entity_id"],
                "additionalProperties": False,
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "Target entity_id.",
                    }
                },
            },
        )

    async def execute(self, context: ToolContext, *, entity_id: str):
        await self._ensure_memory_location(context)
        result = await context.room.memory.delete_entities(
            name=self.memory_name,
            namespace=self._memory_namespace(),
            entity_ids=[entity_id],
        )
        return {
            "deleted": result.deleted_entities > 0,
            "entity_id": entity_id,
            "deleted_entities": result.deleted_entities,
            "deleted_relationships": result.deleted_relationships,
        }


class DeleteRelationshipTool(_MemoriesTool):
    def __init__(self, *, memory_name: str, namespace: Optional[list[str]] = None):
        super().__init__(
            memory_name=memory_name,
            namespace=namespace,
            name="delete_relationship",
            title="delete relationship",
            description="Delete one relationship by source and target entity IDs.",
            input_schema={
                "type": "object",
                "required": ["source_entity_id", "target_entity_id"],
                "additionalProperties": False,
                "properties": {
                    "source_entity_id": {
                        "type": "string",
                        "description": "Source entity_id.",
                    },
                    "target_entity_id": {
                        "type": "string",
                        "description": "Target entity_id.",
                    },
                    "relationship_type": {
                        "type": ["string", "null"],
                        "description": "Optional relationship type. If null, delete all relationships between source and target.",
                    },
                },
            },
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        source_entity_id: str,
        target_entity_id: str,
        relationship_type: Optional[str] = None,
    ):
        await self._ensure_memory_location(context)
        normalized_relationship_type = (
            relationship_type.strip()
            if isinstance(relationship_type, str) and relationship_type.strip() != ""
            else None
        )
        result = await context.room.memory.delete_relationships(
            name=self.memory_name,
            namespace=self._memory_namespace(),
            relationships=[
                MemoryRelationshipSelector(
                    source_entity_id=source_entity_id,
                    target_entity_id=target_entity_id,
                    relationship_type=normalized_relationship_type,
                )
            ],
        )
        return {
            "deleted": result.deleted_relationships > 0,
            "source_entity_id": source_entity_id,
            "target_entity_id": target_entity_id,
            "relationship_type": normalized_relationship_type,
            "deleted_relationships": result.deleted_relationships,
        }


class DeleteAllMemoriesTool(_MemoriesTool):
    def __init__(self, *, memory_name: str, namespace: Optional[list[str]] = None):
        super().__init__(
            memory_name=memory_name,
            namespace=namespace,
            name="delete_all_memories",
            title="delete all memories",
            description="Delete and recreate the configured memory graph.",
            input_schema={
                "type": "object",
                "required": ["confirm"],
                "additionalProperties": False,
                "properties": {
                    "confirm": {
                        "type": "boolean",
                        "description": "Must be true to clear all memories.",
                    }
                },
            },
        )

    async def execute(self, context: ToolContext, *, confirm: bool):
        if not confirm:
            raise ValueError("set confirm=true to delete all memories")

        await context.room.memory.drop(
            name=self.memory_name,
            namespace=self._memory_namespace(),
            ignore_missing=True,
        )
        await context.room.memory.create(
            name=self.memory_name,
            namespace=self._memory_namespace(),
            overwrite=True,
        )
        return {"ok": True, "name": self.memory_name}


class DeleteEntitiesTool(_MemoriesTool):
    def __init__(self, *, memory_name: str, namespace: Optional[list[str]] = None):
        super().__init__(
            memory_name=memory_name,
            namespace=namespace,
            name="delete_entities",
            title="delete entities",
            description="Delete multiple entities by entity_id.",
            input_schema={
                "type": "object",
                "required": ["entity_ids"],
                "additionalProperties": False,
                "properties": {
                    "entity_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                        "description": "List of entity_ids to delete.",
                    }
                },
            },
        )

    async def execute(self, context: ToolContext, *, entity_ids: list[str]):
        await self._ensure_memory_location(context)
        result = await context.room.memory.delete_entities(
            name=self.memory_name,
            namespace=self._memory_namespace(),
            entity_ids=entity_ids,
        )
        deleted = [{"entity_id": entity_id} for entity_id in entity_ids]

        return {
            "deleted": deleted,
            "deleted_entities": result.deleted_entities,
            "deleted_relationships": result.deleted_relationships,
        }


class ListEntitiesTool(_MemoriesTool):
    def __init__(self, *, memory_name: str, namespace: Optional[list[str]] = None):
        super().__init__(
            memory_name=memory_name,
            namespace=namespace,
            name="list_entities",
            title="list entities",
            description="List entities in the memory graph.",
            input_schema={
                "type": "object",
                "required": [],
                "additionalProperties": False,
                "properties": {
                    "limit": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": 1000,
                        "description": "Max number of entities.",
                    },
                    "entity_type": {
                        "type": ["string", "null"],
                        "description": "Optional entity_type filter.",
                    },
                },
            },
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        limit: Optional[int] = 100,
        entity_type: Optional[str] = None,
    ):
        if limit is None:
            limit = 100

        where_clause = None
        if isinstance(entity_type, str) and entity_type.strip() != "":
            escaped = _escape_query_value(entity_type.strip())
            where_clause = f"e.entity_type = '{escaped}'"

        rows = await self._query_entities(
            context=context,
            where_clause=where_clause,
            limit=limit,
        )
        return {"entities": [*(_entity_from_entity_row(row) for row in rows)]}


class MemoriesToolkit(RemoteToolkit):
    def __init__(
        self,
        *,
        memory_name: str = "graph",
        namespace: Optional[list[str]] = None,
        llm_model: Optional[str] = None,
        llm_temperature: Optional[float] = None,
        search_limit: int = 20,
    ):
        super().__init__(
            name="memories",
            title="memories",
            description="Mem0-style memory tools backed by room.memory.",
            tools=[
                AddMemoryTool(
                    memory_name=memory_name,
                    namespace=namespace,
                    llm_model=llm_model,
                    llm_temperature=llm_temperature,
                ),
                SearchMemoriesTool(
                    memory_name=memory_name,
                    namespace=namespace,
                    recall_limit=search_limit,
                ),
                GetRecentMemoriesTool(memory_name=memory_name, namespace=namespace),
                GetRecentRelationshipsTool(
                    memory_name=memory_name, namespace=namespace
                ),
                GetEntityTool(memory_name=memory_name, namespace=namespace),
                DeleteEntityTool(memory_name=memory_name, namespace=namespace),
                DeleteRelationshipTool(memory_name=memory_name, namespace=namespace),
                DeleteAllMemoriesTool(memory_name=memory_name, namespace=namespace),
            ],
        )


class MemoriesToolkitConfig(ToolkitConfig):
    name: Literal["memories"] = "memories"
    memory_name: str = "graph"
    namespace: Optional[list[str]] = None
    llm_model: Optional[str] = None
    llm_temperature: Optional[float] = None
    search_limit: int = 20


class MemoriesToolkitBuilder(ToolkitBuilder):
    def __init__(self):
        super().__init__(name="memories", type=MemoriesToolkitConfig)

    async def make(
        self, *, room: RoomClient, model: str, config: MemoriesToolkitConfig
    ) -> Toolkit:
        del room
        del model
        return MemoriesToolkit(
            memory_name=config.memory_name,
            namespace=config.namespace,
            llm_model=config.llm_model,
            llm_temperature=config.llm_temperature,
            search_limit=config.search_limit,
        )
