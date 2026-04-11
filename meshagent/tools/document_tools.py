from meshagent.api.messaging import TextContent, JsonContent

from .tool import (
    FunctionTool,
    LocalRoomTool,
    ToolContext,
)

from .toolkit import Toolkit

from meshagent.api.schema import MeshSchema, ElementType, ChildProperty
from meshagent.api import RoomClient, RoomException
from meshagent.api.schema_util import merge
import logging

logger = logging.getLogger("document_tools")


class _DocumentTool(LocalRoomTool):
    pass


class RootInsertTool(_DocumentTool):
    def __init__(
        self,
        *,
        room: RoomClient,
        document_type: str,
        schema: MeshSchema,
        element: ElementType,
    ):
        self.element = element
        full_schema = schema.to_json()
        input_schema = merge(
            schema={
                **element.to_json(),
            },
            additional_properties={
                "path": {
                    "type": "string",
                    "description": "the path of the document to insert the element into",
                }
            },
        )

        tag_name = element.tag_name

        super().__init__(
            room=room,
            name=f"{document_type}_insert_root_{tag_name}",
            title=f"insert {tag_name} into a {document_type} (only allowed for documents with .{document_type} extension)",
            description=f"insert a {tag_name} element at the root",
            input_schema=input_schema,
            defs=full_schema["$defs"],
        )

    async def execute(self, *, context: ToolContext, path: str, **kwargs):
        del context
        documents = self.room.sync.get_open_documents()

        if path not in documents:
            raise RoomException(f"the document is not currently open: {path}")

        document = documents[path]
        element = document.root.append_json(kwargs)
        return TextContent(text=f"The content was inserted with the id: {element.id}")


class ElementInsertTool(_DocumentTool):
    def __init__(
        self,
        *,
        room: RoomClient,
        document_type: str,
        schema: MeshSchema,
        element: ElementType,
    ):
        self.element = element

        tag_name = element.tag_name

        cloned_element = element.from_json(element.to_json())

        # remove the child props, those can be inserted in another call

        child_props = []
        for prop in cloned_element.properties:
            if isinstance(prop, ChildProperty):
                child_props.append(prop)

        for cp in child_props:
            cloned_element.properties.remove(cp)

        input_schema = merge(
            schema={
                **cloned_element.to_json(),
            },
            additional_properties={
                "path": {
                    "type": "string",
                    "description": "the id of a parent element to insert the node under",
                },
                "parent_element_id": {
                    "type": "string",
                    "description": "the path of the document to insert the element into",
                },
            },
        )

        super().__init__(
            room=room,
            name=f"{document_type}_insert_{tag_name}",
            title=f"insert {tag_name} into a {document_type} (only allowed for documents with .{document_type} extension)",
            description=f"insert a {tag_name} element at the root",
            input_schema=input_schema,
        )

    async def execute(
        self, *, context: ToolContext, path: str, parent_element_id: str, **kwargs
    ):
        del context
        documents = self.room.sync.get_open_documents()

        if path not in documents:
            raise RoomException(f"the document is not currently open: {path}")

        document = documents[path]
        if parent_element_id == "root":
            element = document.root.append_json(kwargs)
        else:
            element = document.root.get_node_by_id(parent_element_id).append_json(
                kwargs
            )
        return TextContent(text=f"The content was inserted with the id: {element.id}")


class RemoveElementTool(_DocumentTool):
    def __init__(self, *, room: RoomClient):
        super().__init__(
            room=room,
            name="remove_element_by_id",
            title="remove element by id",
            description="remove an element by its id",
            input_schema=merge(
                schema={
                    "type": "object",
                    "required": ["id"],
                    "additionalProperties": False,
                    "properties": {
                        "id": {
                            "type": "string",
                        }
                    },
                },
                additional_properties={
                    "path": {
                        "type": "string",
                    }
                },
            ),
        )

    async def execute(self, *, context: ToolContext, path: str, id: str):
        del context
        documents = self.room.sync.get_open_documents()

        if path not in documents:
            raise RoomException(f"the document is not currently open: {path}")

        document = documents[path]

        node = document.root.get_node_by_id(id)
        if node is None:
            return TextContent(text="there was no matching node")
        else:
            node.delete()
            return TextContent(text="the node was deleted")


