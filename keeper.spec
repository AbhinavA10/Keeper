# PyInstaller build for a single portable Keeper.exe.
#   conda activate keeper
#   pyinstaller keeper.spec
# Output: dist\Keeper.exe
from PyInstaller.utils.hooks import collect_all

heif_datas, heif_binaries, heif_hidden = collect_all("pillow_heif")

a = Analysis(
    ["keeper.py"],
    datas=[("keeper.html", ".")] + heif_datas,
    binaries=heif_binaries,
    hiddenimports=heif_hidden,
    excludes=["numpy", "matplotlib", "pytest", "IPython"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="Keeper",
    icon="assets/keeper.ico",
    console=True,  # the console window shows status; closing it quits Keeper
    upx=False,     # UPX-compressed exes trigger more antivirus false positives
)
