# SPDX-FileCopyrightText: 2026 The LineageOS Project
# SPDX-License-Identifier: Apache-2.0

import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile


SPEC = importlib.util.spec_from_file_location(
    "patch_camera_apk", Path(__file__).with_name("patch_camera_apk.py"))
PATCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCHER)
PATCH = Path(__file__).resolve().parents[1] / "display-patches" / (
    "0001-Use-live-logical-display-metrics.patch")


class PatchSafetyTest(unittest.TestCase):
    def test_reviewed_patch_parses(self):
        patches = PATCHER.parse_patch(PATCH.read_text())
        self.assertEqual(set(patches), set(PATCHER.TARGETS))
        self.assertEqual(len(patches["smali/y2/b.smali"]), 3)

    def test_shifted_line_numbers_do_not_require_fuzzy_matching(self):
        self.assertEqual(PATCHER.replace_hunks("prefix\nbefore\nsuffix\n", [
            ("before\n", "after\n")]), "prefix\nafter\nsuffix\n")

    def test_ambiguous_context_is_rejected(self):
        with self.assertRaises(ValueError):
            PATCHER.replace_hunks("same\nsame\n", [("same\n", "new\n")])

    def test_unexpected_bytecode_is_rejected(self):
        with self.assertRaises(ValueError):
            PATCHER.replace_hunks("changed\n", [("original\n", "new\n")])

    def test_patch_cannot_escape_expected_classes(self):
        patch = PATCH.read_text().replace("smali/y2/b.smali", "../../outside")
        with self.assertRaises(ValueError):
            PATCHER.parse_patch(patch)

    def test_truncated_patch_is_rejected(self):
        with self.assertRaises(ValueError):
            PATCHER.parse_patch(PATCH.read_text().rsplit("\n", 4)[0])

    def test_archive_preserves_resources_and_libraries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "input.apk", root / "output.apk"
            dex = root / "classes.dex"
            dex.write_bytes(b"new-dex")
            original = {
                "classes.dex": b"old-dex",
                "classes2.dex": b"unchanged-dex",
                "resources.arsc": b"resource-table",
                "AndroidManifest.xml": b"binary-manifest",
                "lib/arm64-v8a/camera.so": b"native-library",
                "assets/settings.bin": b"settings",
                "META-INF/services/test": b"service",
                "META-INF/CERT.RSA": b"old-signature",
                "META-INF/CERT.SF": b"old-signature-metadata",
                "META-INF/MANIFEST.MF": b"old-entry-digests",
                "assets/dexopt/baseline.prof": b"old-dex-indices",
                "assets/dexopt/baseline.profm": b"old-dex-indices",
            }
            with zipfile.ZipFile(source, "w") as apk:
                for name, content in original.items():
                    kind = zipfile.ZIP_DEFLATED if name.startswith("assets/") else 0
                    apk.writestr(name, content, compress_type=kind)
            input_bytes = source.read_bytes()
            PATCHER.write_apk(source, output, {"classes.dex": dex})
            with zipfile.ZipFile(output) as apk, zipfile.ZipFile(source) as src:
                for name, content in original.items():
                    if PATCHER.is_signature(name) or name in PATCHER.PROFILES:
                        self.assertNotIn(name, apk.namelist())
                    else:
                        expected = b"new-dex" if name == "classes.dex" else content
                        self.assertEqual(apk.read(name), expected)
                        self.assertEqual(apk.getinfo(name).compress_type,
                                         src.getinfo(name).compress_type)
            self.assertEqual(source.read_bytes(), input_bytes)

    def test_failed_build_preserves_existing_output_and_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "input.apk", root / "output.apk"
            with zipfile.ZipFile(source, "w") as apk:
                apk.writestr("resources.arsc", b"missing-dex")
            output.write_bytes(b"previous-successful-build")
            original = source.read_bytes()
            with self.assertRaises(KeyError):
                PATCHER.build_apk(source, output, PATCH, "unused", "unused", root)
            self.assertEqual(output.read_bytes(), b"previous-successful-build")
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(root.glob("miuicamera-*")), [])

    def test_in_place_write_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "input.apk"
            path.write_bytes(b"original")
            with self.assertRaises(ValueError):
                PATCHER.build_apk(path, path, PATCH, "unused", "unused", root)
            self.assertEqual(path.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