class SetAttributeTool(_DocumentTool):
    def __init__(self, *, room: RoomClient):
        super().__init__(
            room=room,
            name="set_attribute",
            title="set attribute",
            description="update an element by its id",
            input_schema=merge(
                schema={
                    "type": "object",
                    "required": ["id", "attribute_name", "attribute_value"],
                    "additionalProperties": False,
                    "properties": {
                        "id": {
                            "type": "string",
                        },
                        "attribute_name": {
                            "type": "string",
                        },
                        "attribute_value": {"type": "string"},
                    },
                },
                additional_properties={"path": {"type": "string"}},
            ),
        )

    async def execute(
        self,
        *,
        context: ToolContext,
        path: str,
        id: str,
        attribute_name: str,
        attribute_value,
    ):
        del context
        documents = self.room.sync.get_open_documents()

        if path not in documents:
            raise RoomException(f"the document is not currently open: {path}")

        document = documents[path]

        node = document.root.get_node_by_id(id)
        if node is None:
            return TextContent(text="there was no matching node")
        else:
            node[attribute_name] = attribute_value
            return TextContent(text="the node was updated")


class GetDocumentJSONTool(_DocumentTool):
    def __init__(self, *, room: RoomClient):
        super().__init__(
            room=room,
            name="get_document",
            title="get document as JSON",
            description="get the document elements converted to JSON",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
                "required": ["path"],
            },
        )

    async def execute(self, *, context: ToolContext, path: str, **kwargs):
        del context
        documents = self.room.sync.get_open_documents()

        if path not in documents:
            raise RoomException(f"the document is not currently open: {path}")

        document = documents[path]

        return JsonContent(json=document.root.to_json(include_ids=True))


def build_tools(*, room: RoomClient, schema: MeshSchema, document_type: str):
    tools = list[FunctionTool]()

    # for prop in schema.root.properties:
    #    if isinstance(prop, ChildProperty):
    #        child_type: ChildProperty = prop
    #        for tag_name in child_type.child_tag_names:
    #            element = schema.elements_by_tag_name[tag_name]
    #            tools.append(
    #                RootInsertTool(
    #                    document_type=document_type, schema=schema, element=element
    #                )
    #            )

    insert_tools = []
    for element in schema.elements:
        for prop in element.properties:
            if isinstance(prop, ChildProperty):
                child_type: ChildProperty = prop
                for tag_name in child_type.child_tag_names:
                    element = schema.elements_by_tag_name[tag_name]
                    if tag_name not in insert_tools:
                        insert_tools.append(tag_name)
                        tools.append(
                            ElementInsertTool(
                                room=room,
                                document_type=document_type,
                                schema=schema,
                                element=element,
                            )
                        )

    return tools


class DocumentOpenTool(_DocumentTool):
    def __init__(self, *, room: RoomClient):
        super().__init__(
            room=room,
            name="meshagent.document.open",
            input_schema={
                "type": "object",
                "required": ["path"],
                "additionalProperties": False,
                "properties": {"path": {"type": "string"}},
            },
            title="open a mesh document for writing or reading, makes additional tools available for interacting with the document (only for meshdocuments, does not work with pdfs, office docs, txt files, or images)",
            description="open a mesh document",
            rules=[],
        )

    async def execute(self, context: ToolContext, path: str):
        del context
        documents = self.room.sync.get_open_documents()

        if path in documents:
            raise RoomException(f"the document is already open: {path}")

        document = await self.room.sync.open(path=path)
        documents[path] = document

        return None


class DocumentCloseTool(_DocumentTool):
    def __init__(self, *, room: RoomClient):
        super().__init__(
            room=room,
            name="meshagent.document.close",
            input_schema={
                "type": "object",
                "required": ["path"],
                "additionalProperties": False,
                "properties": {"path": {"type": "string"}},
            },
            title="close a mesh document",
            description="close a mesh document, it can no longer be read from or written to until it is opened again",
            rules=[],
        )

    async def execute(self, context: ToolContext, path: str):
        del context
        documents = self.room.sync.get_open_documents()

        if path not in documents:
            raise RoomException(f"the document is not open: {path}")

        if path in documents:
            await self.room.sync.close(path=path)
            documents.pop(path)

        return None


class DocumentAuthoringToolkit(Toolkit):
    def __init__(
        self,
        *,
        name: str = "meshagent.document_authoring",
        description: str = "Tools for interacting with meshdocuments",
        title: str = "meshdocument core",
        room: RoomClient,
    ):
        super().__init__(
            name=name,
            title=title,
            description=description,
            room=room,
            tools=[
                RemoveElementTool(room=room),
                SetAttributeTool(room=room),
                GetDocumentJSONTool(room=room),
                DocumentOpenTool(room=room),
                DocumentCloseTool(room=room),
            ],
        )


class DocumentTypeAuthoringToolkit(Toolkit):
    def __init__(
        self,
        *,
        schema: MeshSchema,
        document_type: str,
        room: RoomClient,
    ):
        name: str = f"meshagent.document_authoring.{document_type}"
        description: str = f"tools for interacting with a .{document_type} meshdocument"
        title: str = f"{document_type}"

        super().__init__(
            name=name,
            title=title,
            description=description,
            room=room,
            tools=[*build_tools(room=room, schema=schema, document_type=document_type)],
        )
