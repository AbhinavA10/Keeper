# PyInstaller build for a single portable PhotoCull.exe.
#   conda activate photocull
#   pyinstaller photocull.spec
# Output: dist\PhotoCull.exe
from PyInstaller.utils.hooks import collect_all

heif_datas, heif_binaries, heif_hidden = collect_all("pillow_heif")

a = Analysis(
    ["photocull.py"],
    datas=[("photocull.html", ".")] + heif_datas,
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
    name="PhotoCull",
    icon="assets/photocull.ico",
    console=True,  # the console window shows status; closing it quits PhotoCull
    upx=False,     # UPX-compressed exes trigger more antivirus false positives
)
