## MeshAgent Tools
The ``meshagent.tools`` package bundles reusable tool and toolkit abstractions plus a set of out of the box MeshAgent toolkits. 

### ToolContext and BaseTool
The ``ToolContext`` tracks the room, caller, and optional "on-behalf-of" participant. The ``BaseTool`` defines metadata used by all tools such as name and description. 

### Tool and Toolkit
A ``Tool`` encapsulates a single operation with an input JSON schema. Each tool implements an ``execute`` function where you define the logic for the tool. The ``Toolkit`` groups tools together and can enforce rules or descriptions.

### Response Types
Response types specify the output that a tool should return. This helps the tool and agent know how to handle the response appropriately. Response types include: ``JsonResponse``, ``TextResponse``, and ``FileResponse``.

```Python Python
from meshagent.tools import Tool, Toolkit, ToolContext
from meshagent.api.messaging import TextResponse

class MyNewTool(Tool):
    def __init__(self):
        super().__init__(
            name="my_new_tool",
            title="A sample tool", 
            description="The tool skeleton",
            input_schema={
                "type":"object",
                "additionalProperties": False,
                "required": [...],
                "properties": {...}
            }
        )
    async def execute(self, ctx:ToolContext, sample_parameter:str):
        # tool logic
        return TextResponse(text="Tool logic complete")
    
class MyNewToolkit(Toolkit):
    def __init__(self):
        super().__init__(
            name="my_new_toolkit", 
            title="An example toolkit", 
            description="The toolkit skeleton", 
            tools=[MyNewTool])


```

### Built-in Toolkits
Some of the built-in MeshAgent toolkits include: 
- ``StorageToolkit``: Provides file operations (read, write, list, etc.)
- ``DocumentAuthoringToolkit``: Defines tools for manipulating Mesh documents (create document, add element, remove element, etc.)

---
### Learn more about MeshAgent on our website or check out the docs for additional examples!

**Website**: [www.meshagent.com](https://www.meshagent.com/)

**Documentation**: [docs.meshagent.com](https://docs.meshagent.com/)

---