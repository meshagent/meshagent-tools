from meshagent.tools.toolkit import Toolkit, Tool, TextResponse, ToolContext
from aiohttp import web, ClientSession

class WebToolkit(Toolkit):
    def __init__(self):
        super().__init__(tools=[
           WebFetchTool()
        ])
    

class WebFetchTool(Tool):
    def __init__(self):
        super().__init__(
            name="get_web_page_text",
            title="get web page text",
            description="gets the text of a web page",
            input_schema={
                "type": "object",
                "properties" : {
                    "url" : { 
                        "type": "string",
                        "description" : "the url of the web page (always start it with a proper scheme like https://)"
                    }
                },
                "additionalProperties" : False,
            })
        
    async def execute(self,
        *,
        context: ToolContext,
        url: str,
    ) -> str:
        async with ClientSession() as session:
            async with session.get(url) as resp:
                body = await resp.text()

                from bs4 import BeautifulSoup
                soup = BeautifulSoup(body, "html.parser")
                return TextResponse(text=soup.get_text())
