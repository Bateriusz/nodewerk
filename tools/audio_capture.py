"""Offline synchronized WAV + fixed-tick image sequence via the existing Studio host."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def frame_ticks(plan: Path) -> list[int]:
    with plan.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or len(rows) > 60 * 3600:
        raise ValueError("invalid frame plan size")
    ticks = []
    for index, row in enumerate(rows):
        tick = index * 800
        if row != {"frame": str(index), "tick": str(tick), "sample": str(tick)}:
            raise ValueError("frame plan is not the canonical 60 fps, 48 kHz mapping")
        ticks.append(tick)
    return ticks

def capture(offline: Path, host: Path, package: Path, song: Path,
            output: Path, *, width: int = 256, height: int = 256,
            timeout: int = 600) -> dict:
    if width <= 0 or height <= 0 or width > 8192 or height > 8192:
        raise ValueError("invalid capture dimensions")
    if output.exists():
        raise FileExistsError("capture destination must not exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    package, song, host, offline = (p.resolve(strict=True)
                                    for p in (package, song, host, offline))
    inputs = {key: sha256(value) for key, value in
              (("package", package), ("song", song), ("host", host), ("offline", offline))}
    with tempfile.TemporaryDirectory(prefix=".audio-capture-", dir=output.parent) as tmp:
        root = Path(tmp) / "capture"
        root.mkdir()
        pcm = root / "audio.wav"
        plan = root / "frames.csv"
        subprocess.run([str(offline), str(song), str(pcm), str(plan)],
                       check=True, timeout=timeout)
        ticks = frame_ticks(plan)
        if len(ticks) * width * height * 16 > 16 * 1024**3:
            raise ValueError("capture exceeds the bounded 16 GiB raw image budget")
        images = root / "images"
        images.mkdir()
        commands = "".join(f"render {tick}\n" for tick in ticks) + "quit\n"
        result = subprocess.run([str(host), "--package", str(package),
                                 "--width", str(width), "--height", str(height),
                                 "--capture-dir", str(images), "--session"],
                                input=commands, capture_output=True, text=True,
                                encoding="utf-8", errors="strict", timeout=timeout,
                                check=True)
        messages = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        if len(messages) != len(ticks) + 2 or messages[0].get("event") != "ready":
            raise ValueError("unexpected native capture session response")
        if messages[0].get("package_sha256") != inputs["package"]:
            raise ValueError("native capture package identity mismatch")
        frames = []
        for tick, row in zip(ticks, messages[1:-1]):
            if (row.get("event") != "rendered" or row.get("ok") is not True
                    or row.get("tick") != tick or row.get("capture") is not True):
                raise ValueError(f"native capture failed at tick {tick}")
            image = images / f"tick_{tick}.rgba32f"
            if image.stat().st_size != width * height * 16:
                raise ValueError("raw source dimensions do not match declared capture")
            frames.append({"tick": tick, "sample": tick,
                           "file": image.relative_to(root).as_posix(),
                           "sha256": sha256(image)})
        if messages[-1].get("event") != "shutdown" or messages[-1].get("ok") is not True:
            raise ValueError("native capture did not retire cleanly")
        if any(sha256(value) != inputs[key] for key, value in
               (("package", package), ("song", song), ("host", host), ("offline", offline))):
            raise ValueError("capture inputs changed during execution")
        report = {"format": "nodewerk-offline-audio-video", "version": 1,
                  "inputs_sha256": inputs, "width": width, "height": height,
                  "frame_rate": 60, "sample_rate": 48000,
                  "audio_sha256": sha256(pcm), "frame_plan_sha256": sha256(plan),
                  "images": frames, "physical_alignment": "not_measured"}
        (root / "capture.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                                          encoding="utf-8")
        root.rename(output)
    return report

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("offline", "host", "package", "song", "output"):
        parser.add_argument("--" + argument, type=Path, required=True)
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument("--height", type=int, default=256)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    capture(args.offline, args.host, args.package, args.song, args.output,
            width=args.width, height=args.height, timeout=args.timeout)

if __name__ == "__main__":
    main()
