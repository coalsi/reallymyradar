# PyInstaller spec: one desktop app per OS (macOS .app, Windows folder with .exe, Linux folder).
#   uv run pyinstaller packaging/reallymyradar.spec --noconfirm
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
PKG = ROOT / "reallymyradar"
ICON = {"darwin": "icon.icns", "win32": "icon.ico"}.get(sys.platform, "icon.png")

hidden = (
    collect_submodules("pyart", filter=lambda m: ".tests" not in m)
    + collect_submodules("uvicorn") + collect_submodules("pystray") + collect_submodules("ngrok") + ["reallymyradar.server"]
)
datas = [
    (str(PKG / "static"), "reallymyradar/static"),
    (str(PKG / "assets"), "reallymyradar/assets"),
    (str(PKG / "nexrad_sites.json"), "reallymyradar"),
] + [d for d in collect_data_files("pyart") if "testing" not in d[0] and "test" not in Path(d[0]).name] \
  + collect_data_files("pyart", include_py_files=True, includes=["default_config.py"]) \
  + collect_data_files("pyart", includes=["testing/*.txt"]) \
  + collect_data_files("cmweather") + collect_data_files("xradar")  # read as files when pyart imports

a = Analysis([str(ROOT / "packaging" / "launch.py")], pathex=[str(ROOT)], hiddenimports=hidden, datas=datas,
             excludes=["matplotlib.tests", "tkinter", "IPython", "pytest"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ReallymyRadar", console=False,
          icon=str(PKG / "assets" / ICON))
coll = COLLECT(exe, a.binaries, a.datas, name="ReallymyRadar")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="ReallymyRadar.app", icon=str(PKG / "assets" / ICON),
                 bundle_identifier="com.reallymyradar.app",
                 info_plist={"LSUIElement": True,  # menu-bar app: no Dock icon
                             "CFBundleShortVersionString": "0.1.0",
                             "NSHighResolutionCapable": True})
