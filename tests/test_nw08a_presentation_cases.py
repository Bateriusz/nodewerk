from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw08a_presentation_cases as cases  # noqa: E402
import nw08a_windows_build_checks as build_checks  # noqa: E402


def test_presentation_case_tool_exports_explicit_geometry(tmp_path: Path) -> None:
    report = cases.run(work=tmp_path / "cases")
    assert report["success"] is True
    assert report["export_mode"] == "test_payload"
    assert report["hardware_evidence"] is False
    assert report["real_player_validation"] == "unexecuted"
    rows = {row["name"]: row for row in report["cases"]}
    assert set(rows) == {"landscape", "portrait", "canonical_hd", "square"}
    for name, width, height in cases.CASES:
        row = rows[name]
        assert row["status"] == "passed"
        assert row["present_width"] == width
        assert row["present_height"] == height
        assert row["preview_dimensions"] == [width, height]


def test_presentation_case_tool_report_is_deterministic(tmp_path: Path) -> None:
    first = cases.run(work=tmp_path / "first")
    second = cases.run(work=tmp_path / "second")
    # Case records are stable apart from the work-directory field.
    assert json.dumps(first["cases"], sort_keys=True) == json.dumps(
        second["cases"], sort_keys=True
    )


def _write_build_checks_evidence(
    root: Path,
    *,
    complete_bytes: int = 240000,
    second_executable_sha256: str | None = None,
) -> tuple[Path, Path]:
    digest = "sha256:v1:test-build-inputs"
    summary = {
        "format": "nw07-verification-summary",
        "mode": "BuildChecks",
        "success": True,
        "checks": [
            {
                "name": "nw07_native_release_export",
                "status": "passed",
                "evidence": {
                    "reproducible": True,
                    "matches_frozen_digest": True,
                    "within_ceiling": True,
                    "release_grade": True,
                    "test_payload": False,
                    "complete_distribution_bytes": complete_bytes,
                    "build_inputs_digest": digest,
                    "package_sha256": "package-sha256",
                    "manifest_sha256": "manifest-sha256",
                    "executable_sha256": "executable-sha256",
                },
            }
        ],
    }

    def report(executable_sha256: str) -> dict:
        return {
            "success": True,
            "release_grade": True,
            "test_payload": False,
            "build_inputs": {
                "digest": digest,
                "matches_frozen_digest": True,
            },
            "artifact": {
                "package_sha256": "package-sha256",
                "manifest_sha256": "manifest-sha256",
                "executable_sha256": executable_sha256,
                "link_manifest_sha256": "link-manifest-sha256",
                "size_accounting": {
                    "complete_distribution_bytes": complete_bytes,
                },
            },
        }

    summary_path = root / "build-checks.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    repro = root / "verification" / "repro"
    repro.mkdir(parents=True)
    (repro / "clean-a-report.json").write_text(
        json.dumps(report("executable-sha256"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (repro / "clean-b-report.json").write_text(
        json.dumps(
            report(second_executable_sha256 or "executable-sha256"),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return summary_path, root / "verification"


def test_windows_build_checks_record_accepts_matching_clean_directories(
    tmp_path: Path,
) -> None:
    summary, verification = _write_build_checks_evidence(tmp_path)
    record = build_checks.build_record(
        summary_path=summary, verification_root=verification
    )
    assert record["success"] is True
    assert record["matches_frozen_digest"] is True
    assert record["reproducible"] is True
    assert record["complete_distribution_bytes"] == 240000
    assert record["hard_ceiling_bytes"] == 262144
    assert record["within_hard_ceiling"] is True
    for field in build_checks.IDENTITY_FIELDS:
        assert record["identity"][field]["match"] is True
        assert (
            record["identity"][field]["first"]
            == record["identity"][field]["second"]
        )


def test_windows_build_checks_record_rejects_unmatched_clean_directories(
    tmp_path: Path,
) -> None:
    summary, verification = _write_build_checks_evidence(
        tmp_path, second_executable_sha256="different-executable-sha256"
    )
    with pytest.raises(
        build_checks.BuildChecksRecordError, match="executable_sha256"
    ):
        build_checks.build_record(
            summary_path=summary, verification_root=verification
        )


def test_windows_build_checks_record_rejects_size_above_hard_limit(
    tmp_path: Path,
) -> None:
    summary, verification = _write_build_checks_evidence(
        tmp_path, complete_bytes=262145
    )
    with pytest.raises(
        build_checks.BuildChecksRecordError, match="262144-byte hard limit"
    ):
        build_checks.build_record(
            summary_path=summary, verification_root=verification
        )


def test_windows_build_checks_cli_writes_the_record(tmp_path: Path) -> None:
    summary, verification = _write_build_checks_evidence(tmp_path)
    output = tmp_path / "record.json"
    assert (
        build_checks.main(
            [
                "--summary",
                str(summary),
                "--verification-root",
                str(verification),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["format"] == build_checks.FORMAT
    assert record["success"] is True
    assert record["within_hard_ceiling"] is True


def test_windows_validation_workflow_builds_template_and_records_checks() -> None:
    workflow = (
        REPO_ROOT
        / ".github"
        / "workflows"
        / "nw08a-presentation-geometry.yml"
    ).read_text(encoding="utf-8")
    template_configure = workflow.index(
        "cmake --preset nw07-release -DNW_ENFORCE_PINNED_TOOLCHAIN=ON"
    )
    template_build = workflow.index(
        "cmake --build --preset nw07-release "
        "--target nw_procedural_image_player"
    )
    case_export = workflow.index("tools/nw08a_presentation_cases.py")
    build_checks_call = workflow.index("tools/verify_nw07.py --mode BuildChecks")
    record_call = workflow.index("tools/nw08a_windows_build_checks.py")
    assert (
        template_configure
        < template_build
        < case_export
        < build_checks_call
        < record_call
    )
    assert (
        "build/nw07-release/staging/procedural_image_player/"
        "nw_procedural_image_player.exe"
    ) in workflow
    toolchain = workflow.index(
        '$toolchain = Join-Path $PWD "cmake/toolchains/msvc-x64.cmake"'
    )
    fixture_configure = workflow.index("cmake -S . -B \"build/nw08a-$mode\"")
    assert toolchain < fixture_configure < case_export
    assert '"-DCMAKE_TOOLCHAIN_FILE=$toolchain"' in workflow
    assert "-DNW_USE_INBOX_UCRT=ON" in workflow
    assert "NW_PLAYER_FROZEN_SPECIALIZATION" in workflow
    assert "$expectedModes = @{ generic = 0; frozen = 1 }" in workflow
    assert "compiled_frozen" in workflow
    assert "--planner-self-test" in workflow
    assert "tools/nw08a_windows_build_checks.py" in workflow
    assert "--verification-root build/nw08a-nw07-verification" in workflow
    assert "nw08a-nw07-build-checks.json" in workflow
    assert "nw08a-windows-build-checks-record.json" in workflow
    assert "actions/upload-artifact@v4" in workflow


def test_windows_validation_doc_builds_template_before_export() -> None:
    doc = (
        REPO_ROOT / "docs" / "nw08a-presentation-geometry-validation.md"
    ).read_text(encoding="utf-8")
    template_build = doc.index(
        "cmake --build --preset nw07-release --target nw_procedural_image_player"
    )
    fixture_build = doc.index("cmake -S . -B build/nw08a-generic")
    case_export = doc.index("python tools/nw08a_presentation_cases.py")
    assert template_build < fixture_build < case_export
    assert "creates `build/nw07-release`" in doc
    assert "nw08a-windows-build-checks-record.json" in doc
    assert "windows-canonical-build-checks" in doc


def test_frozen_fixture_links_the_specialized_player_implementation() -> None:
    cmake = (REPO_ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    bootstrap_start = cmake.index(
        "if(NW05_GENERATED_PLAYER_SOURCE OR "
        "NW05_FROZEN_EMBEDDED_SPECIALIZATION)"
    )
    bootstrap_end = cmake.index("# NW-04: package-driven", bootstrap_start)
    bootstrap = cmake[bootstrap_start:bootstrap_end]
    assert "add_library(nw05_player_bootstrap STATIC" in bootstrap
    assert (
        "NW05_FROZEN_EMBEDDED_SPECIALIZATION="
        "$<BOOL:${NW05_FROZEN_EMBEDDED_SPECIALIZATION}>"
    ) in bootstrap

    fixture_start = cmake.index(
        "add_executable(nw_package_player_fixture", bootstrap_end
    )
    fixture_end = cmake.index(
        "nw_attach_dependency_check(nw_package_player_fixture",
        fixture_start,
    )
    fixture = cmake[fixture_start:fixture_end]
    assert "if(NW05_FROZEN_EMBEDDED_SPECIALIZATION)" in fixture
    assert "nw05_player_bootstrap" in fixture
    assert "nw_player_bootstrap" in fixture
    # The fixture loads arbitrary external packages, including sizes smaller
    # than canonical HD.  It must not link the frozen decoder that substitutes
    # the canonical-HD CAP limits; it links the fully validating decoder even
    # though the player implementation itself is frozen.
    assert fixture.count("nw_package_player_diagnostic") == 2
    assert "\n        nw_package_player\n" not in fixture

    diagnostic_start = cmake.index(
        "add_library(nw_package_player_diagnostic STATIC"
    )
    diagnostic_end = cmake.index("# Host-only NW-06", diagnostic_start)
    diagnostic = cmake[diagnostic_start:diagnostic_end]
    assert "NW05_FROZEN_EMBEDDED_SPECIALIZATION=0" in diagnostic

    generated_start = cmake.index(
        'add_executable(nw05_generated_player "${NW05_GENERATED_PLAYER_SOURCE}")'
    )
    generated_end = cmake.index("add_library(nw_compiler", generated_start)
    generated = cmake[generated_start:generated_end]
    assert "nw_package_player\n" in generated
    assert "nw_package_player_diagnostic" not in generated

    header = (
        REPO_ROOT / "src" / "player" / "package_player.h"
    ).read_text(encoding="utf-8")
    source = (
        REPO_ROOT / "src" / "player" / "package_player.cpp"
    ).read_text(encoding="utf-8")
    fixture_main = (
        REPO_ROOT / "fixtures" / "nw04_package_player" / "main.cpp"
    ).read_text(encoding="utf-8")
    assert "compiled_with_frozen_specialization" in header
    assert (
        "bool PackagePlayer::compiled_with_frozen_specialization()"
        in source
    )
    assert "NW_PLAYER_FROZEN_SPECIALIZATION=" in fixture_main


def test_windows_validation_doc_separates_windowed_display_evidence() -> None:
    doc = (
        REPO_ROOT / "docs" / "nw08a-presentation-geometry-validation.md"
    ).read_text(encoding="utf-8")
    assert "Windowed hardware validation (display evidence)" in doc
    assert "Do not pass `--headless`" in doc
    assert "--emit-image-hash" in doc
    assert "NW_PLAYER_PRESENTS" in doc
    assert "unexecuted" in doc
    assert "CMAKE_TOOLCHAIN_FILE" in doc
    assert "nw05_player_bootstrap" in doc
    assert "NW_PLAYER_FROZEN_SPECIALIZATION" in doc
