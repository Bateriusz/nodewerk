"""Matched same-revision Windows PE builds; no endpoint or GPU is needed."""
from __future__ import annotations
import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
import dependency_check
import nwp_package as nwp
import nw05_export as export
from nw10_pe_ledger import pe_summary, section_deltas

ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / "fixtures/audio/sync_flash.nwgraph"
SONG = ROOT / "fixtures/audio/sync_song.json"

def _hash(data):
    return hashlib.sha256(data).hexdigest()

def _link_commands(root: Path) -> str:
    return subprocess.run(["ninja", "-C", str(root), "-t", "commands", "nw05_generated_player"],
                          check=True, capture_output=True, text=True).stdout.lower().replace("\\", "/")

def validate(template: Path, dxc: str, root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    settings = export.ExportSettings(player_template=str(template), byte_ceiling=64 * 1024 * 1024)
    results = {}
    for variant in ("silent", "audio"):
        outputs = []
        for repetition in ("a", "b"):
            name = variant + "-" + repetition
            build = root / (name + "-build")
            output = root / name
            assert not build.exists() and not output.exists()
            current = replace(settings, specialized_build_root=str(build),
                              audio_song_path=str(SONG) if variant == "audio" else "")
            result = export.export_graph(GRAPH, output, settings=current, dxc_executable=dxc)
            artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
            executable = output / artifact.executable_name
            payload, manifest = export.extract_embedded_package(executable.read_bytes())
            assert payload == artifact.package_bytes
            assert json.loads(manifest) == artifact.manifest
            model = nwp.decode_package(payload)
            assert model.audio_song is not None if variant == "audio" else model.audio_song is None
            commands = _link_commands(build)
            if variant == "silent":
                assert "src/audio/" not in commands
                assert "nw_wasapi_output" not in commands and "nw_procedural_audio" not in commands
                assert "nw_package_audio=0" in commands
                assert b"NWAS" not in executable.read_bytes()
            else:
                assert "src/audio/procedural_audio.cpp" in commands
                assert "src/audio/wasapi_output.cpp" in commands
            outputs.append((artifact, executable, build))
        first, second = outputs
        assert first[1].read_bytes() == second[1].read_bytes()
        assert first[0].package_bytes == second[0].package_bytes
        assert first[0].manifest == second[0].manifest
        results[variant] = first
    silent, silent_exe, silent_build = results["silent"]
    audio, audio_exe, audio_build = results["audio"]
    assert audio.package.audio_song
    assert export.section_sizes(audio.package_bytes)["AUDI"]["size_bytes"] == len(audio.package.audio_song)
    assert "AUDI" not in export.section_sizes(silent.package_bytes)
    assert audio.package_bytes != silent.package_bytes
    # Exact visual package equality after removing only the new optional section.
    assert nwp.encode_package(replace(nwp.decode_package(audio.package_bytes),
            audio_song=None, flags=audio.package.flags & ~nwp.AUDIO_EXTENSION_FLAG)) == silent.package_bytes
    silent_info = dependency_check.parse_pe64_sections_and_imports(silent_exe.read_bytes())
    audio_info = dependency_check.parse_pe64_sections_and_imports(audio_exe.read_bytes())
    def symbols(info):
        return {str(s).lower() for key in ("imports_by_dll", "delay_imports_by_dll")
                for values in info.get(key, {}).values() for s in values}
    assert "cocreateinstance" in symbols(audio_info)
    assert "cocreateinstance" not in symbols(silent_info)
    # Build native Windows targets from the actual audio-enabled configuration.
    subprocess.run(["cmake", "--build", str(audio_build), "--target",
                    "nw_audio_contract", "nw_audio_offline", "nw_audio_player", "nodewerkc",
                    "--parallel", "2"], check=True)
    subprocess.run([str(audio_build / "nw_audio_contract.exe")], check=True)
    native = subprocess.run([str(audio_build / "nodewerkc.exe"), "inspect",
                             str(audio_exe.parent / "common.nwp")],
                            check=True, capture_output=True, text=True)
    inspection = json.loads(native.stdout)
    assert inspection["valid"] and inspection["version"]["minor"] == 2
    assert inspection["audio_extension"]["song_sha256"] == _hash(audio.package.audio_song)
    # Same source and build profile, with only output-layer linkage disabled.
    probe_root = root / "synthesis-probe-build"
    source = (audio_exe.parent / "common.player.cpp").read_text(encoding="utf-8")
    probe = export._build_specialized_player(
        template, source, audio.manifest, nwp.COMMON_PROFILE_ID,
        build_root_override=probe_root, audio_measure_synthesis_only=True)
    probe_path = root / "synthesis-probe.exe"
    probe_path.write_bytes(probe)
    commands = _link_commands(probe_root)
    assert "src/audio/procedural_audio.cpp" in commands and "src/audio/wasapi_output.cpp" not in commands
    s, a, p = pe_summary(silent_exe), pe_summary(audio_exe), pe_summary(probe_path)
    song_bytes = len(audio.package.audio_song)
    # Raw file-size deltas include alignment and optimizer interactions;
    # do not misrepresent these as additive per-symbol sizes.
    return {
        "format": "nodewerk-audio-link-attribution", "version": 1,
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"],
                                                  cwd=ROOT, text=True).strip(),
        "independent_rebuilds": {"silent": True, "audio": True},
        "silent_closure": "verified_by_link_commands_package_and_pe_imports",
        "song_payload_bytes": song_bytes,
        "song_section_directory_bytes": nwp.SECTION_ENTRY_SIZE,
        "manifest_overhead_bytes": a["overlay_bytes"] - s["overlay_bytes"] -
                                    (len(audio.package_bytes) - len(silent.package_bytes)),
        "synthesis_and_package_reader_marginal_pe_bytes": p["pe_image_file_bytes"] - s["pe_image_file_bytes"],
        "wasapi_output_marginal_pe_bytes": a["pe_image_file_bytes"] - p["pe_image_file_bytes"],
        "actual_distributed_exe_delta_bytes": a["total_bytes"] - s["total_bytes"],
        "section_deltas_silent_to_synthesis": section_deltas(s, p),
        "section_deltas_synthesis_to_audio": section_deltas(p, a),
        "artifacts": {"silent": s, "audio": a, "nonshipping_synthesis_probe": p},
        "physical_cpu_and_alignment": "pending_physical_measurement",
        "attribution_method": "matched marginal PE raw sizes including linker padding and optimizer effects",
    }
