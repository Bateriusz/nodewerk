#!/usr/bin/env python3
"""Write deterministic compact-decoder parity fixtures for the C++ harness."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw04_fixture  # noqa: E402
import nw10_compact as compact  # noqa: E402
import nw10_contract as contract  # noqa: E402
import nwp_package as nwp  # noqa: E402


def write_fixtures(output: Path) -> dict[str, int]:
    output.mkdir(parents=True, exist_ok=True)
    package = nwp.encode_package(
        nwp.make_test_package(shader_blob=nw04_fixture.make_dxil_container())
    )
    stored = compact.encode_compact(
        package,
        codec_id=contract.CODEC_STORED_ID,
        layout_id=contract.LAYOUT_AOS_ID,
        delta_references=True,
    )
    mszip = compact.encode_compact(
        package,
        codec_id=contract.CODEC_MSZIP_ID,
        layout_id=contract.LAYOUT_SOA_ID,
        delta_references=True,
    )
    rows = {
        "canonical.bin": package,
        "stored.nwc": stored.bytes,
        "mszip.nwc": mszip.bytes,
    }
    for name, data in rows.items():
        (output / name).write_bytes(data)
    return {name: len(data) for name, data in rows.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    rows = write_fixtures(args.output)
    print(",".join(f"{name}={size}" for name, size in sorted(rows.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
