import asyncio
import json
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict

from meshagent.api.messaging import JsonResponse, LinkResponse, FileResponse
from meshagent.api import RoomClient, RoomException
from meshagent.api.room_server_client import StorageEntry

from .config import ToolkitConfig
from .tool import Tool
from .toolkit import ToolContext, ToolkitBuilder
from .hosting import RemoteToolkit, Toolkit
from .blob import get_bytes_from_url


class StorageToolMount(BaseModel):
    path: str
    read_only: bool = False

    model_config = ConfigDict(extra="forbid")

    def _ensure_writable(self, path: str) -> None:
        if self.read_only:
            raise RoomException(f"storage mount is read-only: {path}")

    async def read_file(
        self,
        *,
        context: ToolContext,
        resolved: "_ResolvedStoragePath",
        path: str,
    ) -> FileResponse:
        raise NotImplementedError

    async def write_text(
        self,
        *,
        context: ToolContext,
        resolved: "_ResolvedStoragePath",
        path: str,
        text: str,
        overwrite: bool,
    ) -> None:
        raise NotImplementedError

    async def write_bytes(
        self,
        *,
        context: ToolContext,
        resolved: "_ResolvedStoragePath",
        path: str,
        data: bytes,
        overwrite: bool,
    ) -> None:
        raise NotImplementedError

    async def list_entries(
        self, *, context: ToolContext, resolved: "_ResolvedStoragePath"
    ) -> list[StorageEntry]:
        raise NotImplementedError

    async def get_download_url(
        self, *, context: ToolContext, resolved: "_ResolvedStoragePath", path: str
    ) -> LinkResponse:
        raise NotImplementedError


class StorageToolLocalMount(StorageToolMount):
    local_path: str

    async def read_file(
        self,
        *,
        context: ToolContext,
        resolved: "_ResolvedStoragePath",
        path: str,
    ) -> FileResponse:
        local_path = _require_local_path(resolved)
        filename = os.path.basename(path)
        mime_type, _ = mimetypes.guess_type(local_path)
        if mime_type is None:
            mime_type = "application/octet-stream"
        data = await _read_local_file(local_path)
        return FileResponse(mime_type=mime_type, name=filename, data=data)

    async def write_text(
        self,
        *,
        context: ToolContext,
        resolved: "_ResolvedStoragePath",
        path: str,
        text: str,
        overwrite: bool,
    ) -> None:
        self._ensure_writable(path)
        local_path = _require_local_path(resolved)
        await _write_local_text(local_path, text, overwrite)

    async def write_bytes(
        self,
        *,
        context: ToolContext,
        resolved: "_ResolvedStoragePath",
        path: str,
        data: bytes,
        overwrite: bool,
    ) -> None:
        self._ensure_writable(path)
        local_path = _require_local_path(resolved)
        await _write_local_bytes(local_path, data, overwrite)

    async def list_entries(
        self, *, context: ToolContext, resolved: "_ResolvedStoragePath"
    ) -> list[StorageEntry]:
        local_path = _require_local_path(resolved)
        return await _list_local_entries(local_path)

    async def get_download_url(
        self, *, context: ToolContext, resolved: "_ResolvedStoragePath", path: str
    ) -> LinkResponse:
        local_path = _require_local_path(resolved)
        file_path = Path(local_path)
        if not file_path.exists():
            raise RoomException(f"file not found: {local_path}")
        if not file_path.is_file():
            raise RoomException(f"path is not a file: {local_path}")
        name = os.path.basename(path)
        return LinkResponse(name=name, url=file_path.resolve().as_uri())


