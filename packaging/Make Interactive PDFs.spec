# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)


ROOT = Path(SPECPATH).resolve().parent

datas = [
    (str(ROOT / "interactive_pdf_app" / "static"), "interactive_pdf_app/static"),
    (str(ROOT / "Icon"), "Icon"),
]
for relative in (
    "VERSION",
):
    datas.append((str(ROOT / relative), "."))
datas += collect_data_files("pdfminer")
datas += collect_data_files("rapidocr")
for package in (
    "pypdf",
    "pdfplumber",
    "pdfminer.six",
    "pypdfium2",
    "Pillow",
    "cryptography",
    "rapidocr",
    "onnxruntime",
    "numpy",
    "opencv-python",
    "pyclipper",
    "PyYAML",
    "Shapely",
    "six",
    "tqdm",
    "omegaconf",
    "antlr4-python3-runtime",
    "requests",
    "certifi",
    "idna",
    "urllib3",
    "colorlog",
    "flatbuffers",
    "packaging",
    "protobuf",
):
    datas += copy_metadata(package)

binaries = collect_dynamic_libs("pypdfium2_raw")

hiddenimports = collect_submodules("rapidocr") + [
    "make_interactive_pdf",
    "local_ocr",
    "progress_events",
    "skill_provenance",
    "verify_interactive_pdf",
    "multipart",
    "multipart.multipart",
    "python_multipart",
    "python_multipart.multipart",
    "pypdf._crypt_providers._cryptography",
    "uvicorn.lifespan.on",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.h11_impl",
]

a = Analysis(
    [str(ROOT / "packaging" / "entrypoint.py")],
    pathex=[str(ROOT), str(ROOT / "scripts")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "Crypto",
        "fitz",
        "gunicorn",
        "httptools",
        "pymupdf",
        "pytest",
        "reportlab",
        "trio",
        "uvloop",
        "watchfiles",
        "websockets",
        "wsproto",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Make Interactive PDFs",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(ROOT / "packaging" / "version_info.txt"),
    icon=str(ROOT / "Icon" / "Make Interactive PDFs.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Make Interactive PDFs",
)
