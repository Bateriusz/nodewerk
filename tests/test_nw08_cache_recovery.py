from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw04_fixture  # noqa: E402
import nw05_export  # noqa: E402
import nwp_package as nwp  # noqa: E402


def _mock_dxc(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dxc = directory / "mock_dxc.py"
    container = nw04_fixture.make_dxil_container()
    dxc.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\n"
        "    sys.exit(0)\n"
        "fo = Path(sys.argv[sys.argv.index('-Fo') + 1])\n"
        "fo.parent.mkdir(parents=True, exist_ok=True)\n"
        f"fo.write_bytes(bytes.fromhex({container.hex()!r}))\n",
        encoding="utf-8",
    )
    dxc.chmod(0o755)
    return dxc


def _export(graph: Path, output: Path, dxc: Path, cache_dir: Path):
    return nw05_export.export_graph(
        graph,
        output,
        dxc_executable=str(dxc),
        cache_dir=str(cache_dir),
        allow_test_payloads=True,
    )


def test_corrupt_disk_cache_entries_regenerate_without_changing_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    graph = REPO_ROOT / "fixtures" / "nw05" / "procedural_image.nwgraph"
    dxc = _mock_dxc(tmp_path / "toolchain")
    cache_dir = tmp_path / "cache"

    first = _export(graph, tmp_path / "out-a", dxc, cache_dir)
    files = sorted(cache_dir.iterdir())
    assert {path.suffix for path in files} == {".dxil", ".json"}
    # The NW-03 cache owns both the blob and its metadata sidecar.
    assert len(files) >= 2

    blob = next(path for path in files if path.suffix == ".dxil")
    meta = next(path for path in files if path.suffix == ".json")
    good_meta = json.loads(meta.read_text(encoding="utf-8"))
    original_blob = blob.read_bytes()
    blob.write_bytes(b"corrupt dxil payload")
    meta.write_text("{not-json", encoding="utf-8")

    second = _export(graph, tmp_path / "out-b", dxc, cache_dir)
    assert blob.read_bytes() == original_blob
    regenerated_meta = json.loads(meta.read_text(encoding="utf-8"))
    assert regenerated_meta["blob_sha256"] == good_meta["blob_sha256"]
    assert regenerated_meta["key"] == good_meta["key"]

    common_a = first.artifacts[nwp.COMMON_PROFILE_ID]
    common_b = second.artifacts[nwp.COMMON_PROFILE_ID]
    assert common_a.package_bytes == common_b.package_bytes
    assert common_a.manifest["manifest_sha256"] == common_b.manifest["manifest_sha256"]


def test_cache_entry_mismatch_is_a_miss_not_a_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
    graph = REPO_ROOT / "fixtures" / "nw05" / "procedural_image.nwgraph"
    dxc = _mock_dxc(tmp_path / "toolchain")
    cache_dir = tmp_path / "cache"
    _export(graph, tmp_path / "first", dxc, cache_dir)
    meta_path = next(cache_dir.glob("*.json"))
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["semantic_digest"] = "sha256:v1:" + "0" * 64
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")

    result = _export(graph, tmp_path / "second", dxc, cache_dir)
    assert result.artifacts[nwp.COMMON_PROFILE_ID].package_bytes
    repaired = json.loads(meta_path.read_text(encoding="utf-8"))
    assert repaired["semantic_digest"] != "sha256:v1:" + "0" * 64

