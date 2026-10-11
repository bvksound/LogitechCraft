# PyInstaller spec: `pyinstaller craft_crown.spec`
# Windows -> dist/CraftCrown/CraftCrown.exe, macOS -> dist/CraftCrown.app (menubalk-app, geen Dock-icoon)
import sys

a = Analysis(["craft_crown.py"], hiddenimports=["hid"] + (
    ["pynput.keyboard._win32", "pynput.mouse._win32"] if sys.platform.startswith("win") else
    ["pynput.keyboard._darwin", "pynput.mouse._darwin"] if sys.platform == "darwin" else []))
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="CraftCrown", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="CraftCrown")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="CraftCrown.app", bundle_identifier="be.craftcrown.app",
                 info_plist={"LSUIElement": True})
