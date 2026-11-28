"""Bounded structural checks for retained physical A/V recordings.

This verifies the archive contains media-shaped bytes and its required tracks
have coherent locally stored sample data and presentation timing. It does not
decode samples or infer the measured cue times.
"""
from __future__ import annotations

import hashlib
import math
import os
import stat
import struct
from bisect import bisect_right
from dataclasses import dataclass
from fractions import Fraction
from itertools import chain
from pathlib import Path
from typing import BinaryIO, Iterator


_MAX_BOXES = 4096
_MAX_SAMPLES = 10_000_000
_MAX_CHUNKS = 1_000_000
_MAX_TIMING_RUNS = 1_000_000
_MAX_EDITS = 1024

_VIDEO_CONFIGS = {
    b"avc1": b"avcC", b"avc3": b"avcC", b"hvc1": b"hvcC", b"hev1": b"hvcC",
    b"av01": b"av1C", b"mp4v": b"esds",
}
_PRORES_TAGS = {b"apco", b"apcs", b"apcn", b"apch", b"ap4h", b"ap4x"}
_AUDIO_CONFIGS = {b"mp4a": b"esds", b"Opus": b"dOps", b"alac": b"alac"}
_MOV_PCM_TAGS = {b"raw ", b"twos", b"sowt", b"in24", b"in32", b"fl32", b"fl64", b"lpcm"}
_CONFIG_HEADER = {
    b"avcC": (7, b"\x01"), b"hvcC": (23, b"\x01"), b"av1C": (4, b"\x81"),
    b"esds": (8, b"\0\0\0\0\x03"), b"dOps": (11, b"\0"), b"alac": (28, b"\0\0\0\0"),
}
_AAC_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000,
              12000, 11025, 8000, 7350)


@dataclass(frozen=True)
class TrackTimeline:
    movie_scale: int
    media_scale: int
    edits: list[tuple[int, int, int]]
    samples: list[tuple[int, int]]

    def contains(self, relative_ticks: int, tick_rate: int) -> bool:
        movie = Fraction(relative_ticks * self.movie_scale, tick_rate)
        index = bisect_right(self.edits, movie, key=lambda edit: edit[0]) - 1
        if index < 0:
            return False
        start, end, media_start = self.edits[index]
        if movie >= end:
            return False
        media = media_start + (movie - start) * self.media_scale / self.movie_scale
        index = bisect_right(self.samples, media, key=lambda span: span[0]) - 1
        return index >= 0 and media < self.samples[index][1]


@dataclass(frozen=True)
class Capture:
    sha256: str
    tracks: dict[str, TrackTimeline]


def _read(stream: BinaryIO, start: int, count: int) -> bytes:
    stream.seek(start)
    data = stream.read(count)
    if len(data) != count:
        raise ValueError("truncated media record")
    return data


def _boxes(stream: BinaryIO, start: int, end: int,
           budget: list[int]) -> Iterator[tuple[bytes, int, int]]:
    while start < end:
        budget[0] -= 1
        if budget[0] < 0 or end - start < 8:
            raise ValueError("invalid or excessively complex media box table")
        width, kind = struct.unpack(">I4s", _read(stream, start, 8))
        header = 8
        if width == 1:
            if end - start < 16:
                raise ValueError("truncated large media box")
            width = struct.unpack(">Q", _read(stream, start + 8, 8))[0]
            header = 16
        elif width == 0:
            width = end - start
        if width < header or width > end - start:
            raise ValueError("media box extends outside its parent")
        yield kind, start + header, start + width
        start += width


def _child(rows: list[tuple[bytes, int, int]], kind: bytes) -> tuple[int, int] | None:
    matches = [(start, end) for tag, start, end in rows if tag == kind]
    if len(matches) > 1:
        raise ValueError("duplicate media metadata")
    return matches[0] if matches else None


def _table_count(stream: BinaryIO, box: tuple[int, int], label: str) -> int:
    start, end = box
    if end - start < 8:
        raise ValueError(f"truncated {label}")
    flags, count = struct.unpack(">II", _read(stream, start, 8))
    if flags:
        raise ValueError(f"unsupported {label} version or flags")
    return count


