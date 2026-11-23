#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOOLCHAIN = ROOT / "benchmarks" / "toolchain-manifest.v1.json"
DEFAULT_SOURCES = ROOT / "tools" / "nw01" / "toolchain-sources.json"

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def build_key(toolchain_path: Path, sources_path: Path, runner_os: str, runner_arch: str) -> str:
    toolchain = json.loads(toolchain_path.read_text(encoding="utf-8"))
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    deps = toolchain["pinned_dependencies"]
    llvm = next(item for item in toolchain["evaluated_toolchains"] if item["toolchain_id"] == "clang-cl-lld")["compiler_version"]
    return "-".join([
        "nw01-portable-v1", runner_os.lower(), runner_arch.lower(),
        f"tc-{sha(toolchain_path)[:16]}", f"src-{sha(sources_path)[:16]}",
        f"cmake-{deps['cmake_version']}-{sources['cmake']['sha256'][:16]}",
        f"llvm-{llvm}-{sources['llvm']['sha256'][:16]}",
        f"dxc-{deps['dxc_version']}-{sources['dxc']['sha256'][:16]}",
    ])

def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--toolchain", type=Path, default=DEFAULT_TOOLCHAIN)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--runner-os", required=True)
    parser.add_argument("--runner-arch", required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    key = build_key(args.toolchain, args.sources, args.runner_os, args.runner_arch)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"key={key}\n")
    print(key)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
