from copy import deepcopy
from .toolkit import Tool, Toolkit, FileResponse, JsonResponse, Response
import asyncio

class MultiTool(Tool):
    def __init__(self, *, tools: list[Tool], name = None, title = None, description = None, rules = None, thumbnail_url = None):
       
        self.tools = tools
        required = []
        defs = None
        properties = {}

        self._subtools = dict[str, Tool]()


        for tool in tools:
        
            if name == None:
                name = tool.name

            if title == None:
                title = tool.title

            if description == None:
                description = tool.description

            if thumbnail_url == None:
                thumbnail_url = tool.thumbnail_url

            if tool.rules != None:
                if rules == None:
                    rules = []
                
                rules.extend(tool.rules)
            
            prop_name = tool.name

            required.append(prop_name)

            self._subtools[prop_name] = tool

            properties[prop_name] = tool.input_schema
            if "description" not in properties[prop_name] and tool.description:
                properties[prop_name]["description"] = tool.description

            if tool.defs != None:
            
                for k, v in tool.defs.items():

                    if defs == None:
                        defs = {}

                    if k in defs:
                        raise Exception("Duplicate defs found, unable to merge")
                    
                    defs[k] = v

                
        input_schema = {
            "additionalProperties" : False,
            "type" : "object",
            "required" : required,
            "properties" : properties
        }

        super().__init__(name=name,  title=title, description=description, rules=rules, thumbnail_url=thumbnail_url, input_schema=input_schema, defs=defs)

    async def execute(self, context, **kwargs):

        calls = []
        results = dict[str, Response]()


        async def call_subtool(k, args):
            results[k] = await self._subtools[k].execute(context=context, **args)
            
        for k, v in kwargs.items():


            calls.append(call_subtool(k, v))

        output = {}
        usage = {}

        await asyncio.gather(*calls)

        for k, v in kwargs.items():

            result = results[k]

            # aggregate usage values
            if isinstance(result, Response):

                if result.usage != None:

                    for usage_key, usage_value in result.usage.items():
                        
                        usage[usage_key] = usage.get(usage_key, 0) + usage_value
            
            
            if isinstance(result, FileResponse):
                return result
            
            elif isinstance(result, Response):
                output[k] = result.to_json()
            
            else:
                output[k] = result

        return JsonResponse(json=output, usage=usage)

    
    
# MultiToolkit can be used to combine multiple tools into one, this can be useful if you want to make sure that every tool call also produces a toast for example
class MultiToolkit(Toolkit):

    def __init__(self, *, required: list[Tool], base_toolkit: Toolkit):
    
        tools = list(map(
            lambda tool: MultiTool(
                tools=[ 
                    tool,
                    *required,
                ]),
            base_toolkit.tools
        ))

        super().__init__(name=base_toolkit.name, tools=tools, rules=base_toolkit.rules, title=base_toolkit.title, description=base_toolkit.description, thumbnail_url=base_toolkit.thumbnail_url)