def _table_rows(stream: BinaryIO, start: int, count: int, fmt: str) -> Iterator[tuple[int, ...]]:
    width = struct.calcsize(fmt)
    while count:
        batch = min(count, (64 * 1024) // width)
        yield from struct.iter_unpack(fmt, _read(stream, start, batch * width))
        count -= batch
        start += batch * width


def _es_descriptor(stream: BinaryIO, start: int, end: int, tag: int,
                   minimum: int) -> tuple[int, int]:
    if start >= end or _read(stream, start, 1)[0] != tag:
        raise ValueError("missing MP4 sample description ES descriptor")
    offset = start + 1
    size = 0
    for i, byte in enumerate(_read(stream, offset, min(4, end - offset)), 1):
        size = (size << 7) | (byte & 0x7f)
        if not byte & 0x80:
            if minimum <= size <= end - offset - i:
                return offset + i, offset + i + size
            break
    raise ValueError("malformed MP4 sample description ES descriptor length")


def _aac_rates(stream: BinaryIO, start: int, end: int) -> set[int]:
    # AudioSpecificConfig, not the AAC media payload. Only read the bounded
    # prefix needed for the core frequency and any explicit HE-AAC extension.
    prefix = _read(stream, start, min(16, end - start))
    packed, remaining = int.from_bytes(prefix, "big"), len(prefix) * 8

    def take(count: int) -> int:
        nonlocal remaining
        remaining -= count
        if remaining < 0:
            raise ValueError("truncated MP4 AAC sample rate configuration")
        return (packed >> remaining) & ((1 << count) - 1)

    def frequency() -> int:
        index = take(4)
        rate = take(24) if index == 15 else (_AAC_RATES[index] if index < len(_AAC_RATES) else 0)
        if not rate:
            raise ValueError("invalid MP4 AAC sample rate configuration")
        return rate

    def audio_object_type() -> int:
        profile = take(5)
        profile = 32 + take(6) if profile == 31 else profile
        if profile not in (1, 2, 3, 4, 5, 6, 17, 19, 20, 23, 29, 39, 42):
            raise ValueError("unsupported MP4 AAC sample rate configuration")
        return profile

    profile = audio_object_type()
    rates = {frequency()}
    take(4)  # channelConfiguration
    if profile in (5, 29):
        rates.add(frequency())
        audio_object_type()
    return rates


def _codec_config(stream: BinaryIO, kind: bytes, start: int, end: int, budget: list[int],
                  *, quicktime_audio: bool = False,
                  audio_shape: tuple[float, int, int] | None = None) -> None:
    children = list(_boxes(stream, start, end, budget))
    if quicktime_audio:
        wrapper = _child(children, b"wave")
        if wrapper:
            children.extend(_boxes(stream, *wrapper, budget))
    box = _child(children, kind)
    minimum, signature = _CONFIG_HEADER[kind]
    if (not box or box[1] - box[0] < minimum
            or _read(stream, box[0], len(signature)) != signature):
        raise ValueError("missing or malformed MP4 sample description codec configuration")
    if kind == b"esds":
        es_start, es_end = _es_descriptor(stream, box[0] + 4, box[1], 0x03, 3)
        flags = _read(stream, es_start + 2, 1)[0]
        cursor = es_start + 3 + (2 if flags & 0x80 else 0)
        if flags & 0x40 and cursor < es_end:
            cursor += 1 + _read(stream, cursor, 1)[0]
        cursor += 2 if flags & 0x20 else 0
        decoder, decoder_end = _es_descriptor(stream, cursor, es_end, 0x04, 13)
        codec, stream_field = _read(stream, decoder, 2)
        types = (0x40, 0x66, 0x67, 0x68, 0x69, 0x6b) if quicktime_audio else (0x20,)
        if codec not in types or stream_field >> 2 != (5 if quicktime_audio else 4):
            raise ValueError("unsupported MP4 sample description MPEG codec or stream type")
        if codec in (0x69, 0x6b):
            if audio_shape is not None:
                raise ValueError("missing MP4 AAC sample rate configuration")
            return
        specific = _es_descriptor(stream, decoder + 13, decoder_end, 0x05, 1)
        if audio_shape is not None:
            rate = audio_shape[0]
            rates = _aac_rates(stream, *specific)
            # v0's 16.16 field cannot represent a whole rate above 65535 Hz.
            if (rate != 0 and rate not in rates) or (rate == 0 and max(rates) <= 65535):
                raise ValueError("MP4 AAC sample rate does not agree with its codec configuration")
    if kind == b"alac" and audio_shape is not None:
        rate, channels, bits = audio_shape
        config = _read(stream, box[0], 28)
        native_rate = int.from_bytes(config[24:28], "big")
        # ISO-BMFF v0 may leave the legacy 16.16 rate field zero for a
        # genuinely unrepresentable rate. Unlike the MOV v2 placeholder,
        # v0's sample-entry channel count and bit depth are still meaningful.
        matches_rate = native_rate > 65535 if rate == 0 else native_rate == rate
        if (not int.from_bytes(config[4:8], "big") or config[8] != 0
                or config[9] not in (16, 20, 24, 32) or bits not in (0, config[9])
                or config[13] != channels or not matches_rate):
            raise ValueError("MP4/MOV ALAC sample description disagrees with its codec configuration")


def _sample_entry(stream: BinaryIO, role: str, tag: bytes, start: int, end: int,
                  budget: list[int]) -> int:
    label = f"{role} MP4 sample description"
    if role == "video":
        if tag not in _VIDEO_CONFIGS and tag not in _PRORES_TAGS:
            raise ValueError(f"unsupported {label} type {tag!r}")
        if end - start < 78:
            raise ValueError(f"truncated {label}")
        header = _read(stream, start, 78)
        width, height = struct.unpack_from(">HH", header, 24)
        if not width or not height or not struct.unpack_from(">H", header, 40)[0] or header[42] > 31:
            raise ValueError(f"malformed {label} dimensions or frame fields")
        if tag in _VIDEO_CONFIGS:
            _codec_config(stream, _VIDEO_CONFIGS[tag], start + 78, end, budget)
    else:
        if tag not in _AUDIO_CONFIGS and tag not in _MOV_PCM_TAGS:
            raise ValueError(f"unsupported {label} type {tag!r}")
        if end - start < 28:
            raise ValueError(f"truncated {label}")
        header = _read(stream, start, 28)
        version = struct.unpack_from(">H", header, 8)[0]
        channels, bits = struct.unpack_from(">HH", header, 16)
        rate, size = struct.unpack_from(">I", header, 24)[0], 28
        compressed_v2 = version == 2 and tag in (b"mp4a", b"alac")
        overflow_compressed = version == 0 and tag in (b"mp4a", b"alac") and rate == 0
        if version == 1:
            size = 44
        elif version == 2 and (tag == b"lpcm" or compressed_v2):
            size = 64
            if end - start < size:
                raise ValueError(f"truncated {label} version 2 extension")
            layout, rate, channels, sentinel, bits, flags, _, _ = struct.unpack(
                ">IdIIIIII", _read(stream, start + 28, 36),
            )
            if layout != 72 or sentinel != 0x7f000000 or not math.isfinite(rate):
                raise ValueError(f"malformed {label} version 2 extension")
            if not compressed_v2 and flags & 1 and bits not in (32, 64):
                raise ValueError(f"malformed {label} floating-point PCM width")
        elif version != 0:
            raise ValueError(f"unsupported {label} version")
        widths = (8, 16, 24, 32, 64)
        if compressed_v2:
            widths += (0, 20)
        elif overflow_compressed and tag == b"alac":
            widths += (20,)
        if end - start < size or not channels or (rate <= 0 and not overflow_compressed) or bits not in widths:
            raise ValueError(f"malformed {label} channels, rate or sample width")
        if tag in _AUDIO_CONFIGS:
            audio_shape = (rate, channels, bits) if compressed_v2 or overflow_compressed else None
            _codec_config(stream, _AUDIO_CONFIGS[tag], start + size, end, budget,
                          quicktime_audio=True, audio_shape=audio_shape)
    if header[:6] != b"\0" * 6:
        raise ValueError(f"malformed {label} reserved bytes")
    return struct.unpack_from(">H", header, 6)[0]


def _description_references(stream: BinaryIO, role: str, descriptions: tuple[int, int],
                            references: tuple[int, int], budget: list[int]) -> list[bool]:
    description_count = _table_count(stream, descriptions, "sample descriptions")
    entries = list(_boxes(stream, descriptions[0] + 8, descriptions[1], budget))
    if len(entries) != description_count:
        raise ValueError("invalid sample descriptions")
    reference_count = _table_count(stream, references, "sample data references")
    refs = list(_boxes(stream, references[0] + 8, references[1], budget))
    if len(refs) != reference_count:
        raise ValueError("invalid sample data references")
    local_refs = [
        kind in (b"url ", b"alis") and end - start >= 4
        and _read(stream, start, 4) == b"\x00\x00\x00\x01"
        for kind, start, end in refs
    ]
    local_descriptions = []
    for tag, start, end in entries:
        index = _sample_entry(stream, role, tag, start, end, budget)
        local_descriptions.append(0 < index <= len(local_refs) and local_refs[index - 1])
    return local_descriptions


def _collect_track_chunks(stream: BinaryIO, role: str, rows: list[tuple[bytes, int, int]],
                          references: tuple[int, int] | None, data: list[tuple[int, int]],
                          spans: list[tuple[int, int]], budgets: tuple[list[int], list[int]]) -> int:
    size_box = _child(rows, b"stsz")
    offset32 = _child(rows, b"stco")
    offset64 = _child(rows, b"co64")
    descriptions = _child(rows, b"stsd")
    mapping = _child(rows, b"stsc")
    if offset32 and offset64:
        raise ValueError("ambiguous media chunk offsets")
    offsets = offset32 or offset64
    if not size_box or not offsets or not descriptions:
        return 0
    start, end = size_box
    if end - start < 12:
        raise ValueError("truncated sample size table")
    flags, fixed_size, sample_count = struct.unpack(">III", _read(stream, start, 12))
    if flags or end - start != 12 + (0 if fixed_size else sample_count * 4):
        raise ValueError("invalid sample size entries")
    if sample_count > _MAX_SAMPLES:
        raise ValueError("too many media samples for bounded capture validation")
    chunk_count = _table_count(stream, offsets, "media chunk offsets")
    offset_width = 4 if offset32 else 8
    if len(spans) + chunk_count > _MAX_CHUNKS or offsets[1] - offsets[0] != 8 + offset_width * chunk_count:
        raise ValueError("invalid media chunk offsets")
    if not sample_count and not chunk_count:
        return 0
    if not mapping:
        raise ValueError("missing media sample-to-chunk table")
    map_count = _table_count(stream, mapping, "media sample-to-chunk table")
    if not 0 < map_count <= chunk_count or mapping[1] - mapping[0] != 8 + 12 * map_count:
        raise ValueError("invalid media sample-to-chunk table")
    if references is None:
        raise ValueError("expected self-contained sample data references")
    box_budget, sample_budget = budgets
    local = _description_references(stream, role, descriptions, references, box_budget)
    if not fixed_size:
        sample_budget[0] -= sample_count
        if sample_budget[0] < 0:
            raise ValueError("too many media sample sizes for bounded capture validation")
    def sample_sizes() -> Iterator[int]:
        for (size,) in _table_rows(stream, start + 12, sample_count, ">I"):
            if not size:
                raise ValueError("media sample has no payload")
            yield size

    sizes = sample_sizes()
    chunk_offsets = _table_rows(stream, offsets[0] + 8, chunk_count, ">I" if offset32 else ">Q")
    maps = _table_rows(stream, mapping[0] + 8, map_count, ">III")
    previous = next(maps)
    if previous[0] != 1:
        raise ValueError("media sample-to-chunk table must begin at the first chunk")
    assigned_samples = 0
    # stsc rows specify runs: each row applies to its first_chunk up to
    # (but not including) the next row's first_chunk.
    for upcoming in chain(maps, [(chunk_count + 1, 0, 0)]):
        first, per_chunk, description = previous
        next_first = upcoming[0]
        if not first < next_first <= chunk_count + 1 or not per_chunk or not 0 < description <= len(local):
            raise ValueError("invalid media sample-to-chunk table")
        if not local[description - 1]:
            raise ValueError("expected a self-contained sample description")
        assigned_samples += (next_first - first) * per_chunk
        if assigned_samples > sample_count:
            raise ValueError("media sample count disagrees with its chunk mapping")
        for _ in range(next_first - first):
            (offset,) = next(chunk_offsets)
            length = fixed_size * per_chunk if fixed_size else sum(next(sizes) for _ in range(per_chunk))
            media_index = bisect_right(data, offset, key=lambda span: span[0]) - 1
            if not length or media_index < 0 or offset + length > data[media_index][1]:
                raise ValueError("media chunk lies outside its media payload or has no sample data")
            spans.append((offset, offset + length))
        previous = upcoming
    if assigned_samples != sample_count:
        raise ValueError("media sample count disagrees with its chunk mapping")
    return sample_count


def _movie_or_media_time(stream: BinaryIO, box: tuple[int, int] | None) -> tuple[int, int]:
    if not box or box[1] - box[0] < 20:
        raise ValueError("missing or truncated MP4 movie or media timing")
    version = _read(stream, box[0], 1)[0]
    if version == 0:
        scale, duration = struct.unpack(">II", _read(stream, box[0] + 12, 8))
    elif version == 1 and box[1] - box[0] >= 32:
        scale, duration = struct.unpack(">IQ", _read(stream, box[0] + 20, 12))
    else:
        raise ValueError("unsupported MP4 movie or media timing")
    if not scale or not duration:
        raise ValueError("MP4 movie and media timing must have a positive scale and duration")
    return scale, duration


def _track_duration(stream: BinaryIO, header: tuple[int, int] | None) -> int:
    if not header or header[1] - header[0] < 24:
        raise ValueError("missing or truncated MP4 track timing")
    start, end = header
    version = _read(stream, start, 1)[0]
    if version == 0:
        return int.from_bytes(_read(stream, start + 20, 4), "big")
    if version == 1 and end - start >= 36:
        return int.from_bytes(_read(stream, start + 28, 8), "big")
    raise ValueError("unsupported MP4 track timing")


def _edit_ranges(stream: BinaryIO, edits: tuple[int, int] | None,
                 duration: int) -> list[tuple[int, int, int]]:
    if edits is None:
        return [(0, duration, 0)]
    start, end = edits
    if end - start < 8:
        raise ValueError("truncated MP4 edit list")
    version_flags, count = struct.unpack(">II", _read(stream, start, 8))
    fmt = {0: ">Iihh", 1 << 24: ">Qqhh"}.get(version_flags)
    if not fmt or not 0 < count <= _MAX_EDITS or end - start != 8 + count * struct.calcsize(fmt):
        raise ValueError("invalid or unsupported MP4 edit list")
    result = []
    cursor = 0
    for length, media_start, rate, fraction in _table_rows(stream, start + 8, count, fmt):
        if not length or media_start < -1 or (rate, fraction) != (1, 0):
            raise ValueError("expected ordinary playback or empty MP4 edits")
        if media_start != -1 and cursor < duration:
            result.append((cursor, min(cursor + length, duration), media_start))
        cursor += length
    return result


def _sample_times(stream: BinaryIO, rows: list[tuple[bytes, int, int]], sample_count: int,
                  media_duration: int, timing_budget: list[int]) -> list[tuple[int, int]]:
    decode, composition = _child(rows, b"stts"), _child(rows, b"ctts")
    if not decode:
        raise ValueError("missing MP4 sample timing")
    count = _table_count(stream, decode, "MP4 decoding times")
    if not 0 < count <= sample_count or decode[1] - decode[0] != 8 + 8 * count:
        raise ValueError("invalid MP4 decoding times")
    timing_budget[0] -= count
    if composition:
        start, end = composition
        if end - start < 8:
            raise ValueError("truncated MP4 composition times")
        flags, entries = struct.unpack(">II", _read(stream, start, 8))
        if flags not in (0, 1 << 24) or not 0 < entries <= sample_count or end - start != 8 + 8 * entries:
            raise ValueError("invalid MP4 composition times")
        timing_budget[0] -= entries
        offsets = _table_rows(stream, start + 8, entries, ">Ii" if flags else ">II")
    else:
        offsets = iter([(sample_count, 0)])
    if timing_budget[0] < 0:
        raise ValueError("too many MP4 timing entries for bounded capture validation")

    spans = []
    used = dts = remaining = shift = 0
    for run, delta in _table_rows(stream, decode[0] + 8, count, ">II"):
        used += run
        if not run or not delta or used > sample_count:
            raise ValueError("MP4 decoding times disagree with the sample count")
        while run:
            if not remaining:
                remaining, shift = next(offsets, (0, 0))
                if not remaining:
                    raise ValueError("MP4 composition times disagree with the sample count")
            group = min(run, remaining)
            duration = group * delta
            spans.append((dts + shift, dts + shift + duration))
            dts += duration
            run -= group
            remaining -= group
    if used != sample_count or remaining or next(offsets, None) is not None or dts != media_duration:
        raise ValueError("MP4 sample timing disagrees with the sample count or media duration")
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _mp4_tracks(stream: BinaryIO, length: int, required: set[str]) -> dict[str, TrackTimeline]:
    budget = [_MAX_BOXES]
    top = list(_boxes(stream, 0, length, budget))
    if any(kind == b"moof" for kind, _, _ in top):
        raise ValueError("fragmented MP4 is unsupported; capture to finalized non-fragmented MP4/MOV")
    file_type, movie = _child(top, b"ftyp"), _child(top, b"moov")
    data = [(start, end) for kind, start, end in top if kind == b"mdat" and end > start]
    if not file_type or file_type[1] - file_type[0] < 8 or not movie or not data:
        raise ValueError("expected a finalized MP4/MOV with media payload")
    movie_children = list(_boxes(stream, *movie, budget))
    movie_scale, movie_duration = _movie_or_media_time(stream, _child(movie_children, b"mvhd"))
    tracks = {}
    spans: list[tuple[int, int]] = []
    sample_budget = [_MAX_SAMPLES]
    timing_budget = [_MAX_TIMING_RUNS]
    for tag, start, end in movie_children:
        if tag != b"trak":
            continue
        track_entries = list(_boxes(stream, start, end, budget))
        media = _child(track_entries, b"mdia")
        if not media:
            continue
        entries = list(_boxes(stream, *media, budget))
        handler, info = _child(entries, b"hdlr"), _child(entries, b"minf")
        if not handler or handler[1] - handler[0] < 12 or not info:
            continue
        kind = _read(stream, handler[0] + 8, 4)
        role = {b"soun": "audio", b"vide": "video"}.get(kind)
        if not role:
            continue
        media_header = _child(entries, b"mdhd")
        info_entries = list(_boxes(stream, *info, budget))
        table, data_info = _child(info_entries, b"stbl"), _child(info_entries, b"dinf")
        references = _child(list(_boxes(stream, *data_info, budget)), b"dref") if data_info else None
        if table:
            rows = list(_boxes(stream, *table, budget))
            count = _collect_track_chunks(stream, role, rows, references, data, spans, (budget, sample_budget))
            if count and role in required:
                if role in tracks:
                    raise ValueError(f"recording has multiple {role} tracks; use an unambiguous capture")
                media_scale, media_duration = _movie_or_media_time(stream, media_header)
                track_duration = min(movie_duration, _track_duration(stream, _child(track_entries, b"tkhd")))
                edit_box = _child(track_entries, b"edts")
                edits = _child(list(_boxes(stream, *edit_box, budget)), b"elst") if edit_box else None
                tracks[role] = TrackTimeline(movie_scale, media_scale, _edit_ranges(stream, edits, track_duration),
                                             _sample_times(stream, rows, count, media_duration, timing_budget))
    spans.sort()
    last = 0
    for start, end in spans:
        if start < last:
            raise ValueError("media chunks overlap")
        last = end
    return tracks


def _wave_format(stream: BinaryIO, start: int, size: int) -> tuple[int, int]:
    if size < 16:
        raise ValueError("truncated PCM WAV format")
    fmt, channels, rate, bytes_per_sec, alignment, bits = struct.unpack(
        "<HHIIHH", _read(stream, start, 16),
    )
    extension = 0
    if size != 16:
        if size < 18:
            raise ValueError("truncated PCM WAV format extension")
        extension = struct.unpack("<H", _read(stream, start + 16, 2))[0]
        if extension != size - 18:
            raise ValueError("incorrect PCM WAV format extension length")
    valid_bits = bits
    if fmt == 0xfffe:
        if extension < 22:
            raise ValueError("truncated PCM WAV extensible format")
        valid_bits, channel_mask, fmt, guid_suffix = struct.unpack(
            "<HIH14s", _read(stream, start + 18, 22),
        )
        if (guid_suffix != b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
                or channel_mask and channel_mask.bit_count() != channels):
            raise ValueError("malformed PCM WAV extensible subformat or channels")
    if (fmt == 1 and (bits not in (8, 16, 24, 32, 64) or not 0 < valid_bits <= bits)
            or fmt == 3 and (bits not in (32, 64) or valid_bits != bits)
            or fmt not in (1, 3)):
        raise ValueError("unsupported PCM WAV integer or floating-point sample width")
    if (not channels or not rate or alignment != channels * (bits // 8)
            or bytes_per_sec != rate * alignment):
        raise ValueError("invalid PCM WAV channels, rate or frame alignment")
    return rate, alignment


def _wave_tracks(stream: BinaryIO, length: int) -> dict[str, TrackTimeline]:
    header = _read(stream, 0, 12)
    if header[8:] != b"WAVE" or struct.unpack_from("<I", header, 4)[0] != length - 8:
        raise ValueError("invalid PCM WAV recording length")
    start = 12
    alignment = 0
    data_size = None
    budget = _MAX_BOXES
    while start < length:
        budget -= 1
        if budget < 0 or length - start < 8:
            raise ValueError("invalid WAV chunk table")
        kind, count = struct.unpack("<4sI", _read(stream, start, 8))
        content = start + 8
        start = content + count + (count & 1)
        if start > length:
            raise ValueError("truncated WAV chunk")
        if kind == b"fmt ":
            if alignment:
                raise ValueError("duplicate PCM WAV format")
            rate, alignment = _wave_format(stream, content, count)
        if kind == b"data":
            if data_size is not None:
                raise ValueError("duplicate WAV sample payload")
            data_size = count
    if alignment and data_size is not None and data_size >= alignment and data_size % alignment == 0:
        frames = data_size // alignment
        return {"audio": TrackTimeline(rate, rate, [(0, frames, 0)], [(0, frames)])}
    return {}


def inspect_capture(path: Path, required: set[str], *, check_container: bool) -> Capture:
    """Hash and inspect the same open regular capture file."""
    label = " and ".join(sorted(required)) + " capture"
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"{label} must be a regular recording file")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} must be a regular recording file")
        if not before.st_size:
            raise ValueError(f"{label} is empty")
        tracks = {}
        if check_container:
            try:
                prefix = _read(stream, 0, 8)
                if prefix[:4] == b"\x1a\x45\xdf\xa3":
                    raise ValueError("MKV/WebM is unsupported; capture to finalized MP4/MOV")
                tracks = (_wave_tracks(stream, before.st_size) if prefix[:4] == b"RIFF"
                          else _mp4_tracks(stream, before.st_size, required))
            except ValueError as exc:
                raise ValueError(f"{label} has an invalid or unsupported recording container: {exc}") from exc
            if not required.issubset(tracks):
                raise ValueError(f"{label} does not contain data for each required track")
        stream.seek(0)
        digest = hashlib.sha256()
        total = 0
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(stream.fileno())
        if (total != before.st_size or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns or before.st_ctime_ns != after.st_ctime_ns):
            raise ValueError(f"{label} changed during verification")
    return Capture(digest.hexdigest(), tracks)