class StorageToolRoomMount(StorageToolMount):
    subpath: Optional[str] = None

    async def read_file(
        self,
        *,
        context: ToolContext,
        resolved: "_ResolvedStoragePath",
        path: str,
    ) -> FileResponse:
        room_path = _require_room_path(resolved)
        filename = os.path.basename(path)
        _, extension = os.path.splitext(path)
        if extension:
            schema_path = _make_room_path(
                resolved.room_root, f".schemas/{extension.lstrip('.')}" + ".json"
            )
            if await context.room.storage.exists(path=schema_path):
                return FileResponse(
                    mime_type="application/json",
                    name=filename,
                    data=json.dumps(
                        await context.room.sync.describe(path=room_path)
                    ).encode(),
                )
        return await context.room.storage.download(path=room_path)

    async def write_text(
        self,
        *,
        context: ToolContext,
        resolved: "_ResolvedStoragePath",
        path: str,
        text: str,
        overwrite: bool,
    ) -> None:
        self._ensure_writable(path)
        room_path = _require_room_path(resolved)
        handle = await context.room.storage.open(path=room_path, overwrite=overwrite)
        await context.room.storage.write(handle=handle, data=text.encode("utf-8"))
        await context.room.storage.close(handle=handle)

    async def write_bytes(
        self,
        *,
        context: ToolContext,
        resolved: "_ResolvedStoragePath",
        path: str,
        data: bytes,
        overwrite: bool,
    ) -> None:
        self._ensure_writable(path)
        room_path = _require_room_path(resolved)
        if not overwrite:
            result = await context.room.storage.exists(path=room_path)
            if result:
                raise RoomException(
                    f"a file already exists at the path: {path}, try another filename"
                )
        handle = await context.room.storage.open(path=room_path, overwrite=overwrite)
        try:
            await context.room.storage.write(handle=handle, data=data)
        finally:
            await context.room.storage.close(handle=handle)

    async def list_entries(
        self, *, context: ToolContext, resolved: "_ResolvedStoragePath"
    ) -> list[StorageEntry]:
        room_path = _require_room_path(resolved)
        return await context.room.storage.list(path=room_path)

    async def get_download_url(
        self, *, context: ToolContext, resolved: "_ResolvedStoragePath", path: str
    ) -> LinkResponse:
        room_path = _require_room_path(resolved)
        name = os.path.basename(path)
        url = await context.room.storage.download_url(path=room_path)
        return LinkResponse(name=name, url=url)


@dataclass(frozen=True)
class _PreparedMount:
    mount: StorageToolMount
    virtual_path: str
    local_root: Optional[str]
    room_root: Optional[str]


@dataclass(frozen=True)
class _ResolvedStoragePath:
    mount: StorageToolMount
    virtual_path: str
    relative_path: str
    local_path: Optional[str]
    room_path: Optional[str]
    room_root: Optional[str]


def _normalize_mount_path(path: str) -> str:
    if path is None:
        raise RoomException("mount path must be set")

    cleaned = path.strip()
    if cleaned in ("", "/", "."):
        return "/"

    cleaned = cleaned.lstrip("/")
    if cleaned == "":
        return "/"

    parts = cleaned.split("/")
    if any(part in {".", ".."} for part in parts):
        raise RoomException(f"invalid mount path: {path}")

    return f"/{'/'.join(parts)}"


def _normalize_virtual_path(path: str) -> str:
    if path is None:
        raise RoomException("path must be set")

    cleaned = path.strip()
    if cleaned in ("", "."):
        return "/"

    cleaned = cleaned.lstrip("/")
    if cleaned == "":
        return "/"

    parts = cleaned.split("/")
    if any(part in {".", ".."} for part in parts):
        raise RoomException(f"dot segments not allowed: {path}")

    return f"/{'/'.join(parts)}"


def _normalize_room_root(subpath: Optional[str]) -> str:
    if subpath is None:
        return ""

    cleaned = subpath.strip()
    if cleaned in ("", "/", "."):
        return ""

    cleaned = cleaned.strip("/")
    parts = cleaned.split("/")
    if any(part in {".", ".."} for part in parts):
        raise RoomException(f"dot segments not allowed: {subpath}")

    return "/".join(parts)


def _prepare_mounts(
    mounts: Optional[list[StorageToolMount]],
) -> list[_PreparedMount]:
    if not mounts:
        mounts = [StorageToolRoomMount(path="/")]

    prepared = []
    for mount in mounts:
        virtual_path = _normalize_mount_path(mount.path)
        if isinstance(mount, StorageToolLocalMount):
            local_root = os.path.abspath(mount.local_path)
            room_root = None
        else:
            local_root = None
            room_root = _normalize_room_root(mount.subpath)

        prepared.append(
            _PreparedMount(
                mount=mount,
                virtual_path=virtual_path,
                local_root=local_root,
                room_root=room_root,
            )
        )

    return prepared


def _resolve_storage_path(
    mounts: list[_PreparedMount], path: str
) -> _ResolvedStoragePath:
    virtual_path = _normalize_virtual_path(path)

    matches = []
    for mount in mounts:
        if mount.virtual_path == "/":
            matches.append(mount)
            continue

        if virtual_path == mount.virtual_path or virtual_path.startswith(
            f"{mount.virtual_path}/"
        ):
            matches.append(mount)

    if not matches:
        raise RoomException(f"path is not within a storage mount: {path}")

    selected = max(matches, key=lambda m: len(m.virtual_path))
    relative_path = virtual_path[len(selected.virtual_path) :].lstrip("/")

    local_path = None
    room_path = None
    room_root = selected.room_root

    if selected.local_root is not None:
        local_path = os.path.normpath(os.path.join(selected.local_root, relative_path))
        if os.path.commonpath([selected.local_root, local_path]) != selected.local_root:
            raise RoomException(f"path escapes the storage mount: {path}")
    else:
        if room_root and relative_path:
            room_path = f"{room_root}/{relative_path}"
        elif room_root:
            room_path = room_root
        else:
            room_path = relative_path

    return _ResolvedStoragePath(
        mount=selected.mount,
        virtual_path=virtual_path,
        relative_path=relative_path,
        local_path=local_path,
        room_path=room_path,
        room_root=room_root,
    )


