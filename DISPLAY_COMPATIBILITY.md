# MiuiCamera and logical screen resolution

This fixes the supplied Camera **5.3.001510.1** prebuilt when mondrian changes
between logical **1440 × 3200** and **1080 × 2400** through WindowManager.

## Cause and fix

`y2.b.r0(Context)` stores window bounds as `app_bound_wide` / `app_bound_thin`
and validates them against `persist.sys.miui_resolution` and the device name.
That property does not change when AOSP sets a forced display size. The old
bounds survive both process death and reboot, so a 1440-pixel layout can remain
inside a 1080-pixel window. Clearing camera data only temporarily hides this.

The display patch instead reads `WindowManager.getCurrentWindowMetrics()` at
each initialization. Existing lifecycle calls cover activity creation,
configuration changes and restart. The in-memory layout cache now also checks
density, because WindowManager size and density updates are separate operations.
The existing system-bar dimension cache is invalidated before rebuilding.

`miuix.autodensity.f.x(...)` independently assumes its logical size equals the
maximum physical display mode unless MIUI updates the resolution property.
It now reads logical dimensions from Android `DisplayMetrics`, normalizes
orientation, and proportionally adjusts the default DPI with rounding. Native
dimensions remain available to MiuiX's physical-screen calculation. The existing
user-density read is retained. There are no fixed 420/560 DPI values in this fix.

This changes display geometry and UI density inside the camera. Photo/video
capture resolutions, sensor configuration and camera native libraries are
preserved. Camera preferences and saved photos do not need to be erased.

## Build integration

Both `device_xiaomi_miuicamera-cupid` and `vendor_xiaomi_miuicamera-cupid` need
their `codex/mondrian-camera-resolution` changes. The vendor `MiuiCamera` import
consumes `MiuiCameraLogicalDisplay`, a Soong rule which:

1. Reads the original prebuilt assembled by `vendorsetup.sh`.
2. Disassembles only `classes.dex` and `classes5.dex` with AOSP's
   `smali-baksmali`, applies the reviewed patch, and assembles with `android-smali`.
3. Copies the original ZIP entries, replacing only those two DEX files.
4. Discards any obsolete signatures and bundled baseline profiles whose DEX
   checksums/indices no longer match. The existing platform signing and APK
   alignment stages then process the generated APK normally.

The rule uses the host tools from `external/google-smali` in the current A17
manifest. No host downloads, external apktool installation, root commands,
runtime force-stop, or source-tree APK modifications are needed.

After applying both commits, use the usual `source build/envsetup.sh`, product
selection and full ROM build command. For a module-only compilation check:

```bash
m MiuiCamera
```

That command is optional before a full build. It does not create a ROM ZIP.
The existing screen-resolution app and SettingsLib patch remain the prerequisites
for the resolution setting itself; this camera change adds no framework patch.

`vendorsetup.sh` preserves tracked APK parts, joins only numbered `.part` files,
replaces the assembled input atomically and avoids changing its timestamp when
the bytes have not changed. It also accepts an already assembled APK from the
old script, which deleted its parts. Soong rebuilds the patched output when the
original APK, patch, or patcher changes. Failed patching stops the build and never
overwrites the original prebuilt.

The display patch is deliberately separate from the legacy extraction patch
series in `patches/`: it is applied by Soong to the actual supplied prebuilt.
Do not apply it to the input APK a second time with apktool. If regenerating the
vendor makefiles, retain the `MiuiCameraLogicalDisplay` rule and the import's
generated-APK reference; generated makefiles otherwise restore the plain import.
When updating the camera APK, review this patch against the new implementation.
Unmatched or ambiguous smali context is an error, not an unpatched fallback.

## DEX format and the startup regression

The original patcher used `--api 35` for both smali tools. In the inspected AOSP
google-smali this selects DEX **041**, while its writer still emits the old **112-byte**
header. ART expects **120 bytes** for 041 and rejects it with
`Header size is 112 but 120 was expected`. The later
`ClassNotFoundException: com.android.camera.CameraAppImpl` is a consequence of
the failed DEX load, even though the class is present.

