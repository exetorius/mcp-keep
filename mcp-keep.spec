# -*- mode: python ; coding: utf-8 -*-
#
# Windows two-binary build. One PyInstaller Analysis, two EXE targets sharing a
# single _internal/ runtime via one COLLECT:
#   - mcp-keep-relay.exe     the relay (--serve)
#   - mcp-keep-watchdog.exe  the crash-supervisor (--watchdog)
#
# Both are byte-identical except their embedded name — the role is still chosen
# by the --serve / --watchdog flag, so a bare double-click of either still just
# prints usage and exits without binding (#56). The distinct names let Task
# Manager show which process is the relay vs the supervisor, and let the watchdog
# spawn the relay-named binary so the relay PID reads as mcp-keep-relay.exe.
# Both are --noconsole (windowless) per #8; logs go to ~/.mcp-keep/keep.log.
#
# CI uses this spec on Windows only (`pyinstaller mcp-keep.spec`). Mac/Linux
# build the single `mcp-keep` console binary via the pyinstaller CLI — no
# watchdog process exists there (launchd KeepAlive / systemd Restart= supervise).


a = Analysis(
    ['python\\proxy.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

relay_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='mcp-keep-relay',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
watchdog_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='mcp-keep-watchdog',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    relay_exe,
    watchdog_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='mcp-keep',
)
