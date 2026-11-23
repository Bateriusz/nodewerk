from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_correctness_runner_reproduces_and_binds_common_artifact() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1"
    ).read_text(encoding="utf-8")
    lowered = text.lower()

    assert 'validateset("ada", "blackwell")' in lowered
    assert "[switch]$compare" in lowered
    assert "[switch]$bundle" in lowered
    assert "enter-nwbuildenvironment" in lowered
    assert "tools/nw07_export.py" in text
    assert "tools/nw10_common_correctness.py" in text
    assert '"build-diagnostic"' in text
    assert '"capture"' in text
    assert '"compare"' in text
    assert "build/nw10-proof/report.json" in text
    assert "Get-NwExpectedArtifact" in text
    assert "--packed-ada-captures" in text
    assert "--packed-blackwell-captures" in text
    assert '"--implementation-head", (& git rev-parse HEAD).Trim()' in text
    assert 'Assert-NwSameValue "artifact_key"' in text
    assert 'Assert-NwSameValue "package_sha256"' in text
    assert 'Assert-NwSameValue "manifest_sha256"' in text
    assert 'Assert-NwSameValue "executable_sha256"' in text
    assert 'Assert-NwSameValue "complete_distribution_bytes"' in text
    assert "COMMON_ARTIFACT=EXACT_MATCH" in text


def test_correctness_runner_keeps_raw_capture_out_of_git_workflow() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1"
    ).read_text(encoding="utf-8")
    lowered = text.lower()

    assert "git add" not in lowered
    assert "git commit" not in lowered
    assert "git push" not in lowered
    assert "raw_capture_bytes_embedded" in text
    assert "local_paths_included" in text
    assert "$env:USERNAME" in text
    assert "$env:USERPROFILE" in text
    assert "Assert-NwPrivacy" in text


def test_correctness_runner_can_create_transfer_bundle() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1"
    ).read_text(encoding="utf-8")

    assert "bundle-manifest.json" in text
    assert "Get-FileHash -Algorithm SHA256" in text
    assert "Compress-Archive" in text
    assert "BUNDLE_ROOT=$bundleRoot" in text
    assert 'nw10-correctness-$Tier$bundleSuffix-bundle.zip' in text
    assert '[ValidateSet("unpacked", "packed")]' in text
    assert '"--mode", $PackagingMode' in text
    assert "-PackagingMode packed" in (
        REPO_ROOT / ".github" / "workflows" / "nw10-compact.yml"
    ).read_text(encoding="utf-8")


def test_correctness_runner_compare_requires_both_hardware_sets() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1"
    ).read_text(encoding="utf-8")

    for parameter in (
        "AdaRecord",
        "AdaCaptures",
        "BlackwellRecord",
        "BlackwellCaptures",
    ):
        assert parameter in text
    assert "Cross-hardware correctness comparison is not verified." in text


def test_correctness_compare_runs_privacy_audit() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1"
    ).read_text(encoding="utf-8")
    compare_block = text[text.index('if ($Compare)'):]
    assert "Assert-NwPrivacy -JsonObject $comparison" in compare_block


def test_correctness_runner_handles_detached_head_for_ci_transfer() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1"
    ).read_text(encoding="utf-8")

    assert "function Get-NwCurrentBranch" in text
    assert "function Assert-NwExpectedBranch" in text
    assert '$output = @(& git branch --show-current)' in text
    assert '[string]::IsNullOrWhiteSpace($Expected)' in text
    assert "(& git branch --show-current).Trim()" not in text


def test_correctness_runner_supports_prebuilt_diagnostic_transfer() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1"
    ).read_text(encoding="utf-8")
    assert "[switch]$PrepareTransferBundle" in text
    assert '"nw07-diagnostics"' in text
    assert '"build-diagnostic"' in text
    assert "run-transferred-correctness-capture.ps1" in text
    assert "TARGET_TOOLCHAIN_REQUIRED=False" in text
    assert "TARGET_DEBUG_CRT_REQUIRED=$($debugRuntimeDependencies.Count -gt 0)" in text
    assert "TARGET_DEBUG_CRT_DEPENDENCIES=" in text


