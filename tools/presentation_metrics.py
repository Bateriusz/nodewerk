from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable, Mapping, Sequence


_GPU_FIELDS = ("GPUTime", "MsGPUTime")
_CPU_BUSY_FIELDS = ("CPUBusy", "MsCPUBusy")
_PRESENT_API_FIELDS = ("MsInPresentAPI", "InPresentAPI")
_DISPLAY_FIELDS = ("MsBetweenDisplayChange", "DisplayedTime", "MsDisplayedTime")


PRESENTMON_260_SHA256 = "b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af"
PRESENTMON_CSV = "presentmon.csv"
PRESENTMON_METADATA = "presentmon-metadata.json"
# PresentMon v2.6.0 CsvOutput.cpp, legacy and v2 column modes respectively.
# CPUStartQPC is in raw ticks; the relative, date-time and TimeInSeconds
# columns are never substituted for the absolute QPC-millisecond timestamp.
_QPC_MS_SCHEMA = {"CPUStartQPCTimeInMs": "legacy_columns", "CPUStartQPCTime": "v2_columns"}
_OTHER_CPU_CLOCKS = {"CPUStartQPC", "CPUStartDateTime", "CPUStartTime",
                     "CPUStartTimeInMs", "CPUStartTimeInSeconds"}


class PresentMonError(RuntimeError):
    def __init__(self, code: str, exit_code: int | None = None):
        self.code, self.exit_code = code, exit_code
        super().__init__(code)


def presentmon_unavailable(code: str, exit_code: int | None = None) -> dict:
    result = {"status": "unavailable", "reason": code}
    if type(exit_code) is int:
        result["collector_exit_code"] = exit_code
    if code == "etw_access_denied":
        result["action"] = ("Timing is optional. If needed, rerun from a Windows PowerShell window "
                            "you explicitly open as administrator, or use an account that already "
                            "has Performance Log Users access.")
    elif code == "collector_start_denied":
        result["action"] = "Check whether Windows allows the verified PresentMon executable to run."
    elif code == "collector_failed":
        result["action"] = "Inspect the private PresentMon logs for the collector failure."
    elif code == "etw_schema_or_clock_unavailable":
        result["action"] = "Use the pinned PresentMon tool with absolute QPC milliseconds."
    return result


def collector_failure(exit_code: int, stderr: str) -> str:
    # v2.6.0 also returns 6 for other session-start failures. The specific
    # collector diagnostic, not a token membership guess or code alone, is
    # the evidence for ETW permission denial.
    if exit_code == 6 and re.search(
        r"(?mi)^\s*(?:error:\s*)?failed to start trace session:\s*access denied[.!]?\s*$", stderr
    ):
        return "etw_access_denied"
    return "collector_failed"


def _portable_path(value: str) -> str:
    return value.replace("\\", "/").casefold()


def presentmon_binding(metadata: Mapping, *, process_id: int, expected_tool_sha256: str) -> str:
    if (metadata.get("format") != "nw07-presentmon-collector"
            or type(metadata.get("version")) is not int or metadata["version"] != 1
            or type(metadata.get("process_id")) is not int or metadata["process_id"] != process_id
            or expected_tool_sha256 != PRESENTMON_260_SHA256
            or metadata.get("presentmon_sha256") != expected_tool_sha256
            or type(metadata.get("exit_code")) is not int or metadata["exit_code"] != 0
            or metadata.get("clock", "absolute_qpc_ms") != "absolute_qpc_ms"):
        raise PresentMonError("etw_collector_identity_mismatch")
    command = metadata.get("command")
    exe = metadata.get("presentmon_path")
    if (not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command)
            or not isinstance(exe, str) or _portable_path(exe) != _portable_path(command[0])
            or command.count("--process_id") != 1 or command.count("--output_file") != 1
            or command.count("--qpc_time_ms") != 1 or command.count("--terminate_on_proc_exit") != 1
            or command.count("--v2_metrics") > 1
            or any(flag in command for flag in ("--qpc_time", "--date_time", "--v1_metrics", "--etl_file"))):
        raise PresentMonError("etw_collector_identity_mismatch")
    pid_arg = command[command.index("--process_id") + 1:][:1]
    output_arg = command[command.index("--output_file") + 1:][:1]
    if pid_arg != [str(process_id)] or not output_arg or _portable_path(output_arg[0]).split("/")[-1] != PRESENTMON_CSV:
        raise PresentMonError("etw_collector_identity_mismatch")
    return "CPUStartQPCTime" if "--v2_metrics" in command else "CPUStartQPCTimeInMs"


