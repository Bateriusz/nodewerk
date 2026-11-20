from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
from pathlib import Path
from typing import Iterable, Mapping, Sequence


_GPU_FIELDS = ("GPUTime", "MsGPUTime")
_CPU_BUSY_FIELDS = ("CPUBusy", "MsCPUBusy")
_PRESENT_API_FIELDS = ("MsInPresentAPI", "InPresentAPI")
_DISPLAY_FIELDS = ("MsBetweenDisplayChange", "DisplayedTime", "MsDisplayedTime")


def _first_present(row: Mapping[str, str], names: Sequence[str]) -> str | None:
    for name in names:
        if name in row:
            return row[name]
    return None


def _metric_value(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text or text.upper() in {"NA", "N/A", "NAN"}:
        return None
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"invalid PresentMon metric value: {raw!r}")
    return value


def extract_samples(
    rows: Iterable[Mapping[str, str]],
    *,
    process_id: int,
) -> tuple[list[float], list[float], list[float]]:
    gpu: list[float] = []
    cpu: list[float] = []
    displayed: list[float] = []
    for row in rows:
        try:
            row_pid = int(str(row.get("ProcessID", "")).strip())
        except ValueError:
            continue
        if row_pid != process_id:
            continue
        gpu_value = _metric_value(_first_present(row, _GPU_FIELDS))
        cpu_busy = _metric_value(_first_present(row, _CPU_BUSY_FIELDS))
        present_api = _metric_value(_first_present(row, _PRESENT_API_FIELDS))
        display_value = _metric_value(_first_present(row, _DISPLAY_FIELDS))
        if gpu_value is not None:
            gpu.append(gpu_value)
        if cpu_busy is not None and present_api is not None:
            cpu.append(cpu_busy + present_api)
        if display_value is not None:
            displayed.append(display_value)
    return gpu, cpu, displayed


def _write_samples(path: Path, values: Sequence[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{value:.9f}\n" for value in values), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"required environment variable {name} is not set")
    return value


def run_presentmon(
    presentmon: Path,
    *,
    process_id: int,
    run_seconds: float,
    run_dir: Path,
    run_index: str,
) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    csv_path = run_dir / "presentmon.csv"
    session = f"NW07-{process_id}-{run_index}"
    command = [
        str(presentmon),
        "--process_id",
        str(process_id),
        "--timed",
        str(run_seconds),
        "--terminate_after_timed",
        "--terminate_on_proc_exit",
        "--output_file",
        str(csv_path),
        "--no_console_stats",
        "--qpc_time_ms",
        "--session_name",
        session,
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=run_seconds + 20.0,
    )
    metadata = {
        "format": "nw07-presentmon-collector",
        "version": 1,
        "command": command,
        "process_id": process_id,
        "run_seconds": run_seconds,
        "presentmon_path": str(presentmon.resolve()),
        "presentmon_sha256": _sha256_file(presentmon),
        "exit_code": completed.returncode,
        "stdout": completed.stdout[-8000:],
        "stderr": completed.stderr[-8000:],
    }
    (run_dir / "presentmon-metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "PresentMon failed: "
            + (completed.stderr.strip() or completed.stdout.strip() or str(completed.returncode))
        )
    if not csv_path.is_file() or csv_path.stat().st_size == 0:
        raise RuntimeError("PresentMon completed without producing a CSV")
    return csv_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect NW-07 raw GPU/CPU/display samples using PresentMon."
    )
    parser.add_argument("--presentmon", type=Path, required=True)
    args = parser.parse_args(argv)

    process_id = int(_required_env("NW07_PLAYER_PID"))
    run_seconds = float(_required_env("NW07_RUN_SECONDS"))
    run_dir = Path(_required_env("NW07_RUN_DIR"))
    run_index = _required_env("NW07_RUN_INDEX")
    gpu_path = Path(_required_env("NW07_GPU_SAMPLES"))
    cpu_path = Path(_required_env("NW07_CPU_SAMPLES"))
    displayed_path = Path(_required_env("NW07_DISPLAYED_SAMPLES"))

    if not args.presentmon.is_file():
        raise FileNotFoundError(f"PresentMon executable not found: {args.presentmon}")

    csv_path = run_presentmon(
        args.presentmon,
        process_id=process_id,
        run_seconds=run_seconds,
        run_dir=run_dir,
        run_index=run_index,
    )
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise RuntimeError("PresentMon CSV has no header")
        gpu, cpu, displayed = extract_samples(reader, process_id=process_id)

    if not gpu:
        raise RuntimeError("PresentMon produced no GPUTime/MsGPUTime samples")
    if not cpu:
        raise RuntimeError("PresentMon produced no CPUBusy/MsCPUBusy samples")
    if not displayed:
        raise RuntimeError(
            "PresentMon produced no MsBetweenDisplayChange/DisplayedTime samples"
        )

    _write_samples(gpu_path, gpu)
    _write_samples(cpu_path, cpu)
    _write_samples(displayed_path, displayed)

    summary = {
        "status": "collected",
        "process_id": process_id,
        "gpu_samples": len(gpu),
        "cpu_samples": len(cpu),
        "displayed_samples": len(displayed),
        "gpu_source": list(_GPU_FIELDS),
        "cpu_source": {"busy": list(_CPU_BUSY_FIELDS), "present_api": list(_PRESENT_API_FIELDS), "formula": "cpu_busy_ms + present_api_ms"},
        "displayed_source": list(_DISPLAY_FIELDS),
        "raw_csv": str(csv_path),
    }
    (run_dir / "collector-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
