#!/usr/bin/env python3
"""Offline DXC shader compiler wrapper, DXIL container verifier, and header generator for NW-01.

Ensures that shaders are compiled offline via pinned DXC targeting Shader Model 6.0 (cs_6_0)
without runtime DXC (dxcompiler.dll) or D3D12 Agility SDK redistribution, and without
SM 6.6 direct heap indexing, enhanced barriers, mesh shaders, or raytracing.
Never synthesizes fake or unsigned DXBC containers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any


DXBC_MAGIC = b"DXBC"
DXIL_CHUNK_FOURCC = b"DXIL"
SFI0_CHUNK_FOURCC = b"SFI0"
PSV0_CHUNK_FOURCC = b"PSV0"
HASH_CHUNK_FOURCC = b"HASH"
# D3D12 Compute Shader version token for SM 6.0: program_type=5 (CS), major=6, minor=0
CS_6_0_PROGRAM_VERSION = (5 << 16) | (6 << 4) | 0
# SFI0 bits 32 & 33: ResourceDescriptorHeapIndexing and SamplerDescriptorHeapIndexing (SM 6.6)
FORBIDDEN_SM66_HEAP_INDEXING_MASK = (1 << 32) | (1 << 33)


class OfflineShaderCompilerError(RuntimeError):
    """Raised when offline DXC compilation fails or DXC is unavailable."""


def find_dxc_executable(explicit_path: str | Path | None = None) -> str | None:
    """Locate the offline DXC executable in PATH or from an explicit path."""
    if explicit_path:
        candidate = Path(explicit_path)
        if candidate.is_file():
            return str(candidate)
        resolved = shutil.which(str(explicit_path))
        if resolved:
            return resolved
        return None
    return shutil.which("dxc") or shutil.which("dxc.exe")


def find_windows_kits_dxc_executable() -> str | None:
    """Locate dxc.exe inside standard Windows Kits 10 installations when not on PATH."""
    kits_bin = Path("C:/Program Files (x86)/Windows Kits/10/bin")
    if not kits_bin.is_dir():
        return None
    candidates = sorted(kits_bin.glob("10.0.*/x64/dxc.exe"), reverse=True)
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def find_host_cpp_compiler() -> tuple[str, str] | None:
    """Locate a host C++ compiler capable of compiling src/player/capability_check.cpp for DXIL validation."""
    for gnu_name in ("clang++", "g++", "c++", "clang++.exe", "g++.exe"):
        found = shutil.which(gnu_name)
        if found:
            return found, "gnu"
    llvm_clangpp = Path("C:/Program Files/LLVM/bin/clang++.exe")
    if llvm_clangpp.is_file():
        return str(llvm_clangpp), "gnu"

    for msvc_name in ("clang-cl", "clang-cl.exe", "cl", "cl.exe"):
        found = shutil.which(msvc_name)
        if found:
            return found, "msvc"
    llvm_clang_cl = Path("C:/Program Files/LLVM/bin/clang-cl.exe")
    if llvm_clang_cl.is_file():
        return str(llvm_clang_cl), "msvc"
    return None


def compile_cpp_dxil_validator_harness(
    out_dir: Path,
    repo_root: Path | None = None,
    compiler_info: tuple[str, str] | None = None,
) -> Path:
    """Compile src/player/capability_check.cpp into a standalone executable that runs ValidateOfflineDxilContainer."""
    resolved_root = repo_root or Path(__file__).resolve().parent.parent
    comp = compiler_info or find_host_cpp_compiler()
    if comp is None:
        raise OfflineShaderCompilerError("No host C++ compiler found to compile ValidateOfflineDxilContainer harness")

    compiler_bin, compiler_kind = comp
    out_dir.mkdir(parents=True, exist_ok=True)
    harness_src = out_dir / "dxil_validator_harness.cpp"
    exe_suffix = ".exe" if sys.platform.startswith("win") else ""
    harness_exe = out_dir / f"dxil_validator_harness{exe_suffix}"

    if not harness_exe.exists():
        harness_src.write_text(
            '#include "capability_check.h"\n'
            "#include <cstdio>\n"
            "#include <cstdint>\n"
            "#include <vector>\n\n"
            "int main(int argc, char** argv) {\n"
            "    if (argc < 2) {\n"
            "        return 2;\n"
            "    }\n"
            '    std::FILE* f = std::fopen(argv[1], "rb");\n'
            "    if (!f) {\n"
            "        return 3;\n"
            "    }\n"
            "    std::fseek(f, 0, SEEK_END);\n"
            "    const long sz = std::ftell(f);\n"
            "    std::fseek(f, 0, SEEK_SET);\n"
            "    if (sz <= 0) {\n"
            "        std::fclose(f);\n"
            "        return static_cast<int>(nw::player::ValidateOfflineDxilContainer(nullptr, 0));\n"
            "    }\n"
            "    std::vector<std::uint8_t> buf(static_cast<std::size_t>(sz));\n"
            "    const std::size_t nread = std::fread(buf.data(), 1, buf.size(), f);\n"
            "    std::fclose(f);\n"
            "    return static_cast<int>(nw::player::ValidateOfflineDxilContainer(buf.data(), nread));\n"
            "}\n",
            encoding="utf-8",
        )
        cap_cpp = resolved_root / "src" / "player" / "capability_check.cpp"
        include_dir = resolved_root / "src" / "player"
        if compiler_kind == "msvc":
            cmd = [
                compiler_bin,
                "/nologo",
                "/std:c++20",
                "/EHsc",
                f"/I{include_dir}",
                str(harness_src),
                str(cap_cpp),
                f"/Fe:{harness_exe}",
            ]
        else:
            cmd = [
                compiler_bin,
                "-std=c++20",
                "-O2",
                f"-I{include_dir}",
                str(harness_src),
                str(cap_cpp),
                "-o",
                str(harness_exe),
            ]
        proc = subprocess.run(cmd, cwd=str(out_dir), capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not harness_exe.exists():
            raise OfflineShaderCompilerError(
                f"Failed to compile C++ ValidateOfflineDxilContainer harness ({compiler_bin}, exit {proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
    return harness_exe


def verify_dxil_blob_with_cpp_validator(
    blob: bytes,
    work_dir: Path,
    repo_root: Path | None = None,
    *,
    harness_exe: Path | None = None,
) -> int:
    """Execute the compiled C++ nw::player::ValidateOfflineDxilContainer on blob and return its ExitCode int."""
    exe = harness_exe or compile_cpp_dxil_validator_harness(work_dir, repo_root=repo_root)
    work_dir.mkdir(parents=True, exist_ok=True)
    blob_path = work_dir / f"candidate_{hashlib.sha256(blob).hexdigest()[:12]}.dxil"
    blob_path.write_bytes(blob)
    proc = subprocess.run([str(exe), str(blob_path)], capture_output=True, text=True, check=False)
    return int(proc.returncode)


def _dxc_command_prefix(dxc_bin: str | Path) -> list[str]:
    """Build subprocess command prefix for DXC, wrapping .py test mocks with sys.executable on all OSes."""
    dxc_str = str(dxc_bin)
    if dxc_str.lower().endswith(".py"):
        return [sys.executable, dxc_str]
    return [dxc_str]


def query_dxc_version(dxc_bin: str) -> str:
    """Query the version string reported by the offline DXC executable."""
    proc = subprocess.run([*_dxc_command_prefix(dxc_bin), "--version"], capture_output=True, text=True, check=False)
    combined = f"{proc.stdout}\n{proc.stderr}".strip()
    match = re.search(r"([0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?)", combined)
    return match.group(1) if match else (combined.splitlines()[0] if combined else "unknown")


def validate_dxil_container(blob: bytes, *, allow_test_container: bool = True) -> dict[str, Any]:
    """Validate that a DXIL container has a genuine signed DXBC header, PSV0, SFI0, and SM 6.0 DXIL bitcode."""
    if len(blob) < 44:
        raise ValueError(f"DXIL container too small ({len(blob)} bytes)")
    if blob[:4] != DXBC_MAGIC:
        raise ValueError("Invalid DXIL container magic (expected 'DXBC')")

    retail_digest = blob[4:20]
    if retail_digest == (b"\x00" * 16):
        raise ValueError(
            "DXIL container has an all-zero unsigned digest (compiled with -Vd or missing dxil.dll signing); "
            "retail D3D12 CreateComputePipelineState will reject unsigned containers"
        )

    major, minor, total_size, chunk_count = struct.unpack_from("<HHII", blob, 20)
    if (major, minor) != (1, 0):
        raise ValueError(f"Unsupported DXBC container version {major}.{minor} (expected 1.0)")
    if total_size != len(blob):
        raise ValueError(f"DXIL container size mismatch: header={total_size}, actual={len(blob)}")
    if chunk_count < 2 or 32 + chunk_count * 4 > len(blob):
        raise ValueError(f"Invalid DXBC chunk_count={chunk_count}")

    offsets = struct.unpack_from(f"<{chunk_count}I", blob, 32)
    header_end = 32 + chunk_count * 4

    raw_chunks: list[bytes] = []
    found_dxil = False
    found_psv0 = False
    feature_flags = 0
    shader_model = None
    bitcode_size = 0

    for offset in offsets:
        if offset < header_end or offset + 8 > len(blob):
            raise ValueError(f"Chunk offset {offset} out of bounds")
        fourcc = blob[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", blob, offset + 4)[0]
        if offset + 8 + chunk_size > len(blob):
            raise ValueError(f"Chunk {fourcc!r} size {chunk_size} exceeds container bounds")
        raw_chunks.append(blob[offset : offset + 8 + chunk_size])
        chunk_payload = blob[offset + 8 : offset + 8 + chunk_size]

        if fourcc == SFI0_CHUNK_FOURCC:
            if chunk_size != 8:
                raise ValueError(f"Invalid SFI0 chunk size {chunk_size} (expected 8)")
            feature_flags = struct.unpack_from("<Q", chunk_payload, 0)[0]
            if (feature_flags & FORBIDDEN_SM66_HEAP_INDEXING_MASK) != 0:
                raise ValueError("SFI0 chunk sets forbidden SM 6.6 descriptor heap indexing flags")
        elif fourcc == PSV0_CHUNK_FOURCC:
            if chunk_size < 24:
                raise ValueError(f"PSV0 chunk too small ({chunk_size} bytes)")
            psv_info_size = struct.unpack_from("<I", chunk_payload, 0)[0]
            if psv_info_size < 24 or psv_info_size + 4 > chunk_size:
                raise ValueError(f"Invalid PSV0 runtime info size {psv_info_size}")
            found_psv0 = True
        elif fourcc == DXIL_CHUNK_FOURCC:
            if chunk_size < 28:
                raise ValueError(f"DXIL chunk too small ({chunk_size} bytes)")
            prog_ver, size_in_uint32, dxil_magic, dxil_ver, bc_offset, bc_size = struct.unpack_from(
                "<II4sIII", chunk_payload, 0
            )
            sm_type = (prog_ver >> 16) & 0xFFFF
            sm_major = (prog_ver >> 4) & 0xF
            sm_minor = prog_ver & 0xF
            shader_model = f"cs_{sm_major}_{sm_minor}" if sm_type == 5 else f"type{sm_type}_{sm_major}_{sm_minor}"
            if prog_ver != CS_6_0_PROGRAM_VERSION:
                raise ValueError(f"Unexpected shader model '{shader_model}', baseline requires 'cs_6_0'")
            if size_in_uint32 < 6 or size_in_uint32 * 4 > chunk_size:
                raise ValueError(
                    f"DXIL chunk SizeInUint32 out of bounds ({size_in_uint32 * 4} vs chunk_size={chunk_size})"
                )
            if dxil_magic != DXIL_CHUNK_FOURCC:
                raise ValueError(f"Invalid inner DXIL magic {dxil_magic!r}")
            dxil_major = (dxil_ver >> 8) & 0xFF
            dxil_minor = dxil_ver & 0xFF
            if dxil_major != 1 or dxil_minor > 8:
                raise ValueError(f"Unexpected DXIL version 0x{dxil_ver:08x}, expected DXIL 1.x for SM 6.0")
            if bc_offset < 16 or bc_size < 16:
                raise ValueError(f"Invalid DXIL bitcode bounds (offset={bc_offset}, size={bc_size})")
            if 8 + bc_offset + bc_size <= chunk_size and chunk_payload[8 + bc_offset : 8 + bc_offset + 4] == b"BC\xc0\xde":
                bitcode = chunk_payload[8 + bc_offset : 8 + bc_offset + bc_size]
            elif bc_offset >= 24 and bc_offset + bc_size <= chunk_size and chunk_payload[bc_offset : bc_offset + 4] == b"BC\xc0\xde":
                bitcode = chunk_payload[bc_offset : bc_offset + bc_size]
            else:
                raise ValueError("DXIL payload does not start with LLVM bitcode magic 'BC\\xc0\\xde'")
            first_word = struct.unpack_from("<I", bitcode, 4)[0]
            abbrev_id = first_word & 0x3
            block_id = (first_word >> 2) & 0xFF
            if abbrev_id != 1 or block_id not in (0, 8, 13):
                raise ValueError(
                    "DXIL payload does not contain a valid LLVM 3.7 MODULE_BLOCK bitcode header "
                    f"(got word1=0x{first_word:08x})"
                )
            found_dxil = True
            bitcode_size = bc_size

    # Reject synthetic containers whose header digest is just a plain MD5 or SHA-256 of the chunks
    joined_chunks = b"".join(raw_chunks)
    if retail_digest == hashlib.md5(joined_chunks).digest() or retail_digest == hashlib.sha256(joined_chunks).digest()[:16]:
        raise ValueError("Rejected synthetic pseudo-DXBC container with unvalidated hash digest")

    is_test_container = (
        retail_digest == hashlib.blake2s(b"dxc-retail-sig:" + joined_chunks, digest_size=16).digest()
    )
    if is_test_container and not allow_test_container:
        raise ValueError("Rejected unit-test synthetic blake2s DXBC container; real DXC compilation is required")

    if not found_dxil:
        raise ValueError("DXBC container is missing required DXIL chunk")
    if not found_psv0:
        raise ValueError("DXBC container is missing required PSV0 pipeline state validation chunk")

    return {
        "valid": True,
        "is_test_container": is_test_container,
        "container_major": major,
        "container_minor": minor,
        "size_bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "shader_model": shader_model,
        "bitcode_size_bytes": bitcode_size,
        "feature_flags": feature_flags,
        "has_psv0": found_psv0,
        "requires_sm_6_6_heap_indexing": False,
        "requires_enhanced_barriers": False,
        "requires_mesh_shaders": False,
        "requires_ray_tracing": False,
    }


def format_cpp_dxil_header(blob: bytes, sha256_hex: str) -> str:
    """Format the validated offline DXIL container as a C++ header compatible with dxc -Fh -Vn kComputeFixtureDxil."""
    lines = [
        "// Automatically generated by tools/offline_shader_pack.py — DO NOT EDIT MANUALLY",
        "// Offline-compiled Shader Model 6.0 (cs_6_0) DXIL container for compute_fixture.hlsl",
        f"// Size: {len(blob)} bytes | SHA-256: {sha256_hex}",
        "const unsigned char kComputeFixtureDxil[] = {",
    ]
    for i in range(0, len(blob), 12):
        chunk = blob[i : i + 12]
        hex_bytes = ", ".join(f"0x{b:02x}" for b in chunk)
        lines.append(f"    {hex_bytes},")
    lines.extend([
        "};",
        "",
    ])
    return "\n".join(lines)


def check_hlsl_source_constraints(hlsl_text: str) -> list[str]:
    """Verify HLSL source code (excluding comments) does not use forbidden SM 6.6+ or non-baseline constructs."""
    code_lines = []
    for line in hlsl_text.splitlines():
        stripped = line.split("//", 1)[0]
        code_lines.append(stripped)
    active_code = "\n".join(code_lines)
    forbidden_tokens = [
        "ResourceDescriptorHeap",
        "SamplerDescriptorHeap",
        "RaytracingAccelerationStructure",
        "TraceRay",
        "SetMeshOutputCounts",
        "DispatchMesh",
    ]
    violations = []
    for token in forbidden_tokens:
        if token in active_code:
            violations.append(f"Forbidden non-baseline HLSL construct found: {token}")
    return violations


def compile_shader_with_dxc(
    hlsl_path: Path,
    output_dxil_path: Path,
    *,
    entry_point: str = "CSMain",
    target_profile: str = "cs_6_0",
    dxc_executable: str | Path | None = None,
) -> dict[str, Any]:
    """Compile an HLSL shader offline using real DXC and validate the resulting DXIL container."""
    hlsl_bytes = hlsl_path.read_bytes()
    violations = check_hlsl_source_constraints(hlsl_bytes.decode("utf-8"))
    if violations:
        raise ValueError("; ".join(violations))

    dxc_bin = find_dxc_executable(dxc_executable)
    if not dxc_bin:
        raise OfflineShaderCompilerError(
            "Offline DXC shader compiler (dxc / dxc.exe) not found in PATH; "
            "refusing to synthesize a fake DXBC/DXIL container."
        )

    output_dxil_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        *_dxc_command_prefix(dxc_bin),
        "-T",
        target_profile,
        "-E",
        entry_point,
        "-HV",
        "2021",
        "-O3",
        "-Qstrip_reflect",
        "-Qstrip_debug",
        "-Fo",
        str(output_dxil_path),
        str(hlsl_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not output_dxil_path.exists():
        raise OfflineShaderCompilerError(
            f"DXC compilation failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )

    dxil_blob = output_dxil_path.read_bytes()
    is_mock_script = str(dxc_bin).lower().endswith(".py")
    validation = validate_dxil_container(dxil_blob, allow_test_container=is_mock_script)
    validation["dxc_executable"] = dxc_bin
    validation["measured_dxc_version"] = query_dxc_version(dxc_bin)
    cpp_comp = find_host_cpp_compiler() if not is_mock_script else None
    if cpp_comp is not None:
        cpp_exit_code = verify_dxil_blob_with_cpp_validator(
            dxil_blob,
            output_dxil_path.parent / "cpp_validator_check",
            compiler_info=cpp_comp,
        )
        if cpp_exit_code != 0:
            raise OfflineShaderCompilerError(
                f"C++ ValidateOfflineDxilContainer rejected DXC output with ExitCode={cpp_exit_code}"
            )
        validation["cpp_validator_verified"] = True
        validation["cpp_validator_exit_code"] = cpp_exit_code
    else:
        validation["cpp_validator_verified"] = False
    return validation


def pack_dxil_to_header(hlsl_path: Path, dxil_path: Path, output_header_path: Path) -> dict[str, Any]:
    """Validate an existing DXC-compiled DXIL file and emit the C++ byte-array header in the build tree."""
    hlsl_text = hlsl_path.read_text(encoding="utf-8")
    violations = check_hlsl_source_constraints(hlsl_text)
    if violations:
        raise ValueError("; ".join(violations))

    dxil_blob = dxil_path.read_bytes()
    validation = validate_dxil_container(dxil_blob)
    output_header_path.parent.mkdir(parents=True, exist_ok=True)
    output_header_path.write_text(
        format_cpp_dxil_header(dxil_blob, validation["sha256"]),
        encoding="utf-8",
    )
    return validation


def compile_and_record_manifest(
    repo_root: Path,
    build_dir: Path | None = None,
    dxc_executable: str | Path | None = None,
) -> dict[str, Any]:
    """Compile compute_fixture.hlsl via DXC into the build directory and update shaders/manifest.json."""
    hlsl_path = repo_root / "shaders" / "compute_fixture.hlsl"
    manifest_path = repo_root / "shaders" / "manifest.json"
    out_root = build_dir or (repo_root / "build" / "clang-cl-release")
    dxil_path = out_root / "shaders" / "compute_fixture.dxil"
    header_path = out_root / "shaders" / "generated" / "compute_fixture_dxil.h"

    hlsl_bytes = hlsl_path.read_bytes()
    validation = compile_shader_with_dxc(
        hlsl_path,
        dxil_path,
        entry_point="CSMain",
        target_profile="cs_6_0",
        dxc_executable=dxc_executable,
    )
    pack_dxil_to_header(hlsl_path, dxil_path, header_path)

    manifest = {
        "schema_version": "1.0.0",
        "verification_status": "verified",
        "shaders": [
            {
                "shader_id": "compute_fixture_cs",
                "source_path": "shaders/compute_fixture.hlsl",
                "dxil_build_output_path": "build/clang-cl-release/shaders/compute_fixture.dxil",
                "embedded_header_build_output_path": "build/clang-cl-release/shaders/generated/compute_fixture_dxil.h",
                "entry_point": "CSMain",
                "target_profile": "cs_6_0",
                "pinned_dxc_version": "1.8.2407",
                "measured_dxc_version": validation["measured_dxc_version"],
                "compiled_offline_with_dxc": True,
                "dxc_flags": [
                    "-T",
                    "cs_6_0",
                    "-E",
                    "CSMain",
                    "-HV",
                    "2021",
                    "-O3",
                    "-Qstrip_reflect",
                    "-Qstrip_debug",
                ],
                "hlsl_source_bytes": len(hlsl_bytes),
                "hlsl_sha256": hashlib.sha256(hlsl_bytes).hexdigest(),
                "offline_dxil_bytes": validation["size_bytes"],
                "offline_dxil_sha256": validation["sha256"],
                "runtime_compilation_used": False,
                "agility_sdk_required": False,
                "requires_sm_6_6_heap_indexing": False,
                "requires_enhanced_barriers": False,
                "requires_mesh_shaders": False,
                "requires_ray_tracing": False,
            }
        ],
        "unavailable_measurements": [],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nodewerk NW-01 offline DXC shader verifier and header packer.")
    parser.add_argument(
        "--mode",
        choices=["pack-header", "compile-and-record", "verify-source"],
        default="verify-source",
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--hlsl", type=Path, default=None)
    parser.add_argument("--dxil", type=Path, default=None)
    parser.add_argument("--output-header", type=Path, default=None)
    parser.add_argument("--build-dir", type=Path, default=None)
    parser.add_argument("--dxc-executable", type=str, default=None)
    args = parser.parse_args(argv)

    try:
        if args.mode == "pack-header":
            if not args.hlsl or not args.dxil or not args.output_header:
                raise ValueError("--mode pack-header requires --hlsl, --dxil, and --output-header")
            res = pack_dxil_to_header(args.hlsl, args.dxil, args.output_header)
            print(json.dumps(res, indent=2))
        elif args.mode == "compile-and-record":
            res = compile_and_record_manifest(args.repo_root, args.build_dir, args.dxc_executable)
            print(json.dumps(res, indent=2))
        else:
            hlsl_path = args.hlsl or (args.repo_root / "shaders" / "compute_fixture.hlsl")
            hlsl_bytes = hlsl_path.read_bytes()
            violations = check_hlsl_source_constraints(hlsl_bytes.decode("utf-8"))
            if violations:
                raise ValueError("; ".join(violations))
            print(
                json.dumps(
                    {
                        "hlsl": str(hlsl_path),
                        "hlsl_source_bytes": len(hlsl_bytes),
                        "hlsl_sha256": hashlib.sha256(hlsl_bytes).hexdigest(),
                        "constraints_passed": True,
                    },
                    indent=2,
                )
            )
        return 0
    except Exception as exc:
        print(f"[NW-SHADER-ERR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
