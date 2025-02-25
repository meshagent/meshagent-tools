from uuid import uuid4
import base64
import aiohttp
from meshagent.api import RoomException
from typing import Optional

class Blob:
    def __init__(self, *, mime_type: str, data: bytes):
        self._mime_type = mime_type
        self._data = data
    
    @property
    def mime_type(self) -> str:
        return self._mime_type

    @property
    def data(self) -> bytes:
        return self._data
    
class BlobStorage:
    def __init__(self):
        self._blobs = dict[str, Blob]()

    def store(self, blob: Blob) -> str:
        uri = f"blob://{str(uuid4())}"
        self._blobs[uri] = blob
        return uri
    
    def remove(self, uri: str):
        self._blobs.pop(uri)
    
    def get(self, uri: str) -> Blob:
        return self._blobs[uri]
    


async def get_bytes_from_url(*, url: str, blob_storage: Optional[BlobStorage] = None) -> Blob:
    if url.startswith("data:"):
        parts = url.split(',', 1)
        #mime_type = None
        #header = parts[0]
        #if ";base64" in header:
        #    mime_type = header.split(";")[0].split(":")[1]

        # Decode the base64 data
        mime_type = parts[0]
        content = base64.b64decode(parts[1])
        #extension = mimetypes.guess_extension(mime_type)
        #file_name = str(uuid.uuid4())+extension
        return Blob(mime_type=mime_type, data=content)
    elif url.startswith("blob:"):
        if blob_storage == None:
            raise RoomException("blob storage is not available for this call")
        
        blob = blob_storage.get(url)
        return blob

    else: 
        async with aiohttp.ClientSession() as session:
            async with session.get(url=url) as response:
                content = await response.content.read()
                return Blob(mime_type=response.content_type, data=content)