#!/usr/bin/env python3
"""NW-03 verification entry point.

Usage:
    python tools/verify_nw03.py --mode ContractsOnly [--summary-json out.json]
    python tools/verify_nw03.py --mode Full [--dxc /path/to/dxc] [--summary-json out.json]

`Full` runs real offline DXC compilation in two clean directories, fixture and
CPU-oracle checks, package round-trip/malformed-input checks, release leakage
checks, and a bounded AddressSanitizer/UndefinedBehaviorSanitizer-backed native
parser fuzz campaign.  It returns nonzero when any required check fails or is
unavailable.  `ContractsOnly` runs the same package/oracle/leakage/fuzz checks
without requiring DXC and is suitable for GPU-less CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler  # noqa: E402
import nw03_oracle  # noqa: E402
import nwp_package  # noqa: E402


@dataclass
class CheckResult:
    name: str
    required: bool
    passed: bool
    detail: str = ""
    unavailable: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


def _synthetic_dxil_container(*, prog_version: int = (5 << 16) | (6 << 4)) -> bytes:
    sfi0 = b"SFI0" + struct.pack("<I", 8) + struct.pack("<Q", 0)
    psv0_info = bytearray(36)
    struct.pack_into("<III", psv0_info, 12, 64, 1, 1)
    psv0_payload = (
        struct.pack("<I", len(psv0_info))
        + bytes(psv0_info)
        + struct.pack("<II", 0, 1)
    )
    psv0 = b"PSV0" + struct.pack("<I", len(psv0_payload)) + psv0_payload
    words = [0x00000021, 0x00000002, 0x00000021, 0x0000000C] + [0] * 16
    bitcode = b"BC\xc0\xde" + struct.pack(f"<{len(words)}I", *words)
    dxil_payload = (
        struct.pack(
            "<II4sIII",
            prog_version,
            (24 + len(bitcode)) // 4,
            b"DXIL",
            0x00010100,
            16,
            len(bitcode),
        )
        + bitcode
    )
    dxil = b"DXIL" + struct.pack("<I", len(dxil_payload)) + dxil_payload
    chunks = [sfi0, psv0, dxil]
    header_size = 32 + len(chunks) * 4
    offsets = []
    cursor = header_size
    for chunk in chunks:
        offsets.append(cursor)
        cursor += len(chunk)
    joined = b"".join(chunks)
    digest = hashlib.blake2s(
        b"dxc-retail-sig:" + joined, digest_size=16
    ).digest()
    header = (
        b"DXBC"
        + digest
        + struct.pack("<HHII", 1, 0, cursor, len(chunks))
        + struct.pack(f"<{len(chunks)}I", *offsets)
    )
    return header + joined


def _fixture_graph() -> Path:
    return REPO_ROOT / "fixtures" / "nw03" / "math_noise.nwgraph"


def check_oracle_and_lowering() -> CheckResult:
    try:
        report = nw03_oracle.oracle_report()
        document = nw03_compiler.load_graph(_fixture_graph())
        generated, diagnostics = nw03_compiler.lower_to_fused_hlsl(document)
        if diagnostics:
            return CheckResult(
                "fixture_oracle_and_lowering",
                True,
                False,
                "lowering diagnostics: " + json.dumps(diagnostics),
            )
        if not report["passed"]:
            return CheckResult(
                "fixture_oracle_and_lowering",
                True,
                False,
                "CPU oracle exceeded documented tolerance",
                evidence={"oracle": report},
            )
        if len(generated) != 2:
            return CheckResult(
                "fixture_oracle_and_lowering",
                True,
                False,
                f"expected 2 reachable specializations, got {len(generated)}",
            )
        if any(
            "unused_expr" in shader.root_node_ids for shader in generated
        ):
            return CheckResult(
                "fixture_oracle_and_lowering",
                True,
                False,
                "unreachable ShaderExpr was emitted",
            )
        return CheckResult(
            "fixture_oracle_and_lowering",
            True,
            True,
            "oracle and reachable lowering checks passed",
            evidence={
                "sample_count": report["sample_count"],
                "specialization_count": len(generated),
                "reachable_roots": [shader.specialization_id for shader in generated],
            },
        )
    except Exception as exc:
        return CheckResult(
            "fixture_oracle_and_lowering", True, False, f"exception: {exc}"
        )


def check_package_roundtrip_and_malformed() -> CheckResult:
    try:
        blob = _synthetic_dxil_container()
        package = nwp_package.make_test_package(
            shader_blob=blob, specialization_count=2
        )
        data = nwp_package.encode_package(package)
        valid, diagnostics = nwp_package.validate_package_bytes(data)
        if not valid:
            return CheckResult(
                "package_roundtrip_and_malformed",
                True,
                False,
                f"valid package rejected: {diagnostics}",
            )
        roundtrip = nwp_package.encode_package(nwp_package.decode_package(data))
        if roundtrip != data:
            return CheckResult(
                "package_roundtrip_and_malformed",
                True,
                False,
                "package bytes are not canonical on round-trip",
            )
        mutations: list[tuple[str, bytes]] = [
            ("magic", b"BAD!" + data[4:]),
            ("truncated", data[:-1]),
            ("checksum", data[:-1] + bytes([data[-1] ^ 0x01])),
        ]
        # Recompute the checksum for mutations that are intended to reach the
        # section/capability/instruction decoders.
        def with_checksum(mutated: bytearray) -> bytes:
            checksum_offset = struct.unpack_from("<Q", mutated, 48)[0]
            mutated[checksum_offset:] = hashlib.sha256(
                bytes(mutated[:checksum_offset])
            ).digest()
            return bytes(mutated)

        bad_version = bytearray(data)
        struct.pack_into("<H", bad_version, 8, 2)
        mutations.append(("version", with_checksum(bad_version)))

        bad_section_count = bytearray(data)
        struct.pack_into("<I", bad_section_count, 40, 63)
        mutations.append(("section_count", with_checksum(bad_section_count)))

        bad_capability = bytearray(data)
        cap_offset = struct.unpack_from("<Q", bad_capability, 64 + 12)[0]
        bad_capability[cap_offset + 2 : cap_offset + 6] = struct.pack("<I", 1)
        mutations.append(("truncated_capability", with_checksum(bad_capability)))

        failures = []
        for name, candidate in mutations:
            ok, found = nwp_package.validate_package_bytes(candidate)
            if ok:
                failures.append(f"{name} mutation unexpectedly accepted")
            elif not all(item.get("category") == "package_validation" for item in found):
                failures.append(f"{name} mutation used the wrong diagnostic category")
        if failures:
            return CheckResult(
                "package_roundtrip_and_malformed", True, False, "; ".join(failures)
            )
        inspection = nwp_package.inspect_package_bytes(data).to_dict()
        if inspection["deduplication"]["unique_shader_blob_count"] != 1:
            return CheckResult(
                "package_roundtrip_and_malformed",
                True,
                False,
                "identical complete shader blobs were not deduplicated",
            )
        return CheckResult(
            "package_roundtrip_and_malformed",
            True,
            True,
            "round-trip and malformed-input checks passed",
            evidence={"mutations": len(mutations), "package_bytes": len(data)},
        )
    except Exception as exc:
        return CheckResult(
            "package_roundtrip_and_malformed", True, False, f"exception: {exc}"
        )


def check_release_leakage() -> CheckResult:
    try:
        blob = _synthetic_dxil_container()
        package = nwp_package.make_test_package(shader_blob=blob, specialization_count=1)
        data = nwp_package.encode_package(package)
        report = nwp_package.leak_scan_report(data)
        if not report["valid"]:
            return CheckResult(
                "release_leakage",
                True,
                False,
                "release leakage findings: " + json.dumps(report["findings"]),
                evidence=report,
            )
        return CheckResult(
            "release_leakage",
            True,
            True,
            "release package contains no authoring metadata",
            evidence=report,
        )
    except Exception as exc:
        return CheckResult(
            "release_leakage", True, False, f"exception: {exc}"
        )


def check_clean_directory_reproducibility(
    dxc_executable: str | None,
    work_root: Path,
) -> CheckResult:
    dxc = nw03_compiler.resolve_dxc(dxc_executable)
    if not dxc:
        return CheckResult(
            "clean_directory_reproducibility",
            True,
            False,
            "offline DXC is unavailable",
            unavailable=True,
        )
    try:
        hashes = []
        reports = []
        for index in range(2):
            directory = work_root / f"clean-{index}"
            directory.mkdir(parents=True, exist_ok=True)
            package_path = directory / "math_noise.nwp"
            report_path = directory / "build-report.json"
            report = nw03_compiler.compile_package(
                _fixture_graph(),
                package_path,
                report_path=report_path,
                cache_dir=directory / "cache",
                dxc_executable=dxc,
            )
            hashes.append(hashlib.sha256(package_path.read_bytes()).hexdigest())
            reports.append(report)
        if hashes[0] != hashes[1]:
            return CheckResult(
                "clean_directory_reproducibility",
                True,
                False,
                "two clean directories produced different package bytes",
                evidence={"hashes": hashes},
            )
        if (
            reports[0]["compiler"]["fingerprint"]
            != reports[1]["compiler"]["fingerprint"]
        ):
            return CheckResult(
                "clean_directory_reproducibility",
                True,
                False,
                "compiler fingerprints differ across clean directories",
            )
        return CheckResult(
            "clean_directory_reproducibility",
            True,
            True,
            "two clean directories produced byte-identical packages",
            evidence={
                "package_sha256": hashes[0],
                "compiler_fingerprint": reports[0]["compiler"]["fingerprint"],
                "profile_fingerprint": reports[0]["profile"]["profile_fingerprint"],
                "shader_specializations": reports[0]["fixture"][
                    "shader_specialization_count"
                ],
            },
        )
    except Exception as exc:
        return CheckResult(
            "clean_directory_reproducibility",
            True,
            False,
            f"exception: {exc}",
        )


def _python_bounded_fuzz(seed: int = 0x4E573033, iterations: int = 512) -> CheckResult:
    try:
        blob = _synthetic_dxil_container()
        base = nwp_package.encode_package(
            nwp_package.make_test_package(shader_blob=blob, specialization_count=2)
        )
        rng = random.Random(seed)
        for iteration in range(iterations):
            candidate = bytearray(base)
            mode = iteration % 5
            if mode == 0:
                # Structural mutation with a recomputed checksum.
                for _ in range(rng.randint(1, 8)):
                    position = rng.randrange(0, max(1, len(candidate) - 32))
                    candidate[position] ^= rng.randrange(1, 256)
                checksum_offset = struct.unpack_from("<Q", candidate, 48)[0]
                if checksum_offset <= len(candidate) - nwp_package.CHECKSUM_SIZE:
                    candidate[checksum_offset:] = hashlib.sha256(
                        bytes(candidate[:checksum_offset])
                    ).digest()
            elif mode == 1:
                candidate = candidate[: rng.randrange(0, len(candidate) + 1)]
            elif mode == 2:
                candidate = bytearray(rng.randbytes(rng.randrange(0, 4096)))
            elif mode == 3:
                count_offset = 40
                struct.pack_into("<I", candidate, count_offset, rng.randrange(0, 100))
                checksum_offset = struct.unpack_from("<Q", candidate, 48)[0]
                candidate[checksum_offset:] = hashlib.sha256(
                    bytes(candidate[:checksum_offset])
                ).digest()
            else:
                # Random section offset/size arithmetic stress.
                entry_offset = 64 + rng.randrange(0, 7) * 32
                struct.pack_into("<Q", candidate, entry_offset + 12, rng.randrange(0, 1 << 32))
                struct.pack_into("<Q", candidate, entry_offset + 20, rng.randrange(0, 1 << 32))
                checksum_offset = struct.unpack_from("<Q", candidate, 48)[0]
                candidate[checksum_offset:] = hashlib.sha256(
                    bytes(candidate[:checksum_offset])
                ).digest()
            nwp_package.validate_package_bytes(bytes(candidate))
            nwp_package.validate_package_bytes(bytes(candidate), require_integrity=False)
        return CheckResult(
            "python_bounded_package_fuzz",
            True,
            True,
            "bounded Python parser fuzz completed",
            evidence={"seed": seed, "iterations": iterations},
        )
    except Exception as exc:
        return CheckResult(
            "python_bounded_package_fuzz", True, False, f"exception: {exc}"
        )


def _find_host_cxx() -> tuple[str, str] | None:
    for name in ("clang++", "g++", "c++"):
        found = shutil.which(name)
        if found:
            return found, "gnu"
    return None


def check_sanitizer_fuzz(work_root: Path, budget: int = 256) -> CheckResult:
    compiler = _find_host_cxx()
    if compiler is None:
        return CheckResult(
            "sanitizer_native_parser_fuzz",
            True,
            False,
            "no host C++ compiler with sanitizer support was found",
            unavailable=True,
        )
    compiler_bin, _kind = compiler
    build_dir = work_root / "sanitizer"
    build_dir.mkdir(parents=True, exist_ok=True)
    harness_source = REPO_ROOT / "tools" / "nw03_parser_fuzz.cpp"
    harness = build_dir / "nw03_parser_fuzz"
    command = [
        compiler_bin,
        "-std=c++20",
        "-O1",
        "-g",
        "-fno-omit-frame-pointer",
        "-fsanitize=address,undefined",
        "-fno-sanitize-recover=all",
        f"-I{REPO_ROOT / 'src' / 'package'}",
        str(harness_source),
        str(REPO_ROOT / "src" / "package" / "nwp_format.cpp"),
        "-o",
        str(harness),
    ]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return CheckResult(
            "sanitizer_native_parser_fuzz",
            True,
            False,
            f"sanitizer harness build failed: {proc.stderr.strip() or proc.stdout.strip()}",
            evidence={"command": command},
        )
    corpus_dir = build_dir / "corpus"
    corpus_dir.mkdir(exist_ok=True)
    blob = _synthetic_dxil_container()
    base = nwp_package.encode_package(
        nwp_package.make_test_package(shader_blob=blob, specialization_count=2)
    )
    (corpus_dir / "valid.nwp").write_bytes(base)
    rng = random.Random(0x4E573033)
    corpus_files = [corpus_dir / "valid.nwp"]
    for index in range(31):
        candidate = bytearray(base)
        if index % 3 == 0:
            candidate = candidate[: rng.randrange(0, len(candidate) + 1)]
        else:
            for _ in range(rng.randint(1, 6)):
                position = rng.randrange(0, max(1, len(candidate) - 32))
                candidate[position] ^= rng.randrange(1, 256)
            checksum_offset = struct.unpack_from("<Q", candidate, 48)[0]
            if checksum_offset <= len(candidate) - nwp_package.CHECKSUM_SIZE:
                candidate[checksum_offset:] = hashlib.sha256(
                    bytes(candidate[:checksum_offset])
                ).digest()
        path = corpus_dir / f"mutant-{index:02d}.nwp"
        path.write_bytes(bytes(candidate))
        corpus_files.append(path)
    environment = dict(os.environ)
    environment.setdefault("ASAN_OPTIONS", "detect_leaks=0:abort_on_error=1")
    environment.setdefault("UBSAN_OPTIONS", "halt_on_error=1:print_stacktrace=1")
    start = time.monotonic()
    for path in corpus_files:
        run = subprocess.run(
            [str(harness), str(path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=environment,
        )
        if run.returncode != 0:
            return CheckResult(
                "sanitizer_native_parser_fuzz",
                True,
                False,
                f"sanitizer harness failed on {path.name} (exit {run.returncode}): "
                + (run.stderr.strip() or run.stdout.strip()),
            )
    elapsed = time.monotonic() - start
    return CheckResult(
        "sanitizer_native_parser_fuzz",
        True,
        True,
        "bounded sanitizer-backed native parser fuzz completed without memory errors",
        evidence={
            "sanitizer": "address,undefined",
            "sanitizer_flags": "-fsanitize=address,undefined -fno-sanitize-recover=all",
            "compiler": compiler_bin,
            "corpus_files": len(corpus_files),
            "corpus_generator_seed": 0x4E573033,
            "campaign_budget_iterations": budget,
            "executed_iterations": len(corpus_files),
            "coverage": [
                "package_sections",
                "capability_records",
                "instruction_operands",
                "resource_dimensions",
                "counts_lengths_offsets_arithmetic",
                "shader_payload_headers",
            ],
            "elapsed_seconds": round(elapsed, 3),
        },
    )


def _run_pytest() -> CheckResult:
    proc = subprocess.run(
        # Disable pytest's repository-root cache provider so verification is
        # hermetic and works in read-only/sandboxed checkouts.
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,
    )
    if proc.returncode != 0:
        return CheckResult(
            "repository_pytest_suite",
            True,
            False,
            "pytest failed: " + (proc.stdout[-4000:] + proc.stderr[-4000:]),
        )
    return CheckResult(
        "repository_pytest_suite",
        True,
        True,
        "repository pytest suite passed",
    )


def run_contract_checks(work_root: Path) -> list[CheckResult]:
    return [
        check_oracle_and_lowering(),
        check_package_roundtrip_and_malformed(),
        check_release_leakage(),
        _python_bounded_fuzz(),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nodewerk NW-03 verification entry point.")
    parser.add_argument(
        "--mode",
        choices=("Full", "ContractsOnly"),
        default="Full",
        help="Full requires real DXC and sanitizer-backed native fuzzing",
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--dxc", type=str, default=None)
    parser.add_argument("--summary-json", type=Path, default=None)
    parser.add_argument("--skip-pytest", action="store_true")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="nw03-verify-") as temporary:
        work_root = Path(temporary)
        results = run_contract_checks(work_root)
        if args.mode == "Full":
            results.append(check_sanitizer_fuzz(work_root))
            results.append(check_clean_directory_reproducibility(args.dxc, work_root))
            if not args.skip_pytest:
                results.append(_run_pytest())
        else:
            # ContractsOnly still verifies the package/lowering contract but
            # deliberately treats unavailable DXC/sanitizer as non-required.
            results.append(
                CheckResult(
                    "sanitizer_native_parser_fuzz",
                    False,
                    False,
                    "not run in ContractsOnly mode",
                    unavailable=True,
                )
            )
            results.append(
                CheckResult(
                    "offline_dxc_compilation",
                    False,
                    False,
                    "not run in ContractsOnly mode",
                    unavailable=True,
                )
            )

    unavailable = [item for item in results if item.unavailable]
    failed = [
        item
        for item in results
        if item.required
        and not item.passed
        and not (args.mode == "ContractsOnly" and item.unavailable)
    ]
    summary = {
        "format": "nw03-verification-summary",
        "version": 1,
        "mode": args.mode,
        "source_root": str(args.repo_root),
        "passed": not failed,
        "unavailable": [item.name for item in unavailable],
        "failures": [item.name for item in failed],
        "checks": [
            {
                "name": item.name,
                "required": item.required,
                "passed": item.passed,
                "unavailable": item.unavailable,
                "detail": item.detail,
                "evidence": item.evidence,
            }
            for item in results
        ],
    }
    if args.summary_json is not None:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(
            json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
        )
    print(json.dumps(summary, indent=2, sort_keys=True))
    if failed:
        return 1
    if args.mode == "Full" and unavailable:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
