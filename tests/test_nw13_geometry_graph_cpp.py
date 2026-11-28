from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "fixtures" / "nw13" / "geometry_operators.nwgraph"


def _find_cxx() -> str | None:
    configured = os.environ.get("CXX")
    if configured:
        candidate = shutil.which(configured) or (configured if Path(configured).exists() else None)
        if candidate:
            return candidate
    for name in ("c++", "g++", "clang++", "cl", "cl.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


@pytest.fixture(scope="module")
def cli_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    compiler = _find_cxx()
    if compiler is None:
        pytest.skip("NW-13 geometry graph semantics require a C++20 compiler; no compiler was found")
    source_dir = REPO_ROOT / "src" / "graph"
    build_dir = tmp_path_factory.mktemp("nw13_geometry_graph_build")
    executable = build_dir / ("nw_graph_cli.exe" if os.name == "nt" else "nw_graph_cli")
    command = [
        compiler,
        "-std=c++20",
        "-O2",
        "-I",
        str(source_dir),
        str(source_dir / "nw_graph.cpp"),
        str(source_dir / "nw_graph_cli.cpp"),
        "-o",
        str(executable),
    ]
    if Path(compiler).stem.lower() == "cl":
        command = [
            compiler, "/nologo", "/std:c++20", "/EHsc", "/O2", f"/I{source_dir}",
            str(source_dir / "nw_graph.cpp"), str(source_dir / "nw_graph_cli.cpp"),
            f"/Fe:{executable}",
        ]
    process = subprocess.run(command, capture_output=True, text=True, check=False, timeout=240, cwd=build_dir)
    assert process.returncode == 0, process.stderr
    return executable


def _run(cli: Path, *args: str) -> tuple[int, dict]:
    process = subprocess.run([str(cli), *args], capture_output=True, text=True, check=False, timeout=120)
    return process.returncode, json.loads(process.stdout)


def test_public_geometry_graph_validates_evaluates_and_hashes(cli_binary: Path) -> None:
    code, payload = _run(cli_binary, "validate", str(FIXTURE))
    assert code == 0, payload
    assert payload["valid"] is True
    assert payload["node_count"] == 16

    code, evaluation = _run(cli_binary, "eval", str(FIXTURE))
    assert code == 0, evaluation
    assert evaluation["ok"] is True
    operators = {value["operator"] for value in evaluation["values"]}
    assert "nw.geom.sphere@1.0.0" in operators
    assert "nw.geom.union@1.0.0" in operators
    assert "nw.geom.displace@1.0.0" in operators
    assert all(value["type"] == "geometry_recipe" for value in evaluation["values"])

    first = subprocess.run([str(cli_binary), "digest", str(FIXTURE)], capture_output=True, text=True, check=False)
    second = subprocess.run([str(cli_binary), "digest", str(FIXTURE)], capture_output=True, text=True, check=False)
    assert first.returncode == 0
    assert first.stdout == second.stdout
    assert first.stdout.strip().startswith("sha256:v1:")

