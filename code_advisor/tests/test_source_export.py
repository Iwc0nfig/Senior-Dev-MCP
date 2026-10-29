"""Source export tests do not require the deployment workspace or an index."""
import base64
import hashlib
from types import SimpleNamespace

import pytest
from mcp.server import MCPServer

from senior_dev_code.tools.source_export import SourceExport, register


def test_original_bytes_hash_pagination_and_change_refusal(tmp_path):
    net = tmp_path / "Legacy"
    net.mkdir()
    (tmp_path / "Modern").mkdir()
    original = b"\xef\xbb\xbf" + ("// Ünïcödé テスト 👩🏽‍💻\r\n" * 4000).encode()
    path = net / "Entity.cs"
    path.write_bytes(original)
    export = SourceExport(tmp_path)
    manifest = export.manifest().structured_content
    row = manifest["files"][0]
    assert row["sha256"] == hashlib.sha256(original).hexdigest()
    chunks, offset = bytearray(), 0
    while True:
        response = export.batch(manifest["manifest"], [{"path": row["path"], "sha256": row["sha256"], "offset": offset}])
        assert len(response.model_dump_json(by_alias=True).encode()) <= 8192
        item = response.structured_content["files"][0]
        chunks.extend(base64.b64decode(item["data"]))
        offset = item["next_offset"]
        if offset is None:
            break
    assert chunks == original
    path.write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        export.batch(manifest["manifest"], [{"path": row["path"], "sha256": row["sha256"]}])


def test_registration_readonly_and_schema(tmp_path):
    server = MCPServer("source-export-contract")
    register(server, SimpleNamespace(cfg=SimpleNamespace(paths=SimpleNamespace(workspaces=tmp_path))))
    tools = server._tool_manager.list_tools()
    assert {t.name for t in tools} == {"get_source_manifest", "read_source_batch"}
    assert all(t.annotations.read_only_hint and t.parameters["type"] == "object" for t in tools)


def test_exclusions_and_path_escape_refused(tmp_path):
    net = tmp_path / "Legacy"
    net.mkdir()
    (net / "bin").mkdir()
    (net / "bin/Excluded.cs").write_text("hidden")
    export = SourceExport(tmp_path)
    manifest = export.manifest().structured_content
    assert not any(r["path"].endswith("Excluded.cs") for r in manifest["files"])
    assert any(r.get("reason") == "excluded_directory" for r in manifest["files"])
    with pytest.raises(ValueError, match="manifest-listed"):
        export.batch(manifest["manifest"], [{"path": "../outside", "sha256": "x"}])