def test_transfer_allows_non_identical_rebuilt_pe_when_common_package_matches() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1"
    ).read_text(encoding="utf-8")
    transfer_block = text[text.index("if ($PrepareTransferBundle)") : text.index("if ($Compare)")]

    assert '"-DNW10_COMPACT_PACKAGE=OFF"' in transfer_block
    assert '"tools/nw07_export.py"' in transfer_block
    assert '"tools/nw10_perf_candidate.py"' not in transfer_block
    assert 'Assert-NwSameValue "artifact_key"' in transfer_block
    assert 'Assert-NwSameValue "package_sha256"' in transfer_block
    assert 'Assert-NwSameValue "manifest_sha256"' in transfer_block
    assert 'Assert-NwSameValue "complete_distribution_bytes"' in transfer_block
    assert 'Assert-NwSameValue "executable_sha256"' not in transfer_block
    assert "REFERENCE_EXECUTABLE_SHA256=" in transfer_block
    assert "REPRODUCED_EXECUTABLE_SHA256=" in transfer_block
    assert "final_executable_sha256 = $finalExecutableSha" in transfer_block
    assert "final_executable_bytes = $finalExecutableBytes" in transfer_block
    assert '$referenceFormat -eq "nw10-intro-report"' in transfer_block
    assert '"nw10_intro"' in transfer_block
    assert "selected_executable_sha256" in transfer_block
    assert "common_package_exact_match = $true" in transfer_block


def test_transferred_capture_requires_no_build_toolchain() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-transferred-correctness-capture.ps1"
    ).read_text(encoding="utf-8").lower()
    for forbidden in (
        "cmake ",
        "dxc.exe",
        "enter-nwbuildenvironment",
        "& git ",
        "python.exe",
        "python ",
    ):
        assert forbidden not in text
    assert "--capture-tick" in text
    assert "--frames 1" in text
    assert "get-filehash" in text
    assert "packaging_mode" in text
    assert "capture_exit_code = 0" in text
    assert "utf8encoding($false)" in text
    assert "measure-object -property bytes" not in text
    assert '$rawbytes += [int64]$row["bytes"]' in text


def test_transferred_capture_uses_single_canonical_result_root() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-transferred-correctness-capture.ps1"
    ).read_text(encoding="utf-8")

    assert '$resultManifestPath = Join-Path $output "bundle-manifest.json"' in text
    assert 'Get-RelativePath $output $file.FullName' in text
    assert '$bundleRoot = Join-Path $output "bundle"' not in text
    assert 'Copy-Item $recordPath (Join-Path $bundleRoot "capture.json")' not in text
    assert 'RESULT_RECORD=$recordPath' in text
    assert 'RESULT_MANIFEST=$resultManifestPath' in text
    assert "$archivePaths = @(" in text
    assert "$resultManifestPath," in text
    assert "(Join-Path $output $verificationFile)" in text


def test_correctness_transfer_accepts_release_intro_without_status_field() -> None:
    text = (
        REPO_ROOT / "tools" / "nw10" / "run-correctness-evidence.ps1"
    ).read_text(encoding="utf-8")
    expected_block = text[
        text.index("function Get-NwExpectedArtifact")
        : text.index("function Get-NwJsonStrings")
    ]

    assert 'Get-NwOptionalProperty $Reference "status"' in expected_block
    assert 'Get-NwOptionalProperty $Reference "canonical_artifact"' in expected_block
    assert "$Reference.status" not in expected_block
    assert "$Reference.canonical_artifact" not in expected_block


def test_docs_point_packed_comparison_at_verifiable_bundle() -> None:
    text = (
        REPO_ROOT / "docs" / "nw10-compact-export.md"
    ).read_text(encoding="utf-8")

    for record in (
        "-AdaRecord build/nw10-correctness-scripted/ada/bundle/capture.json",
        "-AdaCaptures build/nw10-correctness-scripted/ada/bundle/captures",
        "-BlackwellRecord "
        "build/nw10-correctness-scripted/blackwell/bundle/capture.json",
        "-BlackwellCaptures "
        "build/nw10-correctness-scripted/blackwell/bundle/captures",
    ):
        assert record in text
    assert (
        "-PackedAdaRecord "
        "build/nw10-correctness-scripted/ada-packed/bundle/capture.json"
    ) in text
    assert (
        "-PackedAdaCaptures "
        "build/nw10-correctness-scripted/ada-packed/bundle/captures"
    ) in text
    assert (
        "-PackedBlackwellRecord "
        "build/nw10-correctness-scripted/blackwell-packed/bundle/capture.json"
    ) in text
    assert (
        "-PackedBlackwellCaptures "
        "build/nw10-correctness-scripted/blackwell-packed/bundle/captures"
    ) in text
    assert "build/nw10-correctness-scripted/ada-packed/capture.json" not in text
    assert "build/nw10-correctness-scripted/blackwell-packed/capture.json" not in text
    assert "build/nw10-correctness-scripted/ada/capture.json" not in text
    assert "build/nw10-correctness-scripted/blackwell/capture.json" not in text