All seven DEX files in this prebuilt are **039**. The patcher now selects the
assembler API from each original DEX version and requires the output to keep
that version. For 039 it uses **API 28**, which consistently selects 039 in
both legacy smali and AOSP google-smali. This is a bytecode-format setting;
it does not change the manifest's min/target SDK or prevent references to
newer Android methods such as `getCurrentWindowMetrics()`.

Input DEX files, assembled replacements and all DEX files in the final APK are
checked for version, header size, file size, byte order, SHA-1 and Adler-32.
The final DEX entry set must also match the original. A malformed or unexpectedly
upgraded DEX fails the build before replacing the output APK. This guard does
not implement ART's full bytecode verification or support DEX 041 containers.

References: [AOSP DEX header format](https://source.android.com/docs/core/runtime/dex-format#header-item)
and [AOSP google-smali API mapping](https://android.googlesource.com/platform/external/google-smali/+/refs/heads/main/dexlib2/src/main/java/com/android/tools/smali/dexlib2/VersionMap.java).

## Verification

Inspected input revisions:

| Input | Revision |
| --- | --- |
| Device camera tree, `avium-16.2` | `476ecdabd20672cf26b9a0ac5d7566c709e75d62` |
| Vendor camera tree, `avium-16.2` | `0e4f37a4cd5a1374c2d68ef5003d33a1c8ad4322` |
| Assembled input APK, SHA-256 | `0d2c44a72ddb4f283ec8c02fbadee04ec9d836e188d852d2a68ef8cb4c4895f6` |

Initial validation ran the host pipeline using smali/baksmali 2.3.3;
the patched smali also assembled independently with apktool 2.12.1. ZIP content
comparison found **11,873 unchanged entries**, two changed DEX files and only the
two obsolete baseline-profile entries removed. Re-disassembly compared **13,612
classes**: only `y2.b` and `miuix.autodensity.f` changed after excluding generated
disassembler comments.

DEX assembly uses one worker to keep pool ordering reproducible. Two independent
runs of that initial pipeline produced byte-for-byte identical APKs. Those tests
did not exercise the newer google-smali API-to-DEX mapping and missed the startup
regression described above.

The format fix was checked with **Google smali/baksmali 3.0.10**. A minimal
`--api 35` assembly reproduced DEX 041 with a 112-byte header; the native
`dexdump` from **Android SDK Build Tools 36** rejected it with the exact error
reported on the phone. The same source assembled with API 28 passed.

The full production pipeline then rebuilt the original camera APK with those
new smali tools. Android's native `dexdump -c` accepted the resulting APK.
All seven DEX files remained 039/112; all 13,612 class definitions in the two
rebuilt DEX files were preserved, including `CameraAppImpl`. The same 11,873
other ZIP entries were unchanged. The 18 host tests include rejecting malformed
assembler output without overwriting a previous APK, DEX version drift, missing
secondary DEX files and corrupt headers/digests.

Run the patch/packaging failure-handling tests with:

```bash
python3 device/xiaomi/miuicamera-cupid/tools/test_patch_camera_apk.py
```

These checks do not execute the camera on Android. A full A17 Soong/ROM build and
device verification are still required. The source establishes the stale-bounds
and density defects; it does not prove the cause of a black preview on its own.

On the new ROM, without clearing camera data:

1. Open Camera in WQHD+, go to Settings, select FHD+, and return through Recents.
   Check that grid thirds, shutter, mode strip, zoom buttons and touch focus align.
2. Repeat FHD+ → WQHD+ and both confirmation/rollback paths. Repeat after the
   camera process has been removed from memory and after a reboot in FHD+.
3. Check Photo/Video, front/rear camera, 0.6×/1×/2×, 4:3/16:9/full preview,
   camera settings and launch from the lock screen. Inspect saved photo/video
   dimensions as well as the preview; they must follow capture settings.
4. Repeat with a custom Display size, gesture/three-button navigation, and
   60/90/120 Hz. Check the return from Gallery and a canceled resolution change.

If an issue remains, capture the current size/density and the camera log after
reproducing it:

```bash
adb shell wm size
adb shell wm density
adb logcat -d -v threadtime -s Display AutoDensity ActivityBase AndroidRuntime
```
