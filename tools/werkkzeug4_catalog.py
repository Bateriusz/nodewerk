#!/usr/bin/env python3
"""Inventory Werkkzeug4 declarations without compiling or importing its code.

The pinned source manifest describes a reference dataset, not nodewerk's product
implementation. The parser reads top-level .ops declarations and parameter UI
metadata only. It never evaluates embedded C++, defaults, or GUI conditions.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

SOURCE_REPOSITORY = "https://github.com/farbrausch/fr_public"
SOURCE_REVISION = "8c8f82c2ae01133370e649bbc05916e7aceecadd"
# Git blob identities from the complete, non-truncated upstream tree at this pin.
EXPECTED_SOURCES = {
    "altona_wz4/altona/main/wz4lib/basic_ops.ops": "42d725b0ad0823ddb413031007a452b62069ba52",
    "altona_wz4/altona/main/wz4lib/poc_ops.ops": "7e729520699d9df48dcb0afbe069703d9bbab768",
    "altona_wz4/wz4/wz4frlib/adf_ops.ops": "95f8d717b181f47646c268ae6f9dcabc34f792ba",
    "altona_wz4/wz4/wz4frlib/chaos_font_ops.ops": "dc9ca6047622938329a18854cad6e5e52a16fe12",
    "altona_wz4/wz4/wz4frlib/chaosfx_ops.ops": "0f5cd269149c33292341ee71a9c9f85c3c2151f6",
    "altona_wz4/wz4/wz4frlib/chaosmesh_ops.ops": "f5288fba72f478810cd73a065591af0f3a2321c9",
    "altona_wz4/wz4/wz4frlib/easter_ops.ops": "1e4e51f820a79359757efee4520583cdf13af7be",
    "altona_wz4/wz4/wz4frlib/fr033_ops.ops": "2348feecfdfb664490436d8bb26572165fe54c95",
    "altona_wz4/wz4/wz4frlib/fr062_ops.ops": "7fc38e100030a9d226cffbf8d99e15bae2a1d58a",
    "altona_wz4/wz4/wz4frlib/fr063_chaos_ops.ops": "8afd84eb7708405052778a52bea3a2901358f980",
    "altona_wz4/wz4/wz4frlib/fr063_mandelbulb_ops.ops": "91905d9c8833153100a34d38c4d4ce5894ea77c0",
    "altona_wz4/wz4/wz4frlib/fr063_sph_ops.ops": "7be9f22791ad194630cd233ff88e79c90589475d",
    "altona_wz4/wz4/wz4frlib/fr063_tron_ops.ops": "09e445201bc2a42eb22b2e8a589d788d54621972",
    "altona_wz4/wz4/wz4frlib/fr067_ops.ops": "4b494908df552dc11205fb0de20e0ac180594d6e",
    "altona_wz4/wz4/wz4frlib/fxparticle_ops.ops": "b88f1add0e15c83bdb71cf5a83d05989cc6c4763",
    "altona_wz4/wz4/wz4frlib/kbfx_ops.ops": "e535b4184111a7ef5a8862663dbb0fcbbebaec1a",
    "altona_wz4/wz4/wz4frlib/pdf_ops.ops": "611659982c4707358d61728ba36b9f229bede7a0",
    "altona_wz4/wz4/wz4frlib/screens4_ops.ops": "33d27486407b5c65c2cf47e3d2d9a2c234c33076",
    "altona_wz4/wz4/wz4frlib/tron_ops.ops": "79761cc4b08dad8f1796a7694498fe3b5c7970f1",
    "altona_wz4/wz4/wz4frlib/wz3_bitmap_ops.ops": "76daab69e45a0bfbc950ee122a92012f04abf390",
    "altona_wz4/wz4/wz4frlib/wz4_anim_ops.ops": "de6b3a461d1ec83152588df24a387a167b71abad",
    "altona_wz4/wz4/wz4frlib/wz4_audio_ops.ops": "69fbd77a7ea5345d68d30e175312dbcaa9f50a40",
    "altona_wz4/wz4/wz4frlib/wz4_bsp_ops.ops": "3dadfcadd83171cfc897ba718527a609af41be64",
    "altona_wz4/wz4/wz4frlib/wz4_cubemap_ops.ops": "0e20c1aca9217c87e372779325ea65a74357f7ca",
    "altona_wz4/wz4/wz4frlib/wz4_demo2_ops.ops": "32abf54f6e3b8d3cc006c8e0c0d4e69a33e13c08",
    "altona_wz4/wz4/wz4frlib/wz4_ipp_ops.ops": "3e589a6f5ae957e38f73f26e6270ea0a79719407",
    "altona_wz4/wz4/wz4frlib/wz4_mesh_ops.ops": "62cf4fa3c741a38368a2fdf773fc94cfe3499528",
    "altona_wz4/wz4/wz4frlib/wz4_modmtrl_ops.ops": "7b7626cbbdba74bb2f85a1b8ed48f0f2768b429c",
    "altona_wz4/wz4/wz4frlib/wz4_modmtrlmod_ops.ops": "bd979da13f4dcd46b38bc81e51cb79691d86656f",
    "altona_wz4/wz4/wz4frlib/wz4_mtrl2_ops.ops": "ad32a390eb6ffff1f2d5df14476cbd23f74396e5",
    "altona_wz4/wz4/wz4frlib/wz4_mtrl_ops.ops": "15e84556e335bc3e201005423029720149013fb8",
    "altona_wz4/wz4/wz4frlib/wz4_rovemtrl_ops.ops": "58e44b32784963c4faaa06a269e1cedb1c82c1eb",
    "altona_wz4/wz4/wz4frlib/wz4_ssao_ops.ops": "67ee330d32188de19ffe05c173f5c2ed82fe7dd3"
}

_TOKEN = re.compile(
    r'(?P<space>\s+)|(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)'
    r'|(?P<string>"(?:\\[\s\S]|[^"\\])*"|\'(?:\\[\s\S]|[^\'\\])*\')'
    r'|(?P<name>[A-Za-z_][A-Za-z_0-9]*)|(?P<other>.)'
)
_VALUE_TYPES = {
    "float", "float2", "float30", "float31", "float4", "int", "bitmask",
    "char", "color", "flags", "radio", "strobe", "action", "string",
    "filein", "fileout", "link", "custom",
}


@dataclass(frozen=True)
class Token:
    value: str
    kind: str
    start: int
    end: int
    line: int


def tokens(source: str) -> list[Token]:
    result = []
    line = 1
    for match in _TOKEN.finditer(source):
        value = match.group()
        if match.lastgroup == "other" and (
                value in {"\"", "'"} or source.startswith("/*", match.start())):
            raise ValueError(f"Unterminated string or comment at line {line}")
        if match.lastgroup not in {"space", "comment"}:
            result.append(Token(value, match.lastgroup or "other",
                                match.start(), match.end(), line))
        line += value.count("\n")
    return result


def matching(items: list[Token], start: int) -> int:
    pairs = {"(": ")", "[": "]", "{": "}"}
    if items[start].value not in pairs:
        raise ValueError(f"Expected delimiter at line {items[start].line}")
    stack = []
    for index in range(start, len(items)):
        value = items[index].value
        if value in pairs:
            stack.append(pairs[value])
        elif value in pairs.values():
            if not stack or stack.pop() != value:
                raise ValueError(f"Mismatched delimiter at line {items[index].line}")
            if not stack:
                return index
    raise ValueError(f"Unclosed delimiter at line {items[start].line}")


def text_value(token: Token) -> str:
    try:
        value = ast.literal_eval(token.value)
        return value if isinstance(value, str) else token.value
    except (SyntaxError, ValueError):
        return token.value[1:-1]


def signature_inputs(items: list[Token]) -> list[dict]:
    groups: list[list[Token]] = [[]] if items else []
    for token in items:
        if token.value == ",":
            groups.append([])
        else:
            groups[-1].append(token)
    inputs = []
    for index, group in enumerate(groups):
        cursor = 0
        markers = []
        while cursor < len(group) and group[cursor].value in {"*", "?", "~"}:
            markers.append(group[cursor].value)
            cursor += 1
        if cursor >= len(group) or group[cursor].kind != "name":
            raise ValueError("Unrecognized .ops input declaration")
        type_name = group[cursor].value
        cursor += 1
        default = None
        if cursor < len(group):
            suffix = group[cursor:]
            if len(suffix) != 3 or suffix[0].value != "=" or any(
                    token.kind != "name" for token in suffix[1:]):
                raise ValueError("Unrecognized .ops default input")
            default = {"output_type": suffix[1].value, "name": suffix[2].value}
        # The upstream grammar assigns IF_OPTIONAL/IF_WEAK in encounter order.
        connection = "required"
        for marker in markers:
            if marker == "?":
                connection = "optional"
            elif marker == "~":
                connection = "weak"
        inputs.append({"index": index, "type": type_name,
                       "connection": connection, "variadic": "*" in markers,
                       "default_operator": default})
    return inputs


def parameter_fields(items: list[Token], source: str) -> list[dict]:
    """Read declared names/types, including array scope and GUI conditions.

    Fields inside `if` are conditional GUI controls; we do not infer conditional
    runtime existence. `continue flags` entries extend earlier controls and are
    retained as such, rather than counted as additional stored parameters.
    """
    fields = []

    def field(statement: list[Token], in_array: bool, conditions: list[str]) -> None:
        depth = 0
        start = None
        for index, token in enumerate(statement):
            if depth == 0 and token.kind == "name" and token.value in _VALUE_TYPES:
                start = index
                break
            if token.value in {"(", "[", "{"}:
                depth += 1
            elif token.value in {")", "]", "}"}:
                depth -= 1
        if start is None:
            # Layout directives and storage padding are not named controls.
            if statement and not any(t.value in {"group", "label", "tie", "padding"}
                                     for t in statement if t.kind == "name"):
                raise ValueError(f"Unknown parameter declaration at line {statement[0].line}")
            return
        type_token = statement[start]
        cursor = start + 1
        character_capacity = None
        if type_token.value == "char" and cursor < len(statement) and statement[cursor].value == "[":
            close = matching(statement, cursor)
            character_capacity = "".join(t.value for t in statement[cursor + 1:close])
            cursor = close + 1
        label = None
        if cursor < len(statement) and statement[cursor].kind == "string":
            label = text_value(statement[cursor])
            cursor += 1
        name = None
        if cursor < len(statement) and statement[cursor].kind == "name":
            name = statement[cursor].value
            cursor += 1
        if cursor < len(statement) and statement[cursor].kind == "string":
            label = text_value(statement[cursor])
            cursor += 1
        count = None
        if cursor < len(statement) and statement[cursor].value == "[":
            close = matching(statement, cursor)
            count = "".join(t.value for t in statement[cursor + 1:close])
        record = {"type": type_token.value, "name": name, "line": type_token.line}
        if label is not None:
            record["label"] = label
        if count is not None:
            record["array_length_expression"] = count
        if character_capacity is not None:
            record["character_capacity_expression"] = character_capacity
        if in_array:
            record["in_parameter_array"] = True
        if conditions:
            record["gui_conditions"] = conditions.copy()
        if any(t.value == "continue" for t in statement[:start]):
            record["continues_previous_flags"] = True
        if any(t.value == "anim" for t in statement[:start]):
            record["declared_animatable"] = True
        fields.append(record)

    def parse_sequence(begin: int, end: int, in_array: bool, conditions: list[str]) -> None:
        cursor = begin
        while cursor < end:
            cursor = parse_one(cursor, end, in_array, conditions)

    def parse_one(cursor: int, end: int, in_array: bool, conditions: list[str]) -> int:
        token = items[cursor]
        if token.value == "if":
            if cursor + 1 >= end or items[cursor + 1].value != "(":
                raise ValueError(f"Invalid parameter condition at line {token.line}")
            close = matching(items, cursor + 1)
            expression = source[items[cursor + 1].end:items[close].start].strip()
            return parse_one(close + 1, end, in_array, conditions + [expression])
        if token.value == "array":
            opening = cursor + 1
            while opening < end and items[opening].value != "{":
                opening += 1
            if opening >= end:
                raise ValueError(f"Invalid parameter array at line {token.line}")
            close = matching(items, opening)
            parse_sequence(opening + 1, close, True, conditions)
            return close + 1
        if token.value == "{":
            close = matching(items, cursor)
            parse_sequence(cursor + 1, close, in_array, conditions)
            return close + 1
        finish = cursor
        while finish < end and items[finish].value != ";":
            if items[finish].value in {"(", "[", "{"}:
                finish = matching(items, finish)
            finish += 1
        if finish >= end:
            raise ValueError(f"Unterminated parameter declaration at line {token.line}")
        field(items[cursor:finish], in_array, conditions)
        return finish + 1

    parse_sequence(0, len(items), False, [])
    return fields


def parse_ops(source: str, path: str) -> list[dict]:
    """Return every top-level operator declaration, failing on unknown syntax."""
    items = tokens(source)
    result = []
    cursor = 0
    while cursor < len(items):
        token = items[cursor]
        if token.value == "operator":
            if cursor + 3 >= len(items) or any(
                    t.kind != "name" for t in items[cursor + 1:cursor + 3]):
                raise ValueError(f"Malformed operator at {path}:{token.line}")
            output_type = items[cursor + 1].value
            name = items[cursor + 2].value
            opening = cursor + 3
            label = name
            if items[opening].kind == "string":
                label = text_value(items[opening])
                opening += 1
            if items[opening].value != "(":
                raise ValueError(f"Missing operator inputs at {path}:{token.line}")
            close_inputs = matching(items, opening)
            body = close_inputs + 1
            if items[body].value != "{":
                raise ValueError(f"Missing operator body at {path}:{token.line}")
            close_body = matching(items, body)
            flags = []
            tab = output_type
            blocks = []
            fields = []
            at = body + 1
            while at < close_body:
                current = items[at]
                if current.value == "parameter" and items[at + 1].value == "{":
                    end = matching(items, at + 1)
                    blocks.append({"line": current.line, "end_line": items[end].line})
                    fields.extend(parameter_fields(items[at + 2:end], source))
                    at = end + 1
                elif current.value in {"flags", "tab"} and items[at + 1].value == "=":
                    end = at + 2
                    while end < close_body and items[end].value != ";":
                        end += 1
                    values = [t.value for t in items[at + 2:end] if t.kind == "name"]
                    if current.value == "flags":
                        flags.extend(values)
                    elif len(values) == 1:
                        tab = values[0]
                    else:
                        raise ValueError(f"Unrecognized tab at {path}:{current.line}")
                    at = end + 1
                elif current.value in {"{", "(", "["}:
                    at = matching(items, at) + 1
                else:
                    at += 1
            roles = []
            if output_type == "UnitTest":
                roles.append("test_support")
            if Path(path).name == "poc_ops.ops":
                roles.append("poc_library")
            if "hide" in flags:
                roles.append("hidden")
            if "obsolete" in flags:
                roles.append("obsolete")
            if "conversion" in flags:
                roles.append("conversion")
            result.append({
                "key": f"wz4:{path}:{output_type}.{name}",
                "name": name,
                "display_name": label,
                "original_family": output_type,
                "declared_tab": tab,
                "legacy_id": None,
                "source": {"path": path, "line": token.line,
                           "end_line": items[close_body].line,
                           "url": f"{SOURCE_REPOSITORY}/blob/{SOURCE_REVISION}/{path}#L{token.line}"},
                "inputs": signature_inputs(items[opening + 1:close_inputs]),
                "output_type": output_type,
                "declared_flags": sorted(set(flags)),
                "source_roles": roles,
                "parameters": {"status": "declared_names_types_and_gui_conditions",
                               "fields": fields, "source_blocks": blocks},
                "semantic_review": "unreviewed",
                "build_availability": "not_evaluated",
                "demo_usage": "unknown",
            })
            cursor = close_body + 1
        elif token.value in {"{", "(", "["}:
            # Includes type declarations and opaque embedded C++ blocks.
            cursor = matching(items, cursor) + 1
        elif token.value in {"}", ")", "]"}:
            raise ValueError(f"Unmatched closing delimiter at {path}:{token.line}")
        else:
            cursor += 1
    keys = [item["key"] for item in result]
    if len(keys) != len(set(keys)):
        raise ValueError(f"Duplicate source operator identity in {path}")
    return result


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def build_catalog(reference_root: Path) -> dict:
    reference_root = Path(reference_root)
    discovered = {str(p.relative_to(reference_root)).replace("\\", "/")
                  for p in (reference_root / "altona_wz4").rglob("*.ops")}
    if discovered != set(EXPECTED_SOURCES):
        missing = sorted(set(EXPECTED_SOURCES) - discovered)
        extra = sorted(discovered - set(EXPECTED_SOURCES))
        raise ValueError(f"Reference .ops manifest mismatch: missing={missing}, extra={extra}")
    source_files = []
    operators = []
    for path, expected in sorted(EXPECTED_SOURCES.items()):
        data = (reference_root / path).read_bytes()
        actual = git_blob_sha(data)
        if actual != expected:
            raise ValueError(f"Reference byte mismatch for {path}: {actual} != {expected}")
        try:
            entries = parse_ops(data.decode("utf-8-sig"), path)
        except ValueError as error:
            raise ValueError(f"{path}: {error}") from error
        source_files.append({"path": path, "git_blob_sha": actual,
                             "declared_operator_count": len(entries)})
        operators.extend(entries)
    families = Counter(item["original_family"] for item in operators)
    roles = Counter(role for item in operators for role in item["source_roles"])
    return {
        "schema_version": 1,
        "source_repository": SOURCE_REPOSITORY,
        "source_revision": SOURCE_REVISION,
        "scope": {
            "product": "Werkkzeug4",
            "source_selection": "Every .ops file under altona_wz4 at the pinned revision",
            "count_meaning": "Source declarations, including hidden, obsolete, conversion, test and POC-library entries; not a count of available production operators",
            "reference_completeness": "complete_for_the_33_pinned_ops_source_files",
        },
        "extraction_notes": [
            "Only top-level operator declarations are counted; comments, strings, type definitions and embedded C++ blocks are excluded.",
            "Every selected source file is checked against the upstream Git blob SHA. The complete .ops path set must match the reference manifest.",
            "Numeric operator IDs are not declared in this DSL; legacy_id is null. Identity uses source path, output type and declared name, with repository revision recorded separately.",
            "Input type, optional/weak connection, variadic marker and default operator are read from the declaration; build registration and demo reachability are not evaluated.",
            "Parameter names, types, array lengths, GUI conditions, anim markers and continuation flags are metadata only. Ranges, defaults, flags encodings, actual animation bindings, custom controls and runtime behavior require semantic review at the cited source blocks.",
            "Repeated continue-flags controls and repeated parameter-array fields are preserved; their number is not a count of independently stored runtime parameters.",
            "Source declarations from poc_ops.ops and UnitTest output types are explicitly tagged; hidden and obsolete declarations remain in the reference.",
            "This catalog contains no Farbrausch runtime implementation and claims no nodewerk coverage or demo compatibility.",
        ],
        "source_files": source_files,
        "operators": operators,
        "summary": {
            "source_file_count": len(source_files),
            "declared_operator_count": len(operators),
            "by_output_type": dict(sorted(families.items())),
            "by_source_role": dict(sorted(roles.items())),
        },
    }


def serialize_catalog(catalog: dict) -> str:
    """Keep generated data reviewable: one complete operator record per line."""
    metadata = {key: value for key, value in catalog.items() if key != "operators"}
    prefix = json.dumps(metadata, ensure_ascii=False, indent=2)
    records = ["    " + json.dumps(record, ensure_ascii=False, separators=(",", ":"))
               for record in catalog["operators"]]
    return prefix[:-2] + ',\n  "operators": [\n' + ",\n".join(records) + "\n  ]\n}\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-root", type=Path,
                        default=Path(__file__).resolve().parents[1] / "references/farbrausch/fr_public")
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parents[1] / "references/operator_catalog/werkkzeug4.json")
    parser.add_argument("--check", action="store_true",
                        help="Verify the committed catalog without overwriting it")
    args = parser.parse_args()
    catalog = build_catalog(args.reference_root)
    encoded = serialize_catalog(catalog)
    if args.check:
        if args.output.read_text(encoding="utf-8") != encoded:
            raise SystemExit(f"Catalog is stale: {args.output}")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(json.dumps(catalog["summary"], sort_keys=True))


if __name__ == "__main__":
    main()
