from .toolkit import Tool, ToolContext, FileResponse, JsonResponse, LinkResponse, Toolkit
from .blob import BlobStorage, get_bytes_from_url
import os
from meshagent.api import RoomException
from typing import Optional

class DownloadFileTool(Tool):
    def __init__(self):
        super().__init__(
            name="get_file_download_url",
            title="get file download url",
            description="get a url that can be used to download a file in the room",
            input_schema={
                "type" : "object",
                "additionalProperties" : False,
                "required" : ["path"],
                "properties" : {
                    "path" : { "type" : "string", "description" : "the full path of the file" }
                }
            }
        )

    async def execute(self, *, context: ToolContext, path: str):
        name = os.path.basename(path)
        url = await context.room.storage.download_url(path=path)
        return LinkResponse(
            name=name,
            url=url
        )
 
class ListFilesTool(Tool):
    def __init__(self):
        super().__init__(
            name="list_files_in_room",
            title="list files in room",
            description="list the files at a specific path in the room",
            input_schema={
                "type" : "object",
                "additionalProperties" : False,
                "required" : ["path"],
                "properties" : {
                    "path" : { "type" : "string" }
                }
            }
        )

    async def execute(self, *, context: ToolContext, path: str):
        files = await context.room.storage.list(path=path)
        return JsonResponse(
            json={ "files" : list(map(vars, files)) }
        )

class SaveFileFromUrlTool(Tool):
    def __init__(self, blob_storage: Optional[BlobStorage] = None):
        super().__init__(
            name="save_file_from_url",
            title="save file from url",
            description="save a file from a url to a path in the room",
            input_schema={
                "type" : "object",
                "additionalProperties" : False,
                "required" : ["url","path","overwrite"],
                "properties" : {
                    "url" : { "type" : "string", "description" : "the url of a file that should be saved to the room" },
                    "path" : { "type" : "string", "description" : "the destination path (including the filename)"},
                    "overwrite" : { "type" : "boolean", "description" : "whether to overwrite the existing file) (default false)"}
                }
            }
        )
        self.blob_storage = blob_storage

    async def execute(self, *, context: ToolContext, url: str, path: str, overwrite: bool):

        blob = await get_bytes_from_url(url=url, blob_storage=self.blob_storage)
        
        if overwrite == False:
            result = context.room.storage.exists(path=path)
            if result == True:
                raise RoomException(f"a file already exists at the path: {path}, try another filename")

        handle = await context.room.storage.open(path=path, overwrite=overwrite)
        try:
            await context.room.storage.write(handle=handle, data=blob.data)
        finally:
            await context.room.storage.close(handle=handle)

class StorageToolkit(Toolkit):
    def __init__(self, *, blob_storage: Optional[BlobStorage] = None):
        super().__init__(
            name="meshagent.storage",
            title="storage",
            description="tools for interacting with meshagent room storage",
            tools=[
                ListFilesTool(),
                DownloadFileTool(),
                SaveFileFromUrlTool(blob_storage=blob_storage),
            ],
        )