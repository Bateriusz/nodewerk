"""Source-derived A/V cue bindings for optional NWAS v2 film playback."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

import audio_song
import nwp_package as nwp


FORMAT = "nodewerk-audio-presentation-cues"
VERSION = 1


def digest(schedule: Mapping[str, Any]) -> str:
    return hashlib.sha256(nwp.canonical_json_bytes(schedule)).hexdigest()


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _effect_indices(song: Mapping[str, Any], tick: int) -> list[int]:
    return [
        index
        for index, note in enumerate(song["notes"])
        if (note["tick"] == tick and note["bus"] == "effect" and note["velocity_milli"] > 0
            and song["instruments"][note["instrument"]]["gain_milli"] > 0)
    ]


def validate_schedule(
    schedule: Mapping[str, Any], song: Mapping[str, Any], *, song_sha256: str
) -> list[Mapping[str, Any]]:
    """Require complete, unambiguous declared visual/effect occurrences."""
    if (song.get("version") != audio_song.NWAS_V2_VERSION
            or not isinstance(schedule, Mapping)
            or set(schedule) != {"format", "version", "graph_sha256", "song_sha256", "cues"}
            or schedule["format"] != FORMAT or type(schedule["version"]) is not int
            or schedule["version"] != VERSION or not _sha256(schedule["graph_sha256"])
            or not _sha256(song_sha256) or schedule["song_sha256"] != song_sha256):
        raise ValueError("invalid film presentation cue schedule or song binding")
    cues = schedule["cues"]
    if not isinstance(cues, list) or not 1 <= len(cues) <= audio_song.MAX_NWAS_EVENTS:
        raise ValueError("physical A/V needs a nonempty emitted presentation cue schedule")
    previous = -1
    for cue in cues:
        if (not isinstance(cue, Mapping)
                or set(cue) != {"cue_id", "presentation_tick", "effect_note_index"}
                or not isinstance(cue["cue_id"], str)
                or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", cue["cue_id"]) is None):
            raise ValueError("invalid emitted presentation cue")
        tick, index = cue["presentation_tick"], cue["effect_note_index"]
        if (type(tick) is not int or not previous < tick < song["duration_ticks"]
                or type(index) is not int or _effect_indices(song, tick) != [index]):
            raise ValueError("presentation cue is not paired with one emitted effect note")
        previous = tick
    return cues


def from_film(document: Any, song_bytes: bytes) -> dict[str, Any] | None:
    """Describe the flash schedule used by the real shader, not action time.

    Export remains valid without suitable cues. Such a song/product can play,
    but it cannot be used to certify physical A/V alignment.
    """
    song = audio_song.decode_song(song_bytes)
    if song["version"] != audio_song.NWAS_V2_VERSION:
        return None
    import nw03_compiler
    import nw13_film
    import nw13_history

    active = {node.node_id for node in nw13_history.active_nodes(document)}
    roots = [node for node in nw03_compiler._reachable_shader_roots(document)
             if node.operator_id == nw13_film.OPERATOR and node.node_id in active]
    if len(roots) != 1 or not document.source_text:
        return None
    _, _, _, scene, settings, _ = nw13_film.read_film(document, roots[0])
    _timeline, onsets = nw13_film.film_timing(scene, settings)
    flash = settings.get("flash")
    bindings = settings.get("audio_cue_bindings")
    if (not onsets or not isinstance(bindings, dict) or flash["cue"] not in bindings.values()
            or scene.duration_ticks != song["duration_ticks"]
            or scene.lights[flash["light"]].intensity <= 0):
        return None
    cues = []
    for tick in onsets:
        indices = _effect_indices(song, tick)
        if len(indices) != 1:
            return None
        cues.append({"cue_id": flash["cue"], "presentation_tick": tick,
                     "effect_note_index": indices[0]})
    schedule = {
        "format": FORMAT, "version": VERSION,
        "graph_sha256": hashlib.sha256(document.source_text.encode("utf-8")).hexdigest(),
        "song_sha256": hashlib.sha256(song_bytes).hexdigest(), "cues": cues,
    }
    validate_schedule(schedule, song, song_sha256=schedule["song_sha256"])
    return schedule
