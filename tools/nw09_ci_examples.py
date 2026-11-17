#!/usr/bin/env python3
"""Build and stage the two runnable NW-09 CI examples.

NW-09A intentionally reuses the production NW-05/NW-09 generated-player
export path.  The resulting executables embed their .nwp package and link
metadata, so the downloadable artifact needs no source checkout, DXC, or
developer SDK at runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Iterable

import nw05_export
import nwp_package as nwp


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BYTE_CEILING = 64 * 1024 * 1024
DEFAULT_MEMORY_BUDGET = 512 * 1024 * 1024
SMOKE_TICK = 3200
SMOKE_SIMULATION_TICK = SMOKE_TICK // 400

EXAMPLES = (
    (
        "particles",
        REPO_ROOT / "fixtures" / "nw09" / "particles_seeded.nwgraph",
        "nw09-particles.exe",
    ),
    (
        "feedback",
        REPO_ROOT / "fixtures" / "nw09" / "feedback_rect.nwgraph",
        "nw09-feedback.exe",
    ),
)


class ExampleBuildError(RuntimeError):
    """Raised when an NW-09A artifact cannot be built or validated."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_player_metrics(stdout: str) -> dict[str, str]:
    metrics: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.startswith("NW05_"):
            metrics[key] = value
    return metrics


def _validate_smoke_stdout(stdout: str, *, example: str) -> None:
    metrics = _parse_player_metrics(stdout)
    expected = {
        "NW05_LINK_MANIFEST_VERIFIED": "1",
        "NW05_EXPLICIT_TICK": str(SMOKE_TICK),
        "NW05_HISTORY_SIMULATION_TICK": str(SMOKE_SIMULATION_TICK),
        "NW05_HISTORY_PENDING_TICKS": "0",
    }
    missing = [
        f"{key}={value}"
        for key, value in expected.items()
        if metrics.get(key) != value
    ]
    if missing:
        raise ExampleBuildError(
            f"{example}: generated player did not report the required "
            "NW-09 smoke-test markers: " + ", ".join(missing)
        )


def _smoke_launch(executable: Path, *, example: str) -> None:
    completed = subprocess.run(
        [
            str(executable),
            "--tick",
            str(SMOKE_TICK),
            "--frames",
            "1",
            "--allow-software-adapter",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=300,
    )
    if completed.returncode != 0:
        raise ExampleBuildError(
            f"{example}: generated player exited with "
            f"{completed.returncode}: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    _validate_smoke_stdout(completed.stdout, example=example)


def _write_sha256_manifest(root: Path, files: Iterable[Path]) -> Path:
    rows = []
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        rows.append(f"{_sha256(path)}  {relative}")
    manifest = root / "SHA256SUMS.txt"
    manifest.write_text("\n".join(rows) + "\n", encoding="ascii", newline="\n")
    return manifest


def _verify_sha256_manifest(root: Path, manifest: Path) -> None:
    for raw in manifest.read_text(encoding="ascii").splitlines():
        if not raw.strip():
            continue
        digest, relative = raw.split("  ", 1)
        path = root / Path(relative)
        if not path.is_file():
            raise ExampleBuildError(
                f"staged artifact listed in SHA256SUMS.txt is missing: {relative}"
            )
        actual = _sha256(path)
        if actual != digest:
            raise ExampleBuildError(
                f"staged artifact hash changed after publication staging: {relative}"
            )


def build_examples(
    *,
    player_template: Path,
    output_directory: Path,
    dxc_executable: Path,
) -> None:
    if os.name != "nt":
        raise ExampleBuildError("NW-09A runnable examples require Windows")

    player_template = player_template.resolve()
    dxc_executable = dxc_executable.resolve()
    output_directory = output_directory.resolve()

    if not player_template.is_file():
        raise ExampleBuildError(
            f"generated-player template is missing: {player_template}"
        )
    if not dxc_executable.is_file():
        raise ExampleBuildError(f"DXC executable is missing: {dxc_executable}")

    for name, graph, _ in EXAMPLES:
        if not graph.is_file():
            raise ExampleBuildError(f"{name}: canonical graph is missing: {graph}")

    with tempfile.TemporaryDirectory(prefix="nodewerk-nw09a-") as temporary:
        temporary_root = Path(temporary)
        staged_root = temporary_root / "nw09-examples"
        staged_root.mkdir(parents=True)
        staged_executables: list[Path] = []

        for name, graph, filename in EXAMPLES:
            export_root = temporary_root / f"export-{name}"
            build_root = temporary_root / f"build-{name}"
            settings = nw05_export.ExportSettings(
                byte_ceiling=DEFAULT_BYTE_CEILING,
                memory_budget=DEFAULT_MEMORY_BUDGET,
                player_template=str(player_template),
                specialized_build_root=str(build_root),
            )
            result = nw05_export.export_graph(
                graph,
                export_root,
                settings=settings,
                dxc_executable=str(dxc_executable),
            )
            artifact = result.artifacts[nwp.COMMON_PROFILE_ID]
            if artifact.executable_bytes is None or not artifact.executable_name:
                raise ExampleBuildError(
                    f"{name}: production export did not produce a generated player"
                )

            exported = export_root / artifact.executable_name
            if not exported.is_file() or exported.stat().st_size == 0:
                raise ExampleBuildError(
                    f"{name}: generated player is missing or empty: {exported}"
                )

            example_root = staged_root / name
            example_root.mkdir()
            staged = example_root / filename
            shutil.copyfile(exported, staged)
            if staged.stat().st_size == 0:
                raise ExampleBuildError(f"{name}: staged executable is empty")

            # Smoke-test the exact bytes that will be uploaded.
            _smoke_launch(staged, example=name)
            staged_executables.append(staged)

        manifest = _write_sha256_manifest(staged_root, staged_executables)
        _verify_sha256_manifest(staged_root, manifest)

        # Do not expose a partial artifact directory.  Publication occurs only
        # after both exports and both smoke tests have succeeded.
        if output_directory.exists():
            shutil.rmtree(output_directory)
        shutil.copytree(staged_root, output_directory)
        _verify_sha256_manifest(
            output_directory, output_directory / "SHA256SUMS.txt"
        )

    for _, _, filename in EXAMPLES:
        matches = list(output_directory.rglob(filename))
        if len(matches) != 1 or matches[0].stat().st_size == 0:
            raise ExampleBuildError(
                f"final artifact staging did not contain exactly one {filename}"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build, smoke-test, and stage runnable NW-09 examples"
    )
    parser.add_argument("--player-template", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dxc", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        build_examples(
            player_template=args.player_template,
            output_directory=args.output_dir,
            dxc_executable=args.dxc,
        )
    except (ExampleBuildError, nw05_export.ExportError) as exc:
        print(f"NW-09A example build failed: {exc}")
        return 1

    print(f"NW-09A examples staged at {args.output_dir.resolve()}")
    for name, _, filename in EXAMPLES:
        path = args.output_dir / name / filename
        print(f"{name}: {path} sha256={_sha256(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
