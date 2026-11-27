"""Focused source-inventory checks; no Farbrausch checkout required."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from werkkzeug4_catalog import build_catalog, parse_ops, serialize_catalog


class Werkkzeug4CatalogTests(unittest.TestCase):
    def test_comments_strings_and_embedded_code_are_not_declarations(self):
        source = '''
        // operator Fake Ignored() {}
        /* operator Fake AlsoIgnored() {} */
        code { const char *text = "operator Fake String() { }";
               class Thing { int operator()(int x) { return x; } }; }
        type Type { extern void Show() { if (1) { } } }
        operator Type Real "Visible name" (?Type=Type Default,*~Type) {
          flags = hide | obsolete;
          code { const char *text = "} escaped \\\" brace {"; }
        }
        '''
        rows = parse_ops(source, "example.ops")
        self.assertEqual([row["name"] for row in rows], ["Real"])
        self.assertEqual(rows[0]["display_name"], "Visible name")
        self.assertEqual(rows[0]["inputs"], [
            {"index": 0, "type": "Type", "connection": "optional", "variadic": False,
             "default_operator": {"output_type": "Type", "name": "Default"}},
            {"index": 1, "type": "Type", "connection": "weak", "variadic": True,
             "default_operator": None},
        ])
        self.assertEqual(rows[0]["source_roles"], ["hidden", "obsolete"])

    def test_parameter_arrays_and_gui_conditions_preserve_metadata(self):
        source = '''operator Mesh Edit(Mesh) {
          parameter {
            group "int is a label";
            padding(1);
            flags Mode("off|on");
            continue flags Mode "Extra" ("*4no|yes");
            if (Mode) { anim float Amount(0..1)=0.2; }
            array numbers { if(input[0]) float Pos[3]; }
            char[64] Title;
            fileout "Filename";
          }
          code { int RuntimeOnly; }
        }'''
        fields = parse_ops(source, "example.ops")[0]["parameters"]["fields"]
        self.assertEqual([f["name"] for f in fields], ["Mode", "Mode", "Amount", "Pos", "Title", None])
        self.assertTrue(fields[1]["continues_previous_flags"])
        self.assertEqual(fields[2]["gui_conditions"], ["Mode"])
        self.assertTrue(fields[2]["declared_animatable"])
        self.assertTrue(fields[3]["in_parameter_array"])
        self.assertEqual(fields[3]["array_length_expression"], "3")
        self.assertEqual(fields[4]["character_capacity_expression"], "64")
        self.assertEqual(fields[5]["label"], "Filename")

    def test_malformed_declarations_fail_instead_of_disappearing(self):
        cases = [
            'operator Mesh Broken(',
            'operator Mesh Broken() {',
            'operator Mesh Broken(Type=OnlyOneName) {}',
            'operator Mesh Broken() { parameter { mystery Value; } }',
            '/* unterminated operator Mesh Ghost() {}',
            'operator Mesh Broken() { code { "unterminated; } }',
            '}',
        ]
        for source in cases:
            with self.subTest(source=source), self.assertRaises((ValueError, IndexError)):
                parse_ops(source, "bad.ops")

    def test_duplicate_source_identity_is_reported(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            parse_ops('operator Mesh Same() {} operator Mesh Same() {}', "same.ops")

    def test_partial_source_tree_cannot_claim_pinned_completeness(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "manifest mismatch"):
                build_catalog(Path(root))

    def test_one_record_per_line_serialization_roundtrips(self):
        import json
        catalog = {"schema_version": 1, "operators": [{"key": "a", "fields": [1, 2]}, {"key": "b"}],
                   "summary": {"count": 2}}
        result = serialize_catalog(catalog)
        self.assertEqual(json.loads(result), catalog)
        self.assertEqual(result, serialize_catalog(catalog))
        self.assertEqual(sum('"key"' in line for line in result.splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