def _make_room_path(room_root: Optional[str], relative_path: str) -> str:
    room_root = room_root or ""
    relative_path = relative_path.lstrip("/")

    if room_root and relative_path:
        return f"{room_root}/{relative_path}"

    return room_root or relative_path


def _require_local_path(resolved: _ResolvedStoragePath) -> str:
    if resolved.local_path is None:
        raise RoomException("local path is not available for this mount")
    return resolved.local_path


def _require_room_path(resolved: _ResolvedStoragePath) -> str:
    if resolved.room_path is None:
        raise RoomException("room path is not available for this mount")
    return resolved.room_path


async def _read_local_file(path: str) -> bytes:
    def _read() -> bytes:
        return Path(path).read_bytes()

    try:
        return await asyncio.to_thread(_read)
    except FileNotFoundError:
        raise RoomException(f"file not found: {path}")
    except IsADirectoryError:
        raise RoomException(f"path is a directory: {path}")


async def _write_local_bytes(path: str, data: bytes, overwrite: bool) -> None:
    def _write() -> None:
        if not overwrite and os.path.exists(path):
            raise RoomException(
                f"a file already exists at the path: {path}, try another filename"
            )
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    await asyncio.to_thread(_write)


async def _write_local_text(path: str, text: str, overwrite: bool) -> None:
    def _write() -> None:
        if not overwrite and os.path.exists(path):
            raise RoomException(
                f"a file already exists at the path: {path}, try another filename"
            )
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    await asyncio.to_thread(_write)


async def _list_local_entries(path: str) -> list[StorageEntry]:
    def _list() -> list[StorageEntry]:
        try:
            entries = []
            for entry in os.scandir(path):
                stat_info = entry.stat()
                entries.append(
                    StorageEntry(
                        name=entry.name,
                        is_folder=entry.is_dir(),
                        created_at=datetime.fromtimestamp(
                            stat_info.st_ctime, tz=timezone.utc
                        ),
                        updated_at=datetime.fromtimestamp(
                            stat_info.st_mtime, tz=timezone.utc
                        ),
                    )
                )
            return entries
        except FileNotFoundError:
            return []
        except NotADirectoryError:
            return []

    return await asyncio.to_thread(_list)


class _StorageTool(Tool):
    def __init__(self, *, mounts: list[_PreparedMount], **kwargs):
        super().__init__(**kwargs)
        self._mounts = mounts

    def _resolve_path(self, path: str) -> _ResolvedStoragePath:
        return _resolve_storage_path(self._mounts, path)


class ReadFileTool(_StorageTool):
    def __init__(self, *, mounts: list[_PreparedMount]):
        super().__init__(
            name="read_file",
            title="read a file file",
            description="read the contents of a file",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "the full path of the file",
                    }
                },
            },
            mounts=mounts,
        )

    async def execute(self, context: ToolContext, **kwargs):
        path = kwargs["path"]
        resolved = self._resolve_path(path)
        return await resolved.mount.read_file(
            context=context, resolved=resolved, path=path
        )


class WriteFileTool(_StorageTool):
    def __init__(self, *, mounts: list[_PreparedMount]):
        super().__init__(
            name="write_file",
            title="write text file",
            description="write the contents of a text file (for example a .txt file or a source code file). Will not work with binary files.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "text", "overwrite"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "the full path of the file",
                    },
                    "text": {
                        "type": "string",
                        "description": "the text to write to the file",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "whether to overwrite the current file if it exists at the path",
                    },
                },
            },
            mounts=mounts,
        )

    async def execute(self, context: ToolContext, **kwargs):
        path = kwargs["path"]
        text = kwargs["text"]
        overwrite = kwargs["overwrite"]
        resolved = self._resolve_path(path)
        await resolved.mount.write_text(
            context=context,
            resolved=resolved,
            path=path,
            text=text,
            overwrite=overwrite,
        )
        return "the file was saved"


class GetFileDownloadUrl(_StorageTool):
    def __init__(self, *, mounts: list[_PreparedMount]):
        super().__init__(
            name="get_file_download_url",
            title="get file download url",
            description="get a url that can be used to download a file in the room",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "the full path of the file",
                    }
                },
            },
            mounts=mounts,
        )

    async def execute(self, context: ToolContext, **kwargs):
        path = kwargs["path"]
        resolved = self._resolve_path(path)
        return await resolved.mount.get_download_url(
            context=context, resolved=resolved, path=path
        )


