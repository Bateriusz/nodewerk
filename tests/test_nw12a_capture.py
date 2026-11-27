"""Portable preview integrity checks; native rendering remains in Windows CI."""

from dataclasses import replace
from hashlib import sha256
import binascii
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import zlib

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import audio_demo_capture as capture
import audio_song
import nw05_export as export
import nw05_optimizer as optimizer
import nwp_package as nwp

from test_nw05_export import _make_dxc


@pytest.fixture(scope="module")
def release_payload(tmp_path_factory):
    """One real package export, with test DXIL and a deliberately tiny image."""
    root = tmp_path_factory.mktemp("nw12a-capture")
    graph = root / "scene.nwgraph"
    source = (ROOT / "fixtures/nw06/procedural_scene.nwgraph").read_text(encoding="utf-8")
    source = source.replace("setting nw06_output_width 256",
                            "setting nw06_output_width 32\nsetting nw07_output_width 32")
    source = source.replace("setting nw06_output_height 256",
                            "setting nw06_output_height 18\nsetting nw07_output_height 18")
    graph.write_text(source, encoding="utf-8")
    song = audio_song.compile_song({"version": 1, "duration_ticks": 48000, "events": [
        {"tick": 0, "duration": 1000, "frequency_millihz": 440000, "gain": 8000},
    ]})
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")
        result = export.export_graph(
            graph, root / "export", allow_test_payloads=True,
            settings=export.ExportSettings(byte_ceiling=64 * 1024 * 1024,
                                            memory_budget=256 * 1024 * 1024),
            dxc_executable=str(_make_dxc(root / "dxc")), audio_song_payload=song,
        )
    artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
    return artifact.package_bytes, optimizer.manifest_bytes(artifact.manifest)


def stage_bundle(root, payload):
    package_bytes, manifest_bytes = payload
    # Test containers exercise the real embedding/identity readers, not a PE
    # loader. Only the native process below is replaced in portable tests.
    release = export.embed_package(b"test-release", package_bytes, manifest_bytes)
    diagnostic = export.embed_package(b"test-diagnostic", package_bytes, manifest_bytes)
    identity = capture.embedded_identity(release)[2]
    assert (identity["width"], identity["height"]) == (32, 18)
    release_path = root / "release.exe"
    release_path.write_bytes(release)
    bundle = root / "capture"
    bundle.mkdir()
    (bundle / capture.CAPTURE_EXE).write_bytes(diagnostic)
    binding = {
        "exact_release_payload": identity,
        "release_executable_sha256": sha256(release).hexdigest(),
        "diagnostic_executable_sha256": sha256(diagnostic).hexdigest(),
        "ticks": list(capture.CAPTURE_TICKS),
    }
    (bundle / capture.BUILD_REPORT).write_text(json.dumps(binding), encoding="utf-8")
    return release_path, bundle, binding


def simulated_native_capture(mode):
    def run(command, **kwargs):
        assert "--headless" in command and "--allow-software-adapter" in command
        assert command[command.index("--tick") + 1] == "0"
        assert "--validation" not in command
        target = Path(command[command.index("--capture-dir") + 1])
        for index, tick in enumerate(capture.CAPTURE_TICKS):
            if mode in {"blank", "led-only"}:
                data = bytearray(struct.pack("<4f", 0, 0, 0, 1) * (32 * 18))
                if mode == "led-only":
                    struct.pack_into("<f", data, 0, index % 2)
            else:
                blue = index / 20 if mode == "changing" else 0
                data = b"".join(struct.pack("<4f", x / 32, y / 18, blue, 1)
                                for y in range(18) for x in range(32))
            (target / f"tick_{tick}.rgba32f").write_bytes(data)
        return SimpleNamespace(
            returncode=0, stderr="", stdout="NW05_LINK_MANIFEST_VERIFIED=1\n"
            + "".join(f"NW07_CAPTURE_TICK={tick}\n" for tick in capture.CAPTURE_TICKS),
        )
    return run


