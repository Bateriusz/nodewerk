#!/usr/bin/env python3
from __future__ import annotations
import argparse, fnmatch, json, subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SCOPES = ("core", "nw01", "nw07", "nw08", "nw08a", "nw10")
DEFAULT_MAP = Path(__file__).resolve().parents[2] / "ci" / "impact-map.v1.json"

class ImpactMapError(ValueError):
    pass

@dataclass(frozen=True)
class Classification:
    changed_paths: tuple[str, ...]
    direct_scopes: tuple[str, ...]
    scopes: tuple[str, ...]
    docs_only: bool
    full_regression: bool
    reasons: dict[str, str]
    fail_safe: bool = False
    error: str | None = None

    def outputs(self) -> dict[str, str]:
        values = {scope: ("true" if scope in self.scopes else "false") for scope in SCOPES}
        values.update({
            "docs_only": "true" if self.docs_only else "false",
            "full_regression": "true" if self.full_regression else "false",
            "fail_safe": "true" if self.fail_safe else "false",
        })
        for scope in SCOPES:
            values[f"{scope}_reason"] = self.reasons.get(scope, "not impacted")
        return values

def load_map(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ImpactMapError(f"cannot load impact map {path}: {exc}") from exc
    if data.get("version") != 1:
        raise ImpactMapError("impact map version must be 1")
    scopes = data.get("scopes")
    if not isinstance(scopes, dict) or set(scopes) != set(SCOPES):
        raise ImpactMapError(f"impact map scopes must be exactly {', '.join(SCOPES)}")
    for name, spec in scopes.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("patterns"), list):
            raise ImpactMapError(f"scope {name} must define a patterns list")
        unknown = set(spec.get("propagates_to", [])) - set(SCOPES)
        if unknown:
            raise ImpactMapError(f"scope {name} propagates to unknown scopes: {sorted(unknown)}")
    for key in ("docs_patterns", "ci_infrastructure_patterns"):
        if not isinstance(data.get(key), list):
            raise ImpactMapError(f"{key} must be a list")
    return data

def normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized

def matches(path: str, patterns: Iterable[str]) -> bool:
    normalized = normalize_path(path)
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns)

def all_scopes(reason: str, changed: tuple[str, ...], *, fail_safe: bool = False, error: str | None = None) -> Classification:
    return Classification(changed, SCOPES, SCOPES, False, True, {scope: reason for scope in SCOPES}, fail_safe, error)

def classify_paths(paths: Iterable[str], mapping: dict) -> Classification:
    changed = tuple(sorted({normalize_path(str(path)) for path in paths if str(path).strip()}))
    if not changed:
        return all_scopes("empty/unknown diff; conservative full regression", changed, fail_safe=True)
    if all(matches(path, mapping["docs_patterns"]) for path in changed):
        return Classification(changed, (), (), True, False, {scope: "docs-only change" for scope in SCOPES})
    if any(matches(path, mapping["ci_infrastructure_patterns"]) for path in changed):
        return all_scopes("CI/routing infrastructure changed", changed)

    direct: set[str] = set()
    matched: set[str] = set()
    reason_parts = {scope: [] for scope in SCOPES}
    for path in changed:
        if matches(path, mapping["docs_patterns"]):
            matched.add(path)
            continue
        path_scopes = [scope for scope in SCOPES if matches(path, mapping["scopes"][scope]["patterns"])]
        if path_scopes:
            matched.add(path)
            for scope in path_scopes:
                direct.add(scope)
                reason_parts[scope].append(f"direct:{path}")
    unknown = [path for path in changed if path not in matched]
    if unknown:
        return all_scopes("unclassified path(s): " + ", ".join(unknown[:5]), changed, fail_safe=True)

    impacted = set(direct)
    queue = list(direct)
    while queue:
        source = queue.pop(0)
        for downstream in mapping["scopes"][source].get("propagates_to", []):
            if downstream not in impacted:
                impacted.add(downstream)
                reason_parts[downstream].append(f"downstream-of:{source}")
                queue.append(downstream)
    reasons = {
        scope: ("; ".join(reason_parts[scope]) if reason_parts[scope] else "not impacted")
        for scope in SCOPES
    }
    return Classification(changed, tuple(sorted(direct)), tuple(scope for scope in SCOPES if scope in impacted), False, False, reasons)

def classify(event_name: str, paths: Iterable[str], mapping_path: Path) -> Classification:
    if event_name != "pull_request":
        return all_scopes(f"{event_name} uses full regression", tuple(sorted(paths)))
    try:
        return classify_paths(paths, load_map(mapping_path))
    except Exception as exc:
        return all_scopes(
            "impact classifier/map failure; conservative full regression",
            tuple(sorted(paths)),
            fail_safe=True,
            error=f"{type(exc).__name__}: {exc}",
        )

def git_changed_paths(base: str, head: str) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRDTUXB", base, head],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"git diff failed with {proc.returncode}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

def write_outputs(path: Path, classification: Classification) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, value in classification.outputs().items():
            handle.write(f"{key}={value.replace(chr(10), ' ').replace(chr(13), ' ')}\n")

def write_summary(path: Path, classification: Classification) -> None:
    lines = [
        "## CI impact routing", "",
        f"- full_regression: `{str(classification.full_regression).lower()}`",
        f"- docs_only: `{str(classification.docs_only).lower()}`",
        f"- fail_safe: `{str(classification.fail_safe).lower()}`",
        f"- changed paths: `{len(classification.changed_paths)}`",
    ]
    if classification.error:
        lines.append(f"- classifier error: `{classification.error}`")
    lines += ["", "| Scope | Run | Reason |", "|---|---:|---|"]
    for scope in SCOPES:
        lines.append(f"| {scope} | {'yes' if scope in classification.scopes else 'no'} | {classification.reasons.get(scope, 'not impacted')} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--changed-path-file")
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--summary-file", type=Path)
    args = parser.parse_args(argv)
    paths: list[str] = []
    try:
        if args.event_name == "pull_request":
            if args.changed_path_file:
                paths = [line.strip() for line in Path(args.changed_path_file).read_text(encoding="utf-8").splitlines() if line.strip()]
            elif args.base and args.head:
                paths = git_changed_paths(args.base, args.head)
            else:
                raise RuntimeError("pull_request classification requires --base/--head or --changed-path-file")
        result = classify(args.event_name, paths, args.map)
    except Exception as exc:
        result = all_scopes(
            "classifier execution failure; conservative full regression",
            tuple(paths), fail_safe=True, error=f"{type(exc).__name__}: {exc}",
        )
    if args.github_output:
        write_outputs(args.github_output, result)
    if args.summary_file:
        write_summary(args.summary_file, result)
    print(json.dumps({
        "changed_paths": list(result.changed_paths),
        "direct_scopes": list(result.direct_scopes),
        "scopes": list(result.scopes),
        "docs_only": result.docs_only,
        "full_regression": result.full_regression,
        "fail_safe": result.fail_safe,
        "error": result.error,
        "reasons": result.reasons,
    }, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