def bound_presentmon_samples(
    payload: bytes, metadata: Mapping, *, process_id: int, expected_tool_sha256: str,
    expected_csv_sha256: str | None, start_qpc_ms: float, end_qpc_ms: float, sync_interval: int,
    application: str | None = None,
) -> dict:
    """Decode only a proven collector's exact PID, clock window and swap chain."""
    if type(process_id) is not int or process_id <= 0:
        raise PresentMonError("etw_process_binding_mismatch")
    clock = presentmon_binding(metadata, process_id=process_id, expected_tool_sha256=expected_tool_sha256)
    csv_hash = hashlib.sha256(payload).hexdigest()
    recorded_hash = metadata.get("csv_sha256")
    if ((recorded_hash is None and expected_csv_sha256 is None)
            or (recorded_hash is not None and recorded_hash != csv_hash)
            or (expected_csv_sha256 is not None and expected_csv_sha256 != csv_hash)):
        raise PresentMonError("etw_csv_identity_mismatch")
    if (any(type(value) not in (int, float) or not 0 < value < math.inf
            for value in (start_qpc_ms, end_qpc_ms)) or start_qpc_ms >= end_qpc_ms
            or type(sync_interval) is not int or not 0 <= sync_interval <= 4):
        raise PresentMonError("etw_process_window_unavailable")
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""), strict=True)
        header = reader.fieldnames or []
        fields = set(header)
        clocks = fields.intersection(_QPC_MS_SCHEMA)
        is_v2 = clock == "CPUStartQPCTime"
        # The pinned v2 column mode emits CPUBusy, but NOT Present API time.
        # That component must not appear in an explicitly v2 CSV or be
        # reconstructed from a different timing metric.
        required = {"FrameTime", "CPUBusy"} if is_v2 else {"MsCPUBusy", "MsInPresentAPI"}
        incompatible = ({"TimeInSeconds", "MsCPUBusy", "MsGPUTime", "MsBetweenDisplayChange", *_PRESENT_API_FIELDS}
                        if is_v2 else {"FrameTime", "CPUBusy", "GPUTime", "DisplayedTime"})
        if (len(header) != len(fields) or clocks != {clock} or fields.intersection(_OTHER_CPU_CLOCKS | incompatible)
                or not ({"ProcessID", "SyncInterval", "SwapChainAddress"} | required) <= fields
                or any(len(fields.intersection(aliases)) > 1
                       for aliases in (_GPU_FIELDS, _CPU_BUSY_FIELDS, _PRESENT_API_FIELDS, _DISPLAY_FIELDS))):
            raise PresentMonError("etw_schema_or_clock_unavailable")
        filtered = []
        chains = set()
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise PresentMonError("invalid_etw_samples")
            if row["ProcessID"] != str(process_id):
                continue
            stamp = float(row[clock])
            if not math.isfinite(stamp) or stamp <= 0:
                raise PresentMonError("invalid_etw_samples")
            if not start_qpc_ms <= stamp <= end_qpc_ms:
                continue
            if application is not None and "Application" in fields and row["Application"].casefold() != application.casefold():
                raise PresentMonError("etw_process_binding_mismatch")
            address = row["SwapChainAddress"]
            if (row["SyncInterval"] != str(sync_interval)
                    or not re.fullmatch(r"0[xX][0-9a-fA-F]{1,16}", address) or int(address, 16) == 0):
                raise PresentMonError("unmatched_sync_interval_or_swapchain")
            chains.add(int(address, 16))
            filtered.append(row)
        if not filtered:
            raise PresentMonError("etw_process_window_unavailable")
        if len(chains) != 1:
            raise PresentMonError("unmatched_sync_interval_or_swapchain")
        gpu, cpu, displayed = extract_samples(filtered, process_id=process_id)
    except (UnicodeError, ValueError, csv.Error) as exc:
        raise PresentMonError("invalid_etw_samples") from exc
    return {"gpu_time": gpu, "cpu_submit": cpu, "display_intervals": displayed,
            "cpu_submit_reported": not is_v2, "bound_rows": len(filtered), "clock_schema": _QPC_MS_SCHEMA[clock], "csv_sha256": csv_hash}


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
            submit = cpu_busy + present_api
            if not math.isfinite(submit):
                raise ValueError("nonfinite combined PresentMon CPU time")
            cpu.append(submit)
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
    csv_path = run_dir / PRESENTMON_CSV
    metadata_path = run_dir / PRESENTMON_METADATA
    stdout_path, stderr_path = (run_dir / f"presentmon-{name}.bin" for name in ("stdout", "stderr"))
    if any(path.exists() for path in (csv_path, metadata_path, stdout_path, stderr_path)):
        raise PresentMonError("collector_output_already_exists")
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
    tool_hash = _sha256_file(presentmon)
    exit_code, code = None, None
    stdout, stderr = b"", b""
    try:
        completed = subprocess.run(command, capture_output=True, check=False, timeout=run_seconds + 20.0)
        exit_code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        if exit_code != 0:
            code = collector_failure(exit_code, stderr.decode("utf-8", errors="replace"))
        elif not csv_path.is_file() or csv_path.stat().st_size == 0:
            code = "collector_no_csv"
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = exc.stdout or b"", exc.stderr or b""
        code = "collector_timeout"
    except OSError as exc:
        stderr = str(exc).encode("utf-8", errors="replace")
        code = "collector_start_denied" if isinstance(exc, PermissionError) else "collector_launch_failed"
    with stdout_path.open("xb") as stream:
        stream.write(stdout)
    with stderr_path.open("xb") as stream:
        stream.write(stderr)
    metadata = {
        "format": "nw07-presentmon-collector",
        "version": 1,
        "command": command,
        "process_id": process_id,
        "run_seconds": run_seconds,
        "clock": "absolute_qpc_ms",
        "presentmon_path": str(presentmon.resolve()),
        "presentmon_sha256": tool_hash,
        "csv_sha256": _sha256_file(csv_path) if csv_path.is_file() else None,
        "exit_code": exit_code,
        "failure_code": code,
        "stdout": stdout.decode("utf-8", errors="replace")[-8000:],
        "stderr": stderr.decode("utf-8", errors="replace")[-8000:],
    }
    with metadata_path.open("x", encoding="utf-8") as stream:
        json.dump(metadata, stream, indent=2, sort_keys=True)
        stream.write("\n")
    if code:
        raise PresentMonError(code, exit_code)
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
