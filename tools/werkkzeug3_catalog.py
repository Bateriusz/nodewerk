#!/usr/bin/env python3
"""Extract desktop Werkkzeug3 editor registries as reference metadata.

The input is the existing fr_public submodule. No Farbrausch implementation is
translated, executed or copied into the result. The parser reads three explicit
registration table families and records direct editor-control declarations separately
from the unreviewed semantic mapping. It does not parse production graph files.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re


SOURCE_REPOSITORY = "https://github.com/farbrausch/fr_public"
SOURCE_REVISION = "8c8f82c2ae01133370e649bbc05916e7aceecadd"
# Git blob identities from the pinned recursive source tree. Checking the files
# also supports a partial read-only source materialization without a .git folder.
SOURCE_FILES = {
    "werkkzeug3/kdoc.hpp": "07d9156191e388c797a6fefa4ecada16ba38484d",
    "werkkzeug3/novaplayer.cpp": "981e0754763c8d629b976b8e9995b5cd0f83f89f",
    "werkkzeug3/novaplayer.hpp": "6934dd8200bd53e4e47df8f5492c975745c061b1",
    "werkkzeug3/werkkzeug.hpp": "fc36b05cfc8e564321bf4d796c57158c59d5b246",
    "werkkzeug3/werkops.cpp": "c9d9abc274f37ac183531c1ce810b7a914192df2",
    "werkkzeug3/winpara.cpp": "ca779b75c0e3413f2a1ff7c00975f7f7636396fd",
    "werkkzeug3_kkrieger/kdoc.hpp": "05af1f4f914843422727caa3bc6fe2929b379f57",
    "werkkzeug3_kkrieger/novaplayer.cpp": "20eb83373726c486347bee2c0c7f69832a27f860",
    "werkkzeug3_kkrieger/novaplayer.hpp": "6451e5b2a75a0f16913b7aadd6a7916f387e7738",
    "werkkzeug3_kkrieger/werkkzeug.hpp": "d55fc574e655b1e302b830da77ea88f1a16a53ff",
    "werkkzeug3_kkrieger/werkops.cpp": "06969a5fd1ec4cba633a05c79c7c3540ece10b7f",
    "werkkzeug3_kkrieger/winpara.cpp": "b3f341bda585013c212464719960786be3524f35",
}

_QUOTED = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
_LEXICAL = re.compile(_QUOTED + r"|//[^\n]*|/\*[\s\S]*?\*/")
_STRING = re.compile(r'"(?:\\.|[^"\\])*"')


def _comments_to_spaces(text: str) -> str:
    """Keep offsets/line numbers and string contents while excluding comments."""
    def replace(match: re.Match[str]) -> str:
        token = match.group()
        if token.startswith(("//", "/*")):
            return "".join("\n" if char == "\n" else " " for char in token)
        return token

    return _LEXICAL.sub(replace, text)


def _without_literals(text: str) -> str:
    return re.sub(_QUOTED, lambda m: " " * len(m.group()), text)


def _closing(text: str, start: int) -> int:
    pairs = {"{": "}", "(": ")", "[": "]"}
    if text[start] not in pairs:
        raise ValueError("expected opening delimiter")
    masked = _without_literals(text)
    stack: list[str] = []
    for index in range(start, len(masked)):
        char = masked[index]
        if char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values():
            if not stack or stack.pop() != char:
                raise ValueError("unbalanced declaration delimiters")
            if not stack:
                return index
    raise ValueError("unterminated declaration")


def _fields(text: str) -> list[str]:
    masked = _without_literals(text)
    depth = 0
    begin = 0
    result: list[str] = []
    for index, char in enumerate(masked):
        if char in "{([":
            depth += 1
        elif char in "})]":
            depth -= 1
        elif char == "," and depth == 0:
            result.append(text[begin:index].strip())
            begin = index + 1
    tail = text[begin:].strip()
    if tail:
        result.append(tail)
    return result


def _literal(text: str) -> str | None:
    literals = list(_STRING.finditer(text))
    if not literals or _STRING.sub("", text).strip():
        return None
    return "".join(ast.literal_eval(match.group()) for match in literals)


def _table_rows(text: str, type_name: str, table_name: str):
    pattern = rf"\b{type_name}\s+{table_name}\s*\[\s*\]\s*=\s*\{{"
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise ValueError(f"expected one {type_name} {table_name} table")
    start = matches[0].end() - 1
    end = _closing(text, start)
    cursor = start + 1
    sentinel = False
    while cursor < end:
        if text[cursor].isspace() or text[cursor] == ",":
            cursor += 1
            continue
        if text[cursor] != "{" or sentinel:
            raise ValueError(f"unexpected text in {table_name} at offset {cursor}")
        row_end = _closing(text, cursor)
        fields = _fields(text[cursor + 1:row_end])
        if fields == ["0"]:
            sentinel = True
        else:
            yield fields, text.count("\n", 0, cursor) + 1
        cursor = row_end + 1
    if not sentinel:
        raise ValueError(f"missing terminating row in {table_name}")


def _type_array(field: str) -> list[str]:
    if not field.startswith("{") or not field.endswith("}"):
        raise ValueError("expected type declaration array")
    return [value for value in _fields(field[1:-1]) if value != "0"]


def _editor_metadata(text: str) -> dict[str, dict]:
    """Record direct UI declarations without interpreting C++ control flow."""
    functions: dict[str, dict] = {}
    pattern = r"\bvoid\s+(Edit_\w+|KE_\w+_OnPara)\s*\([^{};]*\)\s*\{"
    for match in re.finditer(pattern, text):
        name = match.group(1)
        begin = match.end() - 1
        end = _closing(text, begin)
        body = text[begin + 1:end]
        calls = []
        for call in re.finditer(r"(?:ParaWin|win)\s*->\s*((?:Edit|Add)\w+)\s*\(", body):
            method = call.group(1)
            if method in {"EditOp", "EditGroup", "EditSetGray", "AddGroup"}:
                continue
            call_begin = call.end() - 1
            call_end = _closing(body, call_begin)
            args = _fields(body[call_begin + 1:call_end])
            label = _literal(args[0]) if args else None
            item = {
                "method": method,
                "label": label,
                "argument_expressions": args[1:] if label is not None else args,
                "source_line": text.count("\n", 0, begin + 1 + call.start()) + 1,
            }
            calls.append(item)
        delegates = sorted(set(re.findall(r"\b(Edit_\w+|KE_\w+_OnPara)\s*\(", body)))
        functions[name] = {
            "extraction_status": "partial_static",
            "handler_line": text.count("\n", 0, match.start()) + 1,
            "direct_editor_calls": calls,
            "delegated_editor_handlers": delegates,
        }
    return functions


def extract_registry(text: str, variant: str, registry: str, constants: dict[str, int]) -> list[dict]:
    """Extract one known registry; malformed/duplicate entries fail explicitly."""
    text = _comments_to_spaces(text)
    metadata = _editor_metadata(text)
    table_type = "WerkClass" if registry == "WerkClasses" else "KEffect2"
    filename = "werkops.cpp" if registry == "WerkClasses" else "novaplayer.cpp"
    path = f"{variant}/{filename}"
    result = []
    seen = set()
    for fields, line in _table_rows(text, table_type, registry):
        expected = {13, 14} if registry == "WerkClasses" else {12}
        if len(fields) not in expected:
            raise ValueError(f"{path}:{line}: unexpected field count {len(fields)}")
        name = _literal(fields[0])
        if name is None:
            raise ValueError(f"{path}:{line}: nonliteral operator name")
        legacy_id = int(fields[1 if registry == "WerkClasses" else 2], 0)
        key = f"{variant}:{registry}:0x{legacy_id:04x}"
        if key in seen:
            raise ValueError(f"duplicate operator key: {key}")
        seen.add(key)
        item = {
            "key": key,
            "variant": variant,
            "registry": registry,
            "legacy_id": f"0x{legacy_id:04x}",
            "name": name,
            "source": {
                "path": path,
                "line": line,
                "url": f"{SOURCE_REPOSITORY}/blob/{SOURCE_REVISION}/{path}#L{line}",
            },
            "review_status": "unreviewed",
        }
        if registry == "WerkClasses":
            def count(value: str) -> int:
                return constants[value] if value in constants else int(value, 0)

            handler = fields[9]
            item.update({
                "family": fields[2],
                "ports": {
                    "output_type": fields[2],
                    "minimum_inputs": count(fields[3]),
                    "maximum_inputs": count(fields[4]),
                    "input_type_declarations": _type_array(fields[12]),
                    "link_type_declarations": _type_array(fields[13]) if len(fields) == 14 else [],
                },
                "calling_convention": fields[5],
                "palette_column": fields[6],
                "packing": _literal(fields[8]),
                "handlers": {"editor": handler, "initialize": fields[10], "execute": fields[11]},
            })
            if item["packing"] is None:
                raise ValueError(f"{path}:{line}: nonliteral packing declaration")
        else:
            handler = fields[10]
            item.update({
                "family": "nova_effect",
                "ports": {
                    "output_type": None,
                    "link_type_declarations": _type_array(fields[11]),
                    "note": "Timeline effect registry, not a WerkClasses value-producing graph operator.",
                },
                "handlers": dict(zip(("initialize", "exit", "paint", "set", "editor"), fields[6:11])),
                "parameter_size_expression": fields[4],
                "edit_size_expression": fields[5],
            })
        item["parameters"] = metadata.get(handler, {
            "extraction_status": "unextracted",
            "reason": "Editor handler has no local definition in this source file.",
        })
        result.append(item)
    return result


def extract_animation_registry(text: str, variant: str, constants: dict[str, int]) -> list[dict]:
    """Inventory expression opcodes separately from scene/resource operators."""
    text = _comments_to_spaces(text)
    path = f"{variant}/winpara.cpp"
    result = []
    seen = set()
    for fields, line in _table_rows(text, "WerkOpAnimInfo", "AnimOpCmds"):
        if len(fields) != 5 or _literal(fields[0]) is None:
            raise ValueError(f"{path}:{line}: unexpected animation registration")
        legacy_id = constants[fields[1]]
        key = f"{variant}:AnimOpCmds:0x{legacy_id:04x}"
        if key in seen:
            raise ValueError(f"duplicate operator key: {key}")
        seen.add(key)
        result.append({
            "key": key,
            "variant": variant,
            "registry": "AnimOpCmds",
            "legacy_id": f"0x{legacy_id:04x}",
            "code_symbol": fields[1],
            "name": _literal(fields[0]),
            "family": "animation_expression",
            "ports": {
                "declared_input_count": int(fields[2], 0),
                "output_type": None,
                "note": "Animation bytecode declaration; value/store behavior is not inferred as a typed scene port.",
            },
            "flags": fields[3],
            "handlers": {},
            "parameters": {
                "extraction_status": "registry_metadata_only",
                "payload_kind": fields[4],
                "note": "Payload selector from the registry; its editor controls are not expanded.",
            },
            "source": {
                "path": path,
                "line": line,
                "url": f"{SOURCE_REPOSITORY}/blob/{SOURCE_REVISION}/{path}#L{line}",
            },
            "review_status": "unreviewed",
        })
    return result


def build_catalog(reference_root: Path) -> dict:
    texts = {}
    source_files = []
    for relative, expected in sorted(SOURCE_FILES.items()):
        data = (reference_root / relative).read_bytes()
        actual = hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()
        if actual != expected:
            raise ValueError(
                f"Source revision mismatch for {relative}: {actual} != {expected}. "
                f"Use fr_public at {SOURCE_REVISION}; update provenance before cataloging another revision."
            )
        # These historical files contain Latin-1 comments. ASCII declarations
        # retain exact character offsets, without lossy replacement decoding.
        texts[relative] = data.decode("latin1")
        source_files.append({"path": relative, "git_blob_sha": actual, "bytes": len(data)})
    operators = []
    for variant in ("werkkzeug3", "werkkzeug3_kkrieger"):
        constants = {
            name: int(value, 0)
            for name, value in re.findall(
                r"^#define\s+(KK_MAXINPUT|KK_MAXLINK|KA_\w+)\s+(0x[0-9a-fA-F]+|\d+)\b",
                texts[f"{variant}/kdoc.hpp"], re.MULTILINE,
            )
        }
        for registry, filename in (("WerkClasses", "werkops.cpp"), ("EffectList", "novaplayer.cpp")):
            operators.extend(extract_registry(texts[f"{variant}/{filename}"], variant, registry, constants))
        operators.extend(extract_animation_registry(texts[f"{variant}/winpara.cpp"], variant, constants))
    operators.sort(key=lambda item: (item["variant"], item["registry"], int(item["legacy_id"], 0)))
    return {
        "schema_version": 1,
        "source_repository": SOURCE_REPOSITORY,
        "source_revision": SOURCE_REVISION,
        "scope": {
            "included": [
                "Complete WerkClasses desktop editor tables in werkkzeug3 and werkkzeug3_kkrieger.",
                "Separate Nova EffectList timeline-effect tables in both variants.",
                "Separate AnimOpCmds animation-expression opcode tables in both variants.",
            ],
            "excluded": [
                "Player/export KHandlers tables are deployment subsets, not extra authoring operators.",
                "w3texlib and wz_mobile derivatives are outside this desktop-registry catalog.",
                "Commented-out registrations and zero terminators are not active declarations.",
                "GenThree, RG2 and Werkkzeug4 use other registries and are tracked separately.",
            ],
        },
        "extraction_notes": [
            "Source blob hashes are verified against the pinned revision before extraction.",
            "Legacy IDs are unique only within a variant and registry; variants are not summed as unique capabilities.",
            "Input/link arrays are registration declarations; repeated/optional slot semantics are not inferred.",
            "Parameter metadata records direct editor calls and unevaluated argument expressions. Conditional UI, loops, delegates, blob editors and default assignments are not executed; this is not a complete parameter schema.",
            "Nova timeline effects have no value output type and are counted separately from graph operators.",
            "Animation-expression opcodes are separate records, including load/store variants; parameter/variable-specific menu expansions are not separate opcode registrations.",
            "Availability in a historical build, usage/reachability in production graphs, semantic equivalence and nodewerk implementation coverage remain unreviewed.",
        ],
        "source_files": source_files,
        "summary": {
            "registration_count": len(operators),
            "by_variant_and_registry": dict(sorted(Counter(f'{item["variant"]}:{item["registry"]}' for item in operators).items())),
            "by_variant_and_family": dict(sorted(Counter(f'{item["variant"]}:{item["family"]}' for item in operators).items())),
        },
        "operators": operators,
    }


def serialize_catalog(catalog: dict) -> str:
    """Keep provenance readable and each generated registration on one line."""
    metadata = {key: value for key, value in catalog.items() if key != "operators"}
    prefix = json.dumps(metadata, ensure_ascii=False, indent=2)
    rows = ",\n".join(
        "    " + json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        for item in catalog["operators"]
    )
    return prefix[:-2] + ',\n  "operators": [\n' + rows + "\n  ]\n}\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-root", type=Path, default=Path("references/farbrausch/fr_public"))
    parser.add_argument("--output", type=Path, default=Path("references/operator_catalog/werkkzeug3.json"))
    parser.add_argument("--check", action="store_true", help="Verify the checked-in catalog without rewriting it")
    args = parser.parse_args()
    payload = serialize_catalog(build_catalog(args.reference_root))
    if args.check:
        if args.output.read_text(encoding="utf-8") != payload:
            raise SystemExit("Werkkzeug3 catalog is out of date")
        print("Werkkzeug3 catalog matches the pinned sources")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
