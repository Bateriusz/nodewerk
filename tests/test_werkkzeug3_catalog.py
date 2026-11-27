"""Focused checks for reference-inventory parsing and provenance."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "werkkzeug3_catalog.py"
SPEC = importlib.util.spec_from_file_location("werkkzeug3_catalog", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
catalog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog)

ROW = '''{
  "Synthetic", 0x77, KC_MESH, 0, KK_MAXINPUT, 0x0, COL_NEW, 0,
  "f" "i", Edit_Synthetic, Init_Synthetic, Exec_Synthetic, { KC_MESH }, { 0 },
}'''


def source(rows: str, sentinel: bool = True) -> str:
    return '''void Edit_Synthetic(App *app) {
  app->ParaWin->EditFlags("Label, /* literal */", 0, 1, "x|y");
}
WerkClass WerkClasses[] = {
''' + rows + (", { 0 }" if sentinel else "") + "\n};\n"


class Werkkzeug3CatalogTests(unittest.TestCase):
    def extract(self, text: str):
        return catalog.extract_registry(text, "synthetic", "WerkClasses", {"KK_MAXINPUT": 17})

    def test_comments_are_excluded_but_literals_and_adjacent_strings_survive(self):
        result = self.extract(source("/* " + ROW.replace("Synthetic", "Removed") + " */\n" + ROW))
        self.assertEqual(len(result), 1)
        entry = result[0]
        self.assertEqual(entry["name"], "Synthetic")
        self.assertEqual(entry["packing"], "fi")
        self.assertEqual(entry["ports"]["maximum_inputs"], 17)
        self.assertEqual(entry["parameters"]["direct_editor_calls"][0]["label"], "Label, /* literal */")

    def test_duplicate_ids_are_not_silently_merged(self):
        with self.assertRaisesRegex(ValueError, "duplicate operator key"):
            self.extract(source(ROW + "," + ROW))

    def test_unknown_row_shape_is_not_silently_skipped(self):
        malformed = ROW.replace(", { KC_MESH }, { 0 }", "")
        with self.assertRaisesRegex(ValueError, "field count"):
            self.extract(source(malformed))

    def test_truncated_registry_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing terminating"):
            self.extract(source(ROW, sentinel=False))

    def test_unrelated_source_cannot_claim_the_pinned_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = sorted(catalog.SOURCE_FILES)[0]
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_bytes(b"unrelated revision\n")
            with self.assertRaisesRegex(ValueError, "Source revision mismatch"):
                catalog.build_catalog(root)

    def test_animation_names_do_not_collapse_distinct_opcodes(self):
        text = '''const WerkOpAnimInfo AnimOpCmds[] = {
          { "nop", KA_NOP, 1, 0, AOU_OFF },
          { "loadpara", KA_LOADPARA1, 0, AOIF_LOADPARA, AOU_OFF },
          { "loadpara", KA_LOADPARA2, 0, AOIF_LOADPARA, AOU_OFF },
          { 0 }
        };'''
        entries = catalog.extract_animation_registry(text, "synthetic", {
            "KA_NOP": 0, "KA_LOADPARA1": 3, "KA_LOADPARA2": 4,
        })
        self.assertEqual([entry["legacy_id"] for entry in entries], ["0x0000", "0x0003", "0x0004"])


if __name__ == "__main__":
    unittest.main()
