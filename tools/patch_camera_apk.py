#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The LineageOS Project
# SPDX-License-Identifier: Apache-2.0

"""Apply the reviewed display fix while preserving the prebuilt APK's resources.

Invoked by Soong, with declared smali/baksmali host tools. No downloads, source
tree writes, apktool resource rebuild, or device-side commands are involved.
"""

import argparse
import copy
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import zipfile


TARGETS = {
    "smali/y2/b.smali": "classes.dex",
    "smali_classes5/miuix/autodensity/f.smali": "classes5.dex",
}
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
PROFILES = {"assets/dexopt/baseline.prof", "assets/dexopt/baseline.profm"}


def parse_patch(text):
    """Read a unified diff, accepting only the two reviewed classes."""
    lines = text.splitlines(keepends=True)
    patches = {}
    i = 0
    target = None
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- a/"):
            old = line[len("--- a/"):].rstrip("\n")
            i += 1
            if i >= len(lines) or lines[i] != "+++ b/" + old + "\n":
                raise ValueError("Patch must modify a file in place")
            if old not in TARGETS or old in patches:
                raise ValueError(f"Unexpected or duplicate patch target: {old}")
            target = patches[old] = []
        elif line.startswith("@@"):
            match = HUNK.match(line)
            if target is None or match is None:
                raise ValueError("Invalid patch hunk")
            old_count = int(match[2]) if match[2] is not None else 1
            new_count = int(match[4]) if match[4] is not None else 1
            old_lines, new_lines = [], []
            i += 1
            while len(old_lines) < old_count or len(new_lines) < new_count:
                if i >= len(lines):
                    raise ValueError("Truncated patch hunk")
                item = lines[i]
                # Git also accepts an empty line as empty context.
                if item == "\n":
                    item = " \n"
                if item[:1] not in (" ", "-", "+"):
                    raise ValueError("Invalid patch hunk content")
                if item[0] in (" ", "-"):
                    old_lines.append(item[1:])
                if item[0] in (" ", "+"):
                    new_lines.append(item[1:])
                i += 1
            if len(old_lines) != old_count or len(new_lines) != new_count:
                raise ValueError("Patch hunk length mismatch")
            if not old_lines or not new_lines:
                raise ValueError("Patch hunks must have context")
            target.append(("".join(old_lines), "".join(new_lines)))
            continue
        i += 1
    if set(patches) != set(TARGETS) or any(not v for v in patches.values()):
        raise ValueError("Patch must cover both display classes")
    return patches


def replace_hunks(text, hunks):
    # Ignore source line numbers, but never use fuzzy/ambiguous context matching.
    for before, after in hunks:
        if text.count(before) != 1:
            raise ValueError("Camera bytecode does not match the reviewed patch")
        text = text.replace(before, after, 1)
    return text


def patch_sources(root, patches):
    results = {}
    for name, hunks in patches.items():
        path = root.joinpath(*PurePosixPath(name).parts)
        original = path.read_text(encoding="utf-8")
        try:
            results[path] = replace_hunks(original, hunks)
        except ValueError:
            raise ValueError(f"Unsupported MiuiCamera implementation: {name}") from None
    for path, text in results.items():
        path.write_text(text, encoding="utf-8")


def is_signature(name):
    upper = name.upper()
    return upper.startswith("META-INF/") and (
        upper.endswith((".SF", ".RSA", ".DSA", ".EC"))
        or upper == "META-INF/MANIFEST.MF"
    )


def write_apk(source, destination, replacements):
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(destination, "w") as dst:
        names = src.namelist()
        if len(names) != len(set(names)):
            raise ValueError("APK contains duplicate ZIP entries")
        for info in src.infolist():
            if is_signature(info.filename) or info.filename in PROFILES:
                continue
            metadata = copy.copy(info)
            if info.filename in replacements:
                dst.writestr(metadata, replacements[info.filename].read_bytes())
            else:
                with src.open(info) as reader, dst.open(metadata, "w") as writer:
                    shutil.copyfileobj(reader, writer)


def build_apk(input_apk, output_apk, patch, baksmali, smali, work_dir):
    if input_apk.resolve() == output_apk.resolve():
        raise ValueError("Input APK must not be overwritten")
    patches = parse_patch(patch.read_text(encoding="utf-8"))
    work_dir.mkdir(parents=True, exist_ok=True)
    output_apk.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="miuicamera-", dir=work_dir) as temp:
        root = Path(temp)
        with zipfile.ZipFile(input_apk) as src:
            for target, dex in TARGETS.items():
                dex_path = root / dex
                dex_path.write_bytes(src.read(dex))
                directory = root / target.split("/", 1)[0]
                subprocess.run([
                    baksmali, "-JXmx1g", "disassemble", "--api", "35",
                    "--use-locals", "--sequential-labels", "--jobs", "2",
                    "--output", str(directory), str(dex_path),
                ], check=True)
        patch_sources(root, patches)
        replacements = {}
        for target, dex in TARGETS.items():
            rebuilt = root / ("patched-" + dex)
            # Serial interning avoids scheduling-dependent DEX pool ordering.
            subprocess.run([
                smali, "-JXmx1g", "assemble", "--api", "35", "--jobs", "1",
                "--output", str(rebuilt), str(root / target.split("/", 1)[0]),
            ], check=True)
            replacements[dex] = rebuilt
        temporary_apk = root / "MiuiCamera.apk"
        write_apk(input_apk, temporary_apk, replacements)
        with zipfile.ZipFile(temporary_apk) as check:
            if check.testzip() is not None:
                raise ValueError("Rebuilt APK failed ZIP verification")
        os.replace(temporary_apk, output_apk)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--baksmali", required=True)
    parser.add_argument("--smali", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        build_apk(args.input, args.output, args.patch, args.baksmali,
                  args.smali, args.work_dir)
    except (ValueError, OSError, KeyError, zipfile.BadZipFile,
            subprocess.CalledProcessError) as error:
        parser.exit(1, f"MiuiCamera display patch failed: {error}\n")


if __name__ == "__main__":
    main()
