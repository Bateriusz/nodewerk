from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw07_contract  # noqa: E402
import nw04_fixture  # noqa: E402
import nw05_export  # noqa: E402
import nw08_editor  # noqa: E402
import nw08_studio  # noqa: E402
import nwp_package  # noqa: E402
from schema_validator import validate_file_against_schema  # noqa: E402


COMMON_PROFILE = "nw.common.d3d12.fl12_0.sm6_0.v1"
COMMON_DIGEST = (
    "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a"
)


def test_common_profile_and_requirements_digest_are_unchanged() -> None:
    assert nwp_package.COMMON_PROFILE_ID == COMMON_PROFILE
    assert nwp_package.COMMON_REQUIREMENTS_DIGEST == COMMON_DIGEST
    session = nw08_editor.GraphEditorSession.create_nw07_proof()
    assert session.document.profile_id == COMMON_PROFILE
    assert session.document.requirements_digest == COMMON_DIGEST
    assert nw07_contract.PROFILE_ID == COMMON_PROFILE
    assert nw07_contract.REQUIREMENTS_DIGEST == COMMON_DIGEST


def test_editor_state_is_separate_from_release_and_player_sources() -> None:
    forbidden_in_player = []
    for path in (REPO_ROOT / "src" / "player").glob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in ("nw08_", "nodewerk-studio", "tkinter", "nw08_gui"):
            if token in text:
                forbidden_in_player.append((path.name, token))
    assert forbidden_in_player == []

    root_cmake = (REPO_ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "nw08" not in root_cmake.lower()
    assert "nodewerk-studio-preview" not in root_cmake


def test_release_pinned_input_digest_is_unchanged_by_editor_additions() -> None:
    import nw07_export

    config = nw07_export.load_canonical_config()
    result = nw07_export.verify_pinned_inputs(config)
    assert result["matches_frozen_digest"] is True
    assert result["digest"] == result["frozen_digest"]
    for path in (
        "tools/nw08_cache.py",
        "tools/nw08_editor.py",
        "tools/nw08_preview.py",
        "tools/nw08_studio.py",
        "tools/nw08_gui.py",
        "src/studio/nw_studio_preview_host.cpp",
    ):
        assert path not in nw07_contract.PINNED_BUILD_INPUTS


def test_authoring_infrastructure_is_excluded_from_release_closure() -> None:
    import tempfile

    import nw05_optimizer

    graph = REPO_ROOT / "fixtures" / "nw05" / "procedural_image.nwgraph"
    container = nw04_fixture.make_dxil_container()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        dxc = root / "mock_dxc.py"
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
        template = root / "template.exe"
        template.write_bytes(b"MZ" + b"\x00" * 256)
        output = root / "out"
        os.environ["NODEWERK_NW03_TEST_DIGEST"] = "1"
        try:
            result = nw05_export.export_graph(
                graph,
                output,
                dxc_executable=str(dxc),
                settings=nw05_export.ExportSettings(
                    player_template=str(template)
                ),
                allow_test_payloads=True,
            )
        finally:
            os.environ.pop("NODEWERK_NW03_TEST_DIGEST", None)

        manifest_path = output / "link-manifest.json"
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        excluded = set(manifest["excluded"]["authoring_infrastructure"])
        assert {
            "nodewerk-studio",
            "nw02-graph-parser",
            "nw03-source-maps",
            "nw03-reflection-tables",
        } <= excluded
        retained = manifest["retained"]
        retained_text = json.dumps(retained, sort_keys=True).lower()
        for token in (
            "studio",
            "tkinter",
            "dxcompiler",
            "dxc",
            "source_map",
            "source-map",
            "profiling",
            "nw08",
        ):
            assert token not in retained_text
        assert "nodewerkc" not in retained["code"]
        # The package-link builder is still the single NW-05 implementation.
        assert callable(nw05_optimizer.build_link_manifest)
        assert (
            result.artifacts[nwp_package.COMMON_PROFILE_ID].manifest[
                "manifest_sha256"
            ]
            == manifest["manifest_sha256"]
        )


def test_studio_report_schema_validates() -> None:
    import tempfile

    application = nw08_studio.StudioApplication.create()
    application.preview(0)
    report = application.report()
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        validate_file_against_schema(
            path, REPO_ROOT / "schemas" / "nw08-studio-report.schema.json"
        )
    assert report["hardware_evidence"] is False
    assert report["session"]["profile_id"] == COMMON_PROFILE


def test_frozen_nw07_material_remains_unmodified() -> None:
    # The NW-07 proof and performance-reference contracts are read-only inputs
    # to the editor.  Their pinned digests are part of the frozen contract.
    proof = REPO_ROOT / "fixtures" / "nw07" / "production_loop.nwgraph"
    assert proof.is_file()
    config = json.loads(
        (REPO_ROOT / "benchmarks" / "nw07" / "canonical-production.v1.json").read_text(
            encoding="utf-8"
        )
    )
    import hashlib

    assert hashlib.sha256(proof.read_bytes()).hexdigest() == config["graph_sha256"]
    # Hardware evidence remains honestly pending.
    evidence = json.loads(
        (REPO_ROOT / "benchmarks" / "nw07" / "hardware-evidence.json").read_text(
            encoding="utf-8"
        )
    )
    assert evidence["verification_status"] == "pending_unverified"

def test_windows_ci_reuses_pinned_nw07_release_toolchain() -> None:
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "nw08-studio.yml"
    ).read_text(encoding="utf-8")

    assert "cmake --preset nw07-release -DNW_ENFORCE_PINNED_TOOLCHAIN=ON" in workflow
    assert (
        "cmake --build --preset nw07-release "
        "--target nw_procedural_image_player --parallel 2"
    ) in workflow
    assert "build/nw07-release" in workflow
    assert "build/nw08-runtime" not in workflow
    assert "cmake/toolchains/msvc-x64.cmake" in workflow
    assert "-DCMAKE_BUILD_TYPE=Debug" in workflow
    assert "-DNW_BUILD_PROFILE=dev" in workflow

