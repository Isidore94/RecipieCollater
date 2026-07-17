# PyInstaller recipe for the RecipeCollater Windows desktop app (recipecollater.exe).
#
# Build from the repo root: deploy/windows/build_exe.ps1
#
# The app imports many heavy libraries LAZILY inside worker functions (recipe_scrapers, yt_dlp,
# PIL, bs4, anthropic, openai) so they stay out of the web process's import path. PyInstaller's
# static analysis cannot see those, so they must be force-included here. collect_submodules("app")
# does the same for the app's own lazily/string-imported modules (app.tasks, the provider adapters,
# the pipeline). uvicorn resolves its loop/protocol modules by name at runtime; collect_all keeps
# those importable.
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

# The spec lives in deploy/windows/, so paths are resolved relative to the repo root two up.
# (PyInstaller resolves relative script/data paths against the spec's own directory.)
ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

uv_datas, uv_binaries, uv_hidden = collect_all("uvicorn")
yt_datas, yt_binaries, yt_hidden = collect_all("yt_dlp")
rs_datas, rs_binaries, rs_hidden = collect_all("recipe_scrapers")
pil_datas, pil_binaries, pil_hidden = collect_all("PIL")
# recipe-scrapers parses microformats via extruct -> mf2py, which read data directories
# (e.g. mf2py/backcompat-rules) at import time; collect_all pulls those data files in.
mf_datas, mf_binaries, mf_hidden = collect_all("mf2py")
ex_datas, ex_binaries, ex_hidden = collect_all("extruct")

app_hidden = collect_submodules("app")

datas = uv_datas + yt_datas + rs_datas + pil_datas + mf_datas + ex_datas + [
    (os.path.join(ROOT, "app", "templates"), "app/templates"),
    (os.path.join(ROOT, "app", "static"), "app/static"),
    (os.path.join(ROOT, "app", "migrations"), "app/migrations"),
]

hiddenimports = (
    uv_hidden + yt_hidden + rs_hidden + pil_hidden + mf_hidden + ex_hidden + app_hidden + [
        "anyio",
        "h11",
        "httptools",
        "websockets",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "bs4",
        "lxml",
        "lxml.etree",
        "anthropic",
        "openai",
        "huey",
        "huey.consumer",
        "huey.storage",
        "multipart",
        "python_multipart",
    ]
)

a = Analysis(
    [os.path.join(ROOT, "app", "desktop.py")],
    pathex=[ROOT],
    binaries=uv_binaries + yt_binaries + rs_binaries + pil_binaries + mf_binaries + ex_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "faster_whisper", "tkinter.test"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RecipeCollater",
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
