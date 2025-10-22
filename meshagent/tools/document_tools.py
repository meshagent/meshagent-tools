from meshagent.api.messaging import TextResponse, JsonResponse

from .tool import (
    Tool,
    ToolContext,
)

from .toolkit import Toolkit

from meshagent.api.schema import MeshSchema, ElementType, ChildProperty
from meshagent.api import RoomException
from meshagent.api.schema_util import merge
import logging

logger = logging.getLogger("document_tools")


class RootInsertTool(Tool):
    def __init__(self, *, document_type: str, schema: MeshSchema, element: ElementType):
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
            name=f"{document_type}_insert_{tag_name}",
            title=f"insert {tag_name} into a {document_type} (only allowed for documents with .{document_type} extension)",
            description=f"insert a {tag_name} element at the root",
            input_schema=input_schema,
            defs=full_schema["$defs"],
        )

    async def execute(self, *, context: ToolContext, path: str, **kwargs):
        documents = context.room.sync.get_open_documents()

        if path not in documents:
            raise RoomException(f"the document is not currently open: {path}")

        document = documents[path]
        element = document.root.append_json(kwargs)
        return TextResponse(text=f"The content was inserted with the id: {element.id}")


class RemoveElementTool(Tool):
    def __init__(self):
        super().__init__(
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
        documents = context.room.sync.get_open_documents()

        if path not in documents:
            raise RoomException(f"the document is not currently open: {path}")

        document = documents[path]

        node = document.root.get_node_by_id(id)
        if node is None:
            return TextResponse(text="there was no matching node")
        else:
            node.delete()
            return TextResponse(text="the node was deleted")


class SetAttributeTool(Tool):
    def __init__(self):
        super().__init__(
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
        documents = context.room.sync.get_open_documents()

        if path not in documents:
            raise RoomException(f"the document is not currently open: {path}")

        document = documents[path]

        node = document.root.get_node_by_id(id)
        if node is None:
            return TextResponse(text="there was no matching node")
        else:
            node[attribute_name] = attribute_value
            return TextResponse(text="the node was updated")


class GetDocumentJSONTool(Tool):
    def __init__(self):
        super().__init__(
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
        documents = context.room.sync.get_open_documents()

        if path not in documents:
            raise RoomException(f"the document is not currently open: {path}")

        document = documents[path]

        return JsonResponse(json=document.root.to_json(include_ids=True))


def build_tools(schema: MeshSchema, document_type: str):
    tools = list[Tool]()

    for prop in schema.root.properties:
        if isinstance(prop, ChildProperty):
            child_type: ChildProperty = prop
            for tag_name in child_type.child_tag_names:
                element = schema.elements_by_tag_name[tag_name]
                tools.append(
                    RootInsertTool(
                        document_type=document_type, schema=schema, element=element
                    )
                )

    return tools


class DocumentOpenTool(Tool):
    def __init__(self):
        super().__init__(
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
        documents = context.room.sync.get_open_documents()

        if path in documents:
            raise RoomException(f"the document is already open: {path}")

        document = await context.room.sync.open(path=path)
        documents[path] = document

        return None


class DocumentCloseTool(Tool):
    def __init__(self):
        super().__init__(
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
        documents = context.room.sync.get_open_documents()

        if path not in documents:
            raise RoomException(f"the document is not open: {path}")

        if path in documents:
            await context.room.sync.close(path=path)
            documents.pop(path)

        return None


class DocumentAuthoringToolkit(Toolkit):
    def __init__(
        self,
        *,
        name: str = "meshagent.document_authoring",
        description: str = "Tools for interacting with meshdocuments",
        title: str = "meshdocument core",
    ):
        super().__init__(
            name=name,
            title=title,
            description=description,
            tools=[
                RemoveElementTool(),
                SetAttributeTool(),
                GetDocumentJSONTool(),
                DocumentOpenTool(),
                DocumentCloseTool(),
            ],
        )


class DocumentTypeAuthoringToolkit(Toolkit):
    def __init__(
        self,
        *,
        schema: MeshSchema,
        document_type: str,
    ):
        name: str = f"meshagent.document_authoring.{document_type}"
        description: str = f"tools for interacting with a .{document_type} meshdocument"
        title: str = f"{document_type}"

        super().__init__(
            name=name,
            title=title,
            description=description,
            tools=[*build_tools(schema=schema, document_type=document_type)],
        )
