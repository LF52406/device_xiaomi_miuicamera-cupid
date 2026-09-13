# SPDX-FileCopyrightText: 2026 The LineageOS Project
# SPDX-License-Identifier: Apache-2.0

import importlib.util
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
import zipfile
import zlib


SPEC = importlib.util.spec_from_file_location(
    "patch_camera_apk", Path(__file__).with_name("patch_camera_apk.py"))
PATCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCHER)
PATCH = Path(__file__).resolve().parents[1] / "display-patches" / (
    "0001-Use-live-logical-display-metrics.patch")


def dex_fixture(version=39, header_size=112):
    """An empty standalone DEX with a header and a two-entry map list."""
    data = bytearray(header_size)
    data[:8] = f"dex\n{version:03d}\0".encode("ascii")
    data += struct.pack("<IHHIIHHII", 2, 0, 0, 1, 0, 0x1000, 0, 1, header_size)
    struct.pack_into("<III", data, 32, len(data), header_size, 0x12345678)
    struct.pack_into("<I", data, 52, header_size)
    struct.pack_into("<II", data, 104, len(data) - header_size, header_size)
    if header_size == 120:
        struct.pack_into("<II", data, 112, len(data), 0)
    data[12:32] = hashlib.sha1(data[32:]).digest()
    struct.pack_into("<I", data, 8, zlib.adler32(data[12:]))
    return bytes(data)


class DexFormatTest(unittest.TestCase):
    def test_supported_standalone_formats(self):
        for version in (35, 37, 38, 39, 40):
            with self.subTest(version=version):
                self.assertEqual(PATCHER.validate_dex(
                    dex_fixture(version), "classes.dex"), version)

    def test_reported_041_with_112_byte_header_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "041 header size is 112, expected 120"):
            PATCHER.validate_dex(dex_fixture(41), "classes.dex")

    def test_container_format_requires_separate_support(self):
        with self.assertRaisesRegex(ValueError, "unsupported DEX version 041"):
            PATCHER.validate_dex(dex_fixture(41, 120), "classes.dex")

    def test_tool_must_preserve_original_dex_version(self):
        with self.assertRaisesRegex(ValueError, "changed from 039 to 040"):
            PATCHER.validate_dex(dex_fixture(40), "classes.dex", 39)

    def test_corrupt_envelopes_are_rejected(self):
        original = dex_fixture()
        cases = {
            "truncated DEX header": original[:100],
            "invalid DEX magic": b"bad!" + original[4:],
            "DEX file size": original + b"extra",
            "DEX SHA-1": original[:-1] + bytes([original[-1] ^ 1]),
            "DEX Adler-32": original[:8] + bytes([original[8] ^ 1]) + original[9:],
        }
        for error, data in cases.items():
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                PATCHER.validate_dex(data, "classes.dex")

    def test_unmodified_secondary_dex_is_also_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.apk"
            with zipfile.ZipFile(path, "w") as apk:
                apk.writestr("classes.dex", dex_fixture())
                apk.writestr("classes7.dex", dex_fixture(41))
            with zipfile.ZipFile(path) as apk, self.assertRaisesRegex(
                    ValueError, "classes7.dex: DEX 041"):
                PATCHER.validate_apk_dex(apk)

    def test_missing_secondary_dex_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "output.apk"
            with zipfile.ZipFile(path, "w") as apk:
                apk.writestr("classes.dex", dex_fixture())
            with zipfile.ZipFile(path) as apk, self.assertRaisesRegex(
                    ValueError, "changed its DEX entries"):
                PATCHER.validate_apk_dex(apk, {"classes.dex": 39, "classes5.dex": 39})

    def test_bad_assembler_output_never_replaces_previous_apk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "input.apk", root / "output.apk"
            with zipfile.ZipFile(source, "w") as apk:
                for dex in PATCHER.TARGETS.values():
                    apk.writestr(dex, dex_fixture())
            original = source.read_bytes()
            output.write_bytes(b"previous-successful-build")

            def broken_tool(command, **kwargs):
                # Simulate a tool returning success while emitting the exact
                # malformed envelope reported by ART. The build must reject it.
                if "assemble" in command:
                    Path(command[command.index("--output") + 1]).write_bytes(dex_fixture(41))

            with mock.patch.object(PATCHER.subprocess, "run", side_effect=broken_tool), \
                    mock.patch.object(PATCHER, "patch_sources"), \
                    self.assertRaisesRegex(ValueError, "header size is 112, expected 120"):
                PATCHER.build_apk(source, output, PATCH, "baksmali", "smali", root)
            self.assertEqual(output.read_bytes(), b"previous-successful-build")
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(root.glob("miuicamera-*")), [])

    def test_dex_039_build_selects_api_28_and_keeps_secondary_dex(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "input.apk", root / "output.apk"
            with zipfile.ZipFile(source, "w") as apk:
                for dex in ("classes.dex", "classes2.dex", "classes5.dex"):
                    apk.writestr(dex, dex_fixture())
            commands = []

            def tool(command, **kwargs):
                commands.append(command)
                if "assemble" in command:
                    Path(command[command.index("--output") + 1]).write_bytes(dex_fixture())

            with mock.patch.object(PATCHER.subprocess, "run", side_effect=tool), \
                    mock.patch.object(PATCHER, "patch_sources"):
                PATCHER.build_apk(source, output, PATCH, "baksmali", "smali", root)
            self.assertEqual(len(commands), 4)
            for command in commands:
                self.assertEqual(command[command.index("--api") + 1], "28")
            with zipfile.ZipFile(output) as apk:
                self.assertEqual(PATCHER.validate_apk_dex(apk), {
                    "classes.dex": 39, "classes2.dex": 39, "classes5.dex": 39})


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
