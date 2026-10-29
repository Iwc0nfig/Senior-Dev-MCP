"""Read-only Code Advisor extension.

Hashes are SHA-256 of original file bytes. Bounded batch chunks use base64 so BOMs,
line endings and all encodings survive transport without normalization.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from pathlib import Path

from mcp.types import CallToolResult, TextContent, ToolAnnotations

from ..config import SourceExportOpts, WorkspaceOpts

EXCLUDED = {".git", ".vs", ".svn", "obj", "bin", "node_modules", "packages", "testresults",
            "precompiledweb", "app_data", "_resharper.caches", ".nuget", "buildprocesstemplates", "zotero"}
RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _result(payload):
    return CallToolResult(content=[TextContent(type="text", text="Source export evidence is in structuredContent.")],
                          structured_content=payload)


def _size(payload):
    # Host name/version metadata is injected by MCPServer after tool return.
    # Reserve a bounded allowance for it; the database importer requests 64 KiB.
    return len(_result(payload).model_dump_json(by_alias=True).encode("utf-8")) + 512


class SourceExport:
    def __init__(self, root: Path, opts: SourceExportOpts | None = None, ws: WorkspaceOpts | None = None):
        self.root = root.resolve()
        self.opts = opts or SourceExportOpts()
        self.ws = ws or WorkspaceOpts()
        self.max_file_bytes = self.opts.max_file_bytes
        self._manifests = {}
        self._lock = threading.RLock()
        self._current = None

    def family(self, path: str):
        parts = path.split("/")
        if parts[0].casefold() == self.ws.net48_root.casefold():
            return "legacy", "net48"
        if parts[0].casefold() == self.ws.net8_root.casefold():
            return ("next" if len(parts) > 1 and parts[1].casefold() == self.ws.next_dir.casefold()
                    else "coreapi"), "net8"
        return None

    def _inventory(self):
        records = []
        for top in (self.ws.net48_root, self.ws.net8_root):
            base = self.root / top
            if not base.is_dir():
                records.append({"path": top, "status": "missing_source", "reason": "configured_workspace_side_missing"})
                continue
            if base.is_symlink() or base.is_junction() or not base.resolve().is_relative_to(self.root):
                records.append({"path": top, "status": "unavailable", "reason": "workspace_side_is_link_or_escapes_root"})
                continue
            def on_error(error):
                records.append({"path": Path(error.filename).relative_to(self.root).as_posix(),
                                "status": "unavailable", "reason": "directory_read_failed"})
            for directory, names, filenames in os.walk(base, followlinks=False, onerror=on_error):
                keep = []
                for name in sorted(names):
                    path = Path(directory) / name
                    reason = "excluded_directory" if name.casefold() in EXCLUDED or name.casefold().endswith("-release") else \
                        "symlink_or_junction" if path.is_symlink() or path.is_junction() else None
                    if reason:
                        records.append({"path": path.relative_to(self.root).as_posix(), "status": "omitted", "reason": reason})
                    else:
                        keep.append(name)
                names[:] = keep
                for filename in sorted(filenames):
                    if not filename.casefold().endswith(".cs"):
                        continue
                    path = Path(directory) / filename
                    relative = path.relative_to(self.root).as_posix()
                    family, side = self.family(relative)
                    row = {"path": relative, "family": family, "side": side, "status": "ok", "hash_kind": "sha256_original_bytes"}
                    try:
                        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                            raise ValueError
                        if path.stat().st_size > self.max_file_bytes:
                            row.update(status="omitted", reason="file_over_size_limit")
                        else:
                            with path.open("rb") as stream:
                                data = stream.read(self.max_file_bytes + 1)
                            if len(data) > self.max_file_bytes:
                                row.update(status="omitted", reason="file_over_size_limit")
                            else:
                                row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                    except (OSError, ValueError):
                        row.update(status="unavailable", reason="file_read_or_path_validation_failed")
                    records.append(row)
        return sorted(records, key=lambda r: r["path"])

    def manifest(self, cursor: str | None = None, limit: int | None = None, budget_bytes: int | None = None,
                 refresh: bool = False):
        o = self.opts
        limit = o.manifest_limit_default if limit is None else limit
        budget_bytes = o.budget_default if budget_bytes is None else budget_bytes
        if not 1 <= limit <= o.manifest_limit_max or not o.budget_min <= budget_bytes <= o.budget_max:
            raise ValueError(f"limit must be 1..{o.manifest_limit_max}; "
                             f"budget_bytes must be {o.budget_min}..{o.budget_max}")
        with self._lock:
            if cursor:
                try:
                    binding, offset = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                    if type(offset) is not int or offset < 0 or refresh:
                        raise ValueError
                except (ValueError, TypeError):
                    raise ValueError("Invalid manifest cursor") from None
                if binding not in self._manifests:
                    raise ValueError("Manifest expired; start a new inventory")
            else:
                offset = 0
                if refresh or self._current is None:
                    rows = self._inventory()
                    self._current = _hash(rows)
                    self._manifests[self._current] = rows
                    while len(self._manifests) > 4:
                        self._manifests.pop(next(iter(self._manifests)))
                binding = self._current
            rows = self._manifests[binding]
            if offset > len(rows):
                raise ValueError("Manifest cursor is out of range")
            payload = {"status": "ok", "layer": "filesystem", "manifest": binding,
                       "searched_scope": "all C# files under configured Legacy and Modern roots, including support dependencies",
                       "hash_kind": "sha256_original_bytes", "total_available": len(rows), "files": [],
                       "truncated": False, "next_cursor": None, "returned": 0,
                       "caveats": ["Filesystem text inventory, not compiler-exact. Excluded directories are listed as omissions without traversal."]}
            end = offset
            while end < len(rows) and end - offset < limit:
                payload["files"].append(rows[end])
                end += 1
                payload["returned"] = len(payload["files"])
                payload["truncated"] = end < len(rows)
                payload["next_cursor"] = base64.urlsafe_b64encode(_json([binding, end])).decode().rstrip("=") if end < len(rows) else None
                if _size(payload) > budget_bytes:
                    payload["files"].pop()
                    end -= 1
                    break
            payload["truncated"] = end < len(rows)
            payload["next_cursor"] = base64.urlsafe_b64encode(_json([binding, end])).decode().rstrip("=") if end < len(rows) else None
            if not payload["files"] and end < len(rows) or _size(payload) > budget_bytes:
                raise ValueError("Manifest row exceeds budget")
            payload["returned"] = len(payload["files"])
            if _size(payload) > budget_bytes:
                raise ValueError("Manifest row exceeds budget")
            return _result(payload)

    def batch(self, manifest: str, requests: list[dict], budget_bytes: int | None = None):
        o = self.opts
        budget_bytes = o.budget_default if budget_bytes is None else budget_bytes
        if not o.budget_min <= budget_bytes <= o.budget_max or not 1 <= len(requests) <= o.batch_requests_max:
            raise ValueError(f"Expected 1..{o.batch_requests_max} requests and "
                             f"budget_bytes {o.budget_min}..{o.budget_max}")
        with self._lock:
            rows = self._manifests.get(manifest)
            if rows is None:
                raise ValueError("Manifest expired or unknown")
            allowed = {r["path"]: r for r in rows if r["status"] == "ok"}
        payload = {"status": "ok", "layer": "filesystem", "manifest": manifest, "files": [],
                   "hash_kind": "sha256_original_bytes", "caveats": [], "returned": 0,
                   "truncated": False, "total_available": len(requests)}
        for request in requests:
            path, expected = request.get("path"), request.get("sha256")
            offset = request.get("offset", 0)
            if path not in allowed or expected != allowed[path]["sha256"]:
                raise ValueError("Only manifest-listed paths with their expected hashes may be read")
            if type(offset) is not int or not 0 <= offset <= allowed[path]["bytes"]:
                raise ValueError("Invalid raw-byte offset")
            target = self.root / path
            if not target.resolve().is_relative_to(self.root) or target.is_symlink() or any(
                p.is_symlink() or p.is_junction() for p in target.parents if p != self.root and p.is_relative_to(self.root)
            ):
                raise ValueError("Path escapes configured root or traverses a link")
            try:
                with target.open("rb") as stream:
                    data = stream.read(self.max_file_bytes + 1)
            except OSError:
                raise ValueError("Manifest-listed source is unavailable; start a new refresh") from None
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("Source changed since manifest; refusing mixed content")
            item = {"path": path, "sha256": expected, "offset": offset, "bytes": len(data),
                    "encoding": "base64", "data": "", "next_offset": None, "status": "ok"}
            payload["files"].append(item)
            low, high, best = 0, len(data) - offset, -1
            while low <= high:
                count = (low + high) // 2
                item["data"] = base64.b64encode(data[offset:offset + count]).decode("ascii")
                item["next_offset"] = offset + count if offset + count < len(data) else None
                payload["returned"] = len(payload["files"])
                payload["truncated"] = len(payload["files"]) < len(requests)
                if _size(payload) <= budget_bytes:
                    best = count
                    low = count + 1
                else:
                    high = count - 1
            if best < 0 or best == 0 and offset < len(data):
                payload["files"].pop()
                break
            item["data"] = base64.b64encode(data[offset:offset + best]).decode("ascii")
            item["next_offset"] = offset + best if offset + best < len(data) else None
        payload["returned"] = len(payload["files"])
        payload["truncated"] = len(payload["files"]) < len(requests)
        if not payload["files"] or _size(payload) > budget_bytes:
            raise ValueError("Source batch metadata exceeds budget")
        return _result(payload)


def register(mcp, ctx):
    # A drop-in extension: a bare cfg with only paths.workspaces still works.
    o = getattr(ctx.cfg, "source_export", None) or SourceExportOpts()
    export = SourceExport(ctx.cfg.paths.workspaces, o, getattr(ctx.cfg, "workspace", None))

    @mcp.tool(name="get_source_manifest", annotations=RO, description=(
        "Enumerate C# source in the three migration families and all support files. "
        "Return original-byte SHA-256, explicit omissions and bounded, hash-bound pages. No source writes."))
    def get_source_manifest(cursor: str | None = None, limit: int = o.manifest_limit_default,
                            budget_bytes: int = o.budget_default, refresh: bool = False) -> CallToolResult:
        return export.manifest(cursor, limit, budget_bytes, refresh)

    @mcp.tool(name="read_source_batch", annotations=RO, description=(
        "Read only manifest-listed source files, checking expected original-byte hashes. "
        "Return bounded base64 chunks with raw-byte offsets, preserving BOMs, encodings and line endings."))
    def read_source_batch(manifest: str, requests: list[dict], budget_bytes: int = o.budget_default) -> CallToolResult:
        return export.batch(manifest, requests, budget_bytes)
