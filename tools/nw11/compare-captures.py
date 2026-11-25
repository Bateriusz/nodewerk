#!/usr/bin/env python3
"""Independently compare NW-11 fixed-tick RGBA32F captures.

The measured executables write raw captures plus a manifest.  This
repository-owned tool, which is copied into every validation bundle, verifies
the manifest hashes and computes the fixed-quality image metrics directly from
the pixels.  Evidence therefore never relies on a self-reported image result
from the measured binary.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from array import array
from pathlib import Path
from typing import Any, Mapping

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(0, str(THIS_DIR.parent))

import nw11_contract as contract  # noqa: E402

CAPTURE_FORMAT = "nw11-capture-set"
RESULT_FORMAT = "nw11-image-result"
RESULT_VERSION = 1
CAPTURE_TICKS = (*contract.REFERENCE_FIXED_TICKS, contract.LOOP_TICKS)


class CaptureError(ValueError):
    """Raised for malformed, stale, or incomparable capture sets."""


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureError(f"cannot read capture manifest '{path}': {exc}") from exc
    if not isinstance(value, dict):
        raise CaptureError(f"capture manifest '{path}' is not an object")
    return value


def _validate_manifest(
    document: Mapping[str, Any], *, root: Path, label: str
) -> list[dict[str, Any]]:
    if document.get("format") != CAPTURE_FORMAT:
        raise CaptureError(f"{label} capture manifest has the wrong format")
    ticks = document.get("fixed_ticks")
    if not isinstance(ticks, list) or ticks != list(CAPTURE_TICKS):
        raise CaptureError(f"{label} captures do not cover the frozen fixed ticks")
    images = document.get("images")
    if not isinstance(images, list) or len(images) != len(ticks):
        raise CaptureError(f"{label} capture manifest is incomplete")
    result: list[dict[str, Any]] = []
    for expected_tick, image in zip(ticks, images):
        if not isinstance(image, Mapping):
            raise CaptureError(f"{label} capture entry is not an object")
        if int(image.get("tick", -1)) != int(expected_tick):
            raise CaptureError(f"{label} capture ticks are out of order")
        if str(image.get("format")) != "rgba32f":
            raise CaptureError(f"{label} capture format must be rgba32f")
        width = int(image.get("width", 0))
        height = int(image.get("height", 0))
        if width != contract.OUTPUT_WIDTH or height != contract.OUTPUT_HEIGHT:
            raise CaptureError(f"{label} capture resolution changed")
        relative = str(image.get("path", ""))
        if not relative or Path(relative).is_absolute():
            raise CaptureError(f"{label} capture path must be manifest-relative")
        path = (root / relative).resolve()
        if root.resolve() not in path.parents:
            raise CaptureError(f"{label} capture path escapes the capture root")
        if not path.is_file():
            raise CaptureError(f"{label} capture file is missing: {relative}")
        expected_size = width * height * 4 * 4
        if path.stat().st_size != expected_size:
            raise CaptureError(f"{label} capture file has the wrong byte size")
        actual = contract.sha256_file(path)
        declared = image.get("sha256")
        if declared is not None and str(declared) != actual:
            raise CaptureError(f"{label} capture hash changed: {relative}")
        result.append(
            {
                "tick": int(expected_tick),
                "path": path,
                "width": width,
                "height": height,
                "sha256": actual,
            }
        )
    return result


def _compare_pair(
    reference_path: Path, variant_path: Path
) -> tuple[float, float, float]:
    max_abs = 0.0
    squared = 0.0
    count = 0
    chunk_floats = 4096
    with reference_path.open("rb") as reference, variant_path.open("rb") as variant:
        while True:
            left_bytes = reference.read(chunk_floats * 4)
            right_bytes = variant.read(chunk_floats * 4)
            if not left_bytes and not right_bytes:
                break
            if len(left_bytes) != len(right_bytes):
                raise CaptureError("capture files have different lengths")
            left = array("f")
            right = array("f")
            left.frombytes(left_bytes)
            right.frombytes(right_bytes)
            if sys.byteorder != "little":
                left.byteswap()
                right.byteswap()
            for a, b in zip(left, right):
                if not math.isfinite(a) or not math.isfinite(b):
                    raise CaptureError("capture contains non-finite pixels")
                delta = abs(a - b)
                if delta > max_abs:
                    max_abs = delta
                squared += delta * delta
                count += 1
    if count == 0:
        raise CaptureError("capture comparison produced no samples")
    return max_abs, math.sqrt(squared / count), count


def compare(
    *,
    baseline_manifest: Path,
    variant_manifest: Path,
    reference_kind: str,
) -> dict[str, Any]:
    baseline_root = baseline_manifest.resolve().parent
    variant_root = variant_manifest.resolve().parent
    baseline = _validate_manifest(
        _load(baseline_manifest), root=baseline_root, label="baseline"
    )
    variant = _validate_manifest(
        _load(variant_manifest), root=variant_root, label="variant"
    )
    overall_max = 0.0
    overall_squared = 0.0
    overall_count = 0
    per_tick = []
    for reference, candidate in zip(baseline, variant):
        if reference["tick"] != candidate["tick"]:
            raise CaptureError("baseline and variant ticks do not match")
        max_abs, rmse, count = _compare_pair(reference["path"], candidate["path"])
        overall_max = max(overall_max, max_abs)
        overall_squared += rmse * rmse * count
        overall_count += count
        per_tick.append(
            {
                "tick": reference["tick"],
                "max_abs_channel_error": max_abs,
                "rmse": rmse,
                "baseline_sha256": reference["sha256"],
                "variant_sha256": candidate["sha256"],
            }
        )
    loop_boundary = {}
    for label, captures in (("baseline", baseline), ("variant", variant)):
        maximum, rmse, _ = _compare_pair(captures[0]["path"], captures[-1]["path"])
        loop_boundary[label] = {"max_abs_channel_error": maximum, "rmse": rmse}
        overall_max = max(overall_max, maximum)
    return {
        "format": RESULT_FORMAT,
        "version": RESULT_VERSION,
        "task": contract.TASK,
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "reference_kind": reference_kind,
        "fixed_ticks": list(contract.REFERENCE_FIXED_TICKS),
        "max_abs_channel_error": overall_max,
        "rmse": math.sqrt(overall_squared / overall_count),
        "baseline_manifest_sha256": contract.sha256_file(baseline_manifest),
        "variant_manifest_sha256": contract.sha256_file(variant_manifest),
        "per_tick": per_tick,
        "loop_boundary": loop_boundary,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-manifest", required=True)
    parser.add_argument("--variant-manifest", required=True)
    parser.add_argument(
        "--reference-kind",
        required=True,
        choices=("raster_reference", "analytic_reference", "sdf_reference"),
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        result = compare(
            baseline_manifest=Path(args.baseline_manifest),
            variant_manifest=Path(args.variant_manifest),
            reference_kind=args.reference_kind,
        )
    except CaptureError as exc:
        print(f"[NW11-CAPTURE-ERR] {exc}", file=sys.stderr)
        return 2
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    if result["max_abs_channel_error"] > contract.IMAGE_ABSOLUTE_TOLERANCE:
        return 10
    if result["rmse"] > contract.IMAGE_RMSE_TOLERANCE:
        return 10
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
