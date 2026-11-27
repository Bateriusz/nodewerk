#!/usr/bin/env python3
"""Build a capture sibling of the shipped demo and publish honest WARP previews.

The diagnostic executable embeds the release package and manifest byte for byte.
Only the existing capture-enabled player is linked again; no scene or shader is
re-exported. These images are offline visual previews, not desktop/GPU evidence.
"""

from __future__ import annotations

import argparse
import binascii
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from typing import Any
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nw05_export as export
import nw05_optimizer as optimizer
import nwp_package as nwp

TICKS_PER_SECOND = 48_000
CAPTURE_TICKS = (192000, 480000, 768000, 960000, 1224000, 1344000,
                 1584000, 1824000, 1932000, 1968000, 2112000)
CAPTURE_EXE = "neon-breach-capture.exe"
BUILD_REPORT = "capture-build.json"


def _json(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def package_dimensions(package: nwp.NwpPackage) -> tuple[int, int]:
    values = {p.name: struct.unpack("<I", p.value)[0] for p in package.parameters
              if p.name in {"nw05.present.width", "nw05.present.height"}
              and p.type == "uint" and p.component_count == 1 and len(p.value) == 4}
    width, height = values.get("nw05.present.width", 0), values.get("nw05.present.height", 0)
    if not (0 < width <= 8192 and 0 < height <= 8192):
        raise ValueError("capture requires explicit, bounded package presentation dimensions")
    presented = [i.operands[0].value for i in package.frame
                 if i.opcode == "frame.present" and i.operands]
    outputs = [r for r in package.resources if r.resource_id in presented]
    if len(outputs) != 1 or outputs[0].size_bytes != width * height * 16:
        raise ValueError("package presentation size does not match its RGBA32F output")
    return width, height


def embedded_identity(executable: bytes) -> tuple[bytes, bytes, dict[str, Any]]:
    package_bytes, manifest_bytes = export.extract_embedded_package(executable)
    package = nwp.decode_package(package_bytes, allow_dev=False)
    manifest = json.loads(manifest_bytes)
    if optimizer.manifest_bytes(manifest) != manifest_bytes:
        raise ValueError("release executable must contain its complete canonical link manifest")
    expected_manifest = "sha256:v1:" + sha256(nwp.canonical_json_bytes(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )).hexdigest()
    if manifest.get("manifest_sha256") != expected_manifest:
        raise ValueError("embedded link manifest digest is invalid")
    if not package.audio_song or not manifest.get("audio"):
        raise ValueError("capture sibling requires the actual shipped audio package")
    width, height = package_dimensions(package)
    identity = {
        "package_sha256": sha256(package_bytes).hexdigest(),
        "manifest_sha256": sha256(manifest_bytes).hexdigest(),
        "nwas_sha256": sha256(package.audio_song).hexdigest(),
        "shader_sha256": {str(s.shader_id): sha256(s.blob).hexdigest() for s in package.shaders},
        "width": width, "height": height,
    }
    return package_bytes, manifest_bytes, identity


def build_capture(release: Path, template: Path, output: Path, build_root: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("capture player linking requires the pinned Windows toolchain")
    configuration = export._template_build_configuration(template)
    if (configuration.get("NW_BUILD_PROFILE") != "dev"
            or configuration.get("NW07_CAPTURE_CONTROLS", "").upper() != "ON"):
        raise ValueError("capture template must enable existing NW07_CAPTURE_CONTROLS in the dev profile")
    if output.exists():
        raise FileExistsError(output)
    release_bytes = release.read_bytes()
    package_bytes, manifest_bytes, identity = embedded_identity(release_bytes)
    manifest = json.loads(manifest_bytes)
    source = export.generate_player_source(
        package_sha256=identity["package_sha256"],
        manifest_sha256=manifest["manifest_sha256"], manifest=manifest,
        memory_budget_bytes=int(manifest["link_metadata"]["memory_budget_bytes"]),
        width=identity["width"], height=identity["height"],
    )
    player = export._build_specialized_player(
        template, source, manifest, nwp.COMMON_PROFILE_ID,
        build_root_override=build_root, frozen_embedded_specialization=False,
    )
    diagnostic = export.embed_package(player, package_bytes, manifest_bytes)
    actual_package, actual_manifest, actual_identity = embedded_identity(diagnostic)
    if (actual_package, actual_manifest, actual_identity) != (package_bytes, manifest_bytes, identity):
        raise RuntimeError("capture sibling changed the release package or manifest")
    output.mkdir(parents=True)
    (output / CAPTURE_EXE).write_bytes(diagnostic)
    _json(output / BUILD_REPORT, {
        "format": "nodewerk-demo-capture-build", "version": 1,
        "release_executable_sha256": sha256(release_bytes).hexdigest(),
        "diagnostic_executable_sha256": sha256(diagnostic).hexdigest(),
        "generated_player_source_sha256": sha256(source.encode("utf-8")).hexdigest(),
        "exact_release_payload": identity, "package_and_manifest_byte_identical": True,
        "diagnostic_runtime_dependencies": export._pe_runtime_dependencies(player),
        "capture_controls": "NW07_CAPTURE_CONTROLS", "ticks": list(CAPTURE_TICKS),
        "scope": "Headless WARP visual preview; no physical GPU, Present or audio-device claim.",
    })


def rgba32f_rgb(data: bytes, width: int, height: int) -> tuple[bytes, dict[str, Any]]:
    """Convert the already tonemapped presentation resource without another gamma."""
    if len(data) != width * height * 16:
        raise ValueError("native capture has an incorrect RGBA32F byte count")
    rgb = bytearray(width * height * 3)
    minimum, maximum = math.inf, -math.inf
    for pixel, rgba in enumerate(struct.iter_unpack("<4f", data)):
        if not all(map(math.isfinite, rgba)):
            raise ValueError(f"native capture contains a nonfinite component at pixel {pixel}")
        for channel in range(3):
            value = rgba[channel]
            minimum, maximum = min(minimum, value), max(maximum, value)
            rgb[pixel * 3 + channel] = round(min(1.0, max(0.0, value)) * 255)
    # Inspect the image interior, so a cue LED or border cannot make a blank
    # renderer appear to contain a scene or to change meaningfully over time.
    samples = bytearray()
    for sy in range(72):
        y = min(height - 1, int(height * (0.05 + 0.90 * (sy + 0.5) / 72)))
        for sx in range(128):
            x = min(width - 1, int(width * (0.05 + 0.90 * (sx + 0.5) / 128)))
            offset = (y * width + x) * 3
            samples.extend(rgb[offset:offset + 3])
    colors = {bytes(samples[i:i + 3]) for i in range(0, len(samples), 3)}
    return bytes(rgb), {
        "finite_rgba32f": True, "rgb_min": minimum, "rgb_max": maximum,
        "interior_distinct_colors": len(colors),
        "interior_sha256": sha256(samples).hexdigest(),
        "rgb8_sha256": sha256(rgb).hexdigest(),
    }


def write_png(path: Path, rgb: bytes, width: int, height: int) -> None:
    if len(rgb) != width * height * 3:
        raise ValueError("PNG dimensions do not match the RGB bytes")

    def chunk(kind: bytes, content: bytes) -> bytes:
        return (struct.pack(">I", len(content)) + kind + content
                + struct.pack(">I", binascii.crc32(kind + content) & 0xffffffff))

    stride = width * 3
    scanlines = b"".join(b"\0" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(scanlines, 6)) + chunk(b"IEND", b""))


def thumbnail(rgb: bytes, width: int, height: int, target_width: int = 384) -> tuple[bytes, int]:
    target_height = max(1, round(height * target_width / width))
    result = bytearray(target_width * target_height * 3)
    for y in range(target_height):
        for x in range(target_width):
            source = (min(height - 1, y * height // target_height) * width
                      + min(width - 1, x * width // target_width)) * 3
            target = (y * target_width + x) * 3
            result[target:target + 3] = rgb[source:source + 3]
    return bytes(result), target_height


def contact_sheet(path: Path, frames: list[tuple[int, bytes]], thumb_height: int) -> None:
    glyphs = {" ": bytes(7)}
    glyph_path = Path(__file__).resolve().parent.parent / "fixtures/nw12a/glyphs.txt"
    for line in glyph_path.read_text(encoding="ascii").splitlines():
        char, rows = line.split()
        glyphs[char] = bytes.fromhex(rows)
    columns, thumb_width, label_height = 3, 384, 24
    cell_height = thumb_height + label_height
    width, height = columns * thumb_width, math.ceil(len(frames) / columns) * cell_height
    canvas = bytearray(bytes((14, 18, 26)) * width * height)
    for index, (tick, rgb) in enumerate(frames):
        left, top = (index % columns) * thumb_width, (index // columns) * cell_height
        for y in range(thumb_height):
            start = ((top + label_height + y) * width + left) * 3
            canvas[start:start + thumb_width * 3] = rgb[y * thumb_width * 3:(y + 1) * thumb_width * 3]
        label = f"{tick / TICKS_PER_SECOND:05.2f} S"
        for letter, char in enumerate(label):
            for gy, bits in enumerate(glyphs.get(char, glyphs[" "])):
                for gx in range(5):
                    if bits & (1 << (4 - gx)):
                        for dy in range(2):
                            for dx in range(2):
                                offset = ((top + 5 + gy * 2 + dy) * width
                                          + left + 10 + letter * 12 + gx * 2 + dx) * 3
                                canvas[offset:offset + 3] = b"\xe8\xee\xf6"
    write_png(path, bytes(canvas), width, height)


def _runtime_environment() -> dict[str, str]:
    """Use the hosted runner's installed diagnostic CRT; never ship it in the demo."""
    environment = os.environ.copy()
    if os.name != "nt":
        return environment
    roots = [Path(value) for key in ("ProgramFiles", "ProgramFiles(x86)")
             if (value := environment.get(key))]
    directories: list[Path] = []
    for root in roots:
        directories.extend(root.glob("Microsoft Visual Studio/*/*/VC/Redist/MSVC/*/debug_nonredist/x64/Microsoft.VC*.DebugCRT"))
        kits = root / "Windows Kits/10"
        for pattern in ("bin/*/x64/ucrt", "bin/*/ucrt/DLLs/x64", "bin/x64/ucrt", "bin/ucrt/DLLs/x64"):
            directories.extend(kits.glob(pattern))
    environment["PATH"] = os.pathsep.join(str(p) for p in sorted(set(directories), reverse=True)) + os.pathsep + environment.get("PATH", "")
    return environment


def capture_bundle(bundle: Path, release: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "format": "nodewerk-demo-visual-preview", "version": 1, "status": "failed",
        "scope": "Offline headless WARP previews of the exact release package; no physical GPU, Present or WASAPI observation.",
        "frames": [],
    }
    try:
        build = json.loads((bundle / BUILD_REPORT).read_text(encoding="utf-8"))
        release_bytes = release.read_bytes()
        diagnostic = (bundle / CAPTURE_EXE).read_bytes()
        original_package, original_manifest, identity = embedded_identity(release_bytes)
        capture_package, capture_manifest, capture_identity = embedded_identity(diagnostic)
        if ((capture_package, capture_manifest) != (original_package, original_manifest)
                or capture_identity != identity or build["exact_release_payload"] != identity
                or build["release_executable_sha256"] != sha256(release_bytes).hexdigest()
                or build["diagnostic_executable_sha256"] != sha256(diagnostic).hexdigest()):
            raise ValueError("capture diagnostic is not bound to the shipped release bytes")
        if build["ticks"] != list(CAPTURE_TICKS):
            raise ValueError("capture build has a different shot selection")
        report["binding"] = build
        width, height = identity["width"], identity["height"]
        with tempfile.TemporaryDirectory(prefix=".nw12a-raw-", dir=output.parent) as temporary:
            raw = Path(temporary)
            command = [str((bundle / CAPTURE_EXE).resolve()), "--headless", "--allow-software-adapter",
                       "--tick", "0", "--capture-dir", str(raw.resolve())]
            for tick in CAPTURE_TICKS:
                command.extend(("--capture-tick", str(tick)))
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", check=False, timeout=600, env=_runtime_environment())
            (output / "capture.log").write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
            if result.returncode != 0:
                raise RuntimeError(f"native diagnostic capture exited {result.returncode}; see capture.log")
            if "NW05_LINK_MANIFEST_VERIFIED=1" not in result.stdout:
                raise RuntimeError("native diagnostic did not admit its embedded manifest")
            thumbnails: list[tuple[int, bytes]] = []
            thumb_height = 0
            for tick in CAPTURE_TICKS:
                if f"NW07_CAPTURE_TICK={tick}\n" not in result.stdout:
                    raise RuntimeError(f"native diagnostic did not acknowledge capture tick {tick}")
                data = (raw / f"tick_{tick}.rgba32f").read_bytes()
                rgb, statistics = rgba32f_rgb(data, width, height)
                name = f"frame-{tick:07d}.png"
                write_png(output / name, rgb, width, height)
                preview, thumb_height = thumbnail(rgb, width, height)
                thumbnails.append((tick, preview))
                report["frames"].append({
                    "tick": tick, "seconds": tick / TICKS_PER_SECOND, "png": name,
                    "width": width, "height": height, "rgba32f_sha256": sha256(data).hexdigest(),
                    "png_sha256": sha256((output / name).read_bytes()).hexdigest(), **statistics,
                })
            contact_sheet(output / "contact-sheet.png", thumbnails, thumb_height)
        if not any(frame["interior_distinct_colors"] > 1 for frame in report["frames"]):
            raise ValueError("all captured scene interiors are blank or uniform")
        if len({frame["interior_sha256"] for frame in report["frames"]}) < 2:
            raise ValueError("selected story shots all contain the same image interior")
        report["status"] = "passed"
        report["checks"] = {"exact_release_package": True, "finite_frames": True,
                            "visible_scene_content": True, "shots_change": True}
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        _json(output / "capture-report.json", report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="link an existing capture player around the shipped bytes")
    build.add_argument("--release", type=Path, required=True)
    build.add_argument("--player-template", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--build-root", type=Path, required=True)
    capture = commands.add_parser("capture", help="capture and validate offline previews on Windows")
    capture.add_argument("--bundle", type=Path, required=True)
    capture.add_argument("--release", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "build":
        build_capture(args.release, args.player_template, args.output, args.build_root)
    else:
        capture_bundle(args.bundle, args.release, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
