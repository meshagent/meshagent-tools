from pydantic import BaseModel


class ToolkitConfig(BaseModel):
    name: str