def test_capture_rejects_a_self_consistent_diagnostic_for_a_different_package(
    tmp_path, monkeypatch, release_payload,
):
    release, bundle, binding = stage_bundle(tmp_path, release_payload)
    original_package, original_manifest = release_payload
    package = nwp.decode_package(original_package)
    song = audio_song.decode_song(package.audio_song)
    song["events"][0]["gain"] += 1
    replacement_song = audio_song.compile_song(song)
    replacement_package = nwp.encode_package(replace(package, audio_song=replacement_song))
    replacement_manifest = json.loads(original_manifest)
    export._attach_audio_manifest(replacement_manifest, replacement_song)
    export._refresh_manifest_sha256(replacement_manifest)
    diagnostic = export.embed_package(b"other-diagnostic", replacement_package,
                                      optimizer.manifest_bytes(replacement_manifest))
    (bundle / capture.CAPTURE_EXE).write_bytes(diagnostic)
    binding["diagnostic_executable_sha256"] = sha256(diagnostic).hexdigest()
    binding["exact_release_payload"] = capture.embedded_identity(diagnostic)[2]
    (bundle / capture.BUILD_REPORT).write_text(json.dumps(binding), encoding="utf-8")
    monkeypatch.setattr(capture.subprocess, "run", lambda *args, **kwargs: pytest.fail(
        "a different release payload must be rejected before native execution"))
    output = tmp_path / "preview"
    with pytest.raises(ValueError, match="not bound to the shipped release bytes"):
        capture.capture_bundle(bundle, release, output)
    assert json.loads((output / "capture-report.json").read_text())["status"] == "failed"


def test_rgba_conversion_and_png_preserve_pixels_and_reject_invalid_readbacks(tmp_path):
    raw = struct.pack("<8f", -1, .5, 2, 1, .25, .75, 1, 1)
    rgb, statistics = capture.rgba32f_rgb(raw, 2, 1)
    assert rgb == bytes((0, 128, 255, 64, 191, 255))
    assert statistics["finite_rgba32f"]
    path = tmp_path / "frame.png"
    capture.write_png(path, rgb, 2, 1)
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    cursor, compressed, kinds = 8, bytearray(), []
    while cursor < len(data):
        size = struct.unpack_from(">I", data, cursor)[0]
        kind, payload = data[cursor + 4:cursor + 8], data[cursor + 8:cursor + 8 + size]
        assert binascii.crc32(kind + payload) & 0xffffffff == struct.unpack_from(
            ">I", data, cursor + 8 + size)[0]
        if kind == b"IHDR":
            assert struct.unpack(">2I5B", payload) == (2, 1, 8, 2, 0, 0, 0)
        elif kind == b"IDAT":
            compressed.extend(payload)
        kinds.append(kind)
        cursor += 12 + size
    assert kinds == [b"IHDR", b"IDAT", b"IEND"] and cursor == len(data)
    assert zlib.decompress(compressed) == b"\0" + rgb
    with pytest.raises(ValueError, match="byte count"):
        capture.rgba32f_rgb(raw[:-1], 2, 1)
    for bad in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValueError, match="nonfinite"):
            capture.rgba32f_rgb(struct.pack("<4f", 0, 0, 0, bad), 1, 1)


@pytest.mark.parametrize("mode,error", [
    ("blank", "blank or uniform"),
    ("led-only", "blank or uniform"),
    ("unchanged", "same image interior"),
])
def test_capture_refuses_blank_led_only_or_unchanged_scenes(
    tmp_path, monkeypatch, release_payload, mode, error,
):
    release, bundle, _ = stage_bundle(tmp_path, release_payload)
    monkeypatch.setattr(capture.subprocess, "run", simulated_native_capture(mode))
    output = tmp_path / "preview"
    with pytest.raises(ValueError, match=error):
        capture.capture_bundle(bundle, release, output)
    report = json.loads((output / "capture-report.json").read_text())
    assert report["status"] == "failed" and len(report["frames"]) == 11
    assert error in report["error"]


def test_capture_publishes_all_selected_shots_bound_to_the_release(
    tmp_path, monkeypatch, release_payload,
):
    release, bundle, binding = stage_bundle(tmp_path, release_payload)
    monkeypatch.setattr(capture.subprocess, "run", simulated_native_capture("changing"))
    output = tmp_path / "preview"
    capture.capture_bundle(bundle, release, output)
    report = json.loads((output / "capture-report.json").read_text())
    assert report["status"] == "passed" and report["binding"] == binding
    assert [frame["tick"] for frame in report["frames"]] == list(capture.CAPTURE_TICKS)
    assert all(report["checks"].values())
    for frame in report["frames"]:
        assert frame["finite_rgba32f"]
        assert sha256((output / frame["png"]).read_bytes()).hexdigest() == frame["png_sha256"]
    assert (output / "contact-sheet.png").read_bytes().startswith(b"\x89PNG")
    assert (output / "capture.log").is_file()