class ListFilesTool(_StorageTool):
    def __init__(self, *, mounts: list[_PreparedMount]):
        super().__init__(
            name="list_files_in_room",
            title="list files in room",
            description="list the files at a specific path in the room",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {"path": {"type": "string"}},
            },
            mounts=mounts,
        )

    async def execute(self, context: ToolContext, **kwargs):
        path = kwargs["path"]
        resolved = self._resolve_path(path)
        files = await resolved.mount.list_entries(context=context, resolved=resolved)
        return JsonResponse(
            json={"files": list([f.model_dump(mode="json") for f in files])}
        )


class SaveFileFromUrlTool(_StorageTool):
    def __init__(self, *, mounts: list[_PreparedMount]):
        super().__init__(
            name="save_file_from_url",
            title="save file from url",
            description="save a file from a url to a path in the room",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["url", "path", "overwrite"],
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "the url of a file that should be saved to the room",
                    },
                    "path": {
                        "type": "string",
                        "description": "the destination path (including the filename)",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "whether to overwrite the existing file) (default false)",
                    },
                },
            },
            mounts=mounts,
        )

    async def execute(self, context: ToolContext, **kwargs):
        url = kwargs["url"]
        path = kwargs["path"]
        overwrite = kwargs["overwrite"]
        resolved = self._resolve_path(path)
        blob = await get_bytes_from_url(url=url)
        await resolved.mount.write_bytes(
            context=context,
            resolved=resolved,
            path=path,
            data=blob.data,
            overwrite=overwrite,
        )


class StorageToolkit(RemoteToolkit):
    def __init__(
        self,
        read_only: bool = False,
        mounts: Optional[list[StorageToolMount]] = None,
    ):
        prepared_mounts = _prepare_mounts(mounts)
        self._mounts = prepared_mounts
        self._read_only = read_only
        has_writable_mount = any(
            not prepared.mount.read_only for prepared in prepared_mounts
        )

        if read_only or not has_writable_mount:
            tools = [
                ListFilesTool(mounts=prepared_mounts),
                ReadFileTool(mounts=prepared_mounts),
            ]
        else:
            tools = [
                ListFilesTool(mounts=prepared_mounts),
                WriteFileTool(mounts=prepared_mounts),
                ReadFileTool(mounts=prepared_mounts),
                SaveFileFromUrlTool(mounts=prepared_mounts),
            ]
        super().__init__(
            name="storage",
            title="storage",
            description="tools for interacting with meshagent room storage",
            tools=tools,
        )

    def _ensure_writable(self, path: str) -> None:
        if self._read_only:
            raise RoomException(f"storage toolkit is read-only: {path}")

    def _resolve_path(self, path: str) -> _ResolvedStoragePath:
        return _resolve_storage_path(self._mounts, path)

    async def read_file(self, *, context: ToolContext, path: str) -> FileResponse:
        resolved = self._resolve_path(path)
        return await resolved.mount.read_file(
            context=context,
            resolved=resolved,
            path=path,
        )

    async def list_entries(
        self, *, context: ToolContext, path: str
    ) -> list[StorageEntry]:
        resolved = self._resolve_path(path)
        return await resolved.mount.list_entries(context=context, resolved=resolved)

    async def get_download_url(
        self, *, context: ToolContext, path: str
    ) -> LinkResponse:
        resolved = self._resolve_path(path)
        return await resolved.mount.get_download_url(
            context=context,
            resolved=resolved,
            path=path,
        )

    async def write_text(
        self,
        *,
        context: ToolContext,
        path: str,
        text: str,
        overwrite: bool,
    ) -> None:
        self._ensure_writable(path)
        resolved = self._resolve_path(path)
        await resolved.mount.write_text(
            context=context,
            resolved=resolved,
            path=path,
            text=text,
            overwrite=overwrite,
        )

    async def write_bytes(
        self,
        *,
        context: ToolContext,
        path: str,
        data: bytes,
        overwrite: bool,
    ) -> None:
        self._ensure_writable(path)
        resolved = self._resolve_path(path)
        await resolved.mount.write_bytes(
            context=context,
            resolved=resolved,
            path=path,
            data=data,
            overwrite=overwrite,
        )


class StorageToolkitConfig(ToolkitConfig):
    name: str = "storage"


class StorageToolkitBuilder(ToolkitBuilder):
    def __init__(self, *, mounts: Optional[list[StorageToolMount]] = None):
        super().__init__(name="storage", type=StorageToolkitConfig)
        self.mounts = mounts

    async def make(
        self, *, room: RoomClient, model: str, config: StorageToolkitConfig
    ) -> Toolkit:
        return StorageToolkit(mounts=self.mounts)
