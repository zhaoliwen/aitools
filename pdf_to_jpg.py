# -*- coding: utf-8 -*-
"""将 PDF 按页渲染为 JPG 图片。"""

import shutil
import time
import zipfile
from pathlib import Path

import fitz  # PyMuPDF

# 手动改这里：源 zip 路径。课程名 / PDF / 输出目录均由其文件名推导
ZIP_PATH = Path(r"e:\source\22-神经网络基础与Tensorflow实战.zip")
TMP_DIR = Path(r"e:\tmp")

NAME = ZIP_PATH.stem
COURSE_DIR = TMP_DIR / NAME
WORK_DIR = COURSE_DIR / NAME
PDF_PATH = WORK_DIR / f"{NAME}.pdf"
OUT_DIR = WORK_DIR / f"{NAME}_pdf_2_imgs"

# 渲染缩放（2.0 ≈ 144 DPI，清晰度足够）
ZOOM = 2.0
JPEG_QUALITY = 90
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MARKDOWN_ASSET_DIR = "Assets"


def _zip_entry_name(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("gbk")
    except UnicodeError:
        return info.filename


def extract_zip(zip_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            name = _zip_entry_name(info)
            target = dest / name
            if info.is_dir() or name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def ensure_course_dir() -> None:
    if COURSE_DIR.is_dir():
        return
    if not ZIP_PATH.is_file():
        raise FileNotFoundError(f"ZIP 不存在: {ZIP_PATH}")

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    dest_zip = TMP_DIR / ZIP_PATH.name
    if dest_zip.resolve() != ZIP_PATH.resolve():
        shutil.copy2(ZIP_PATH, dest_zip)
        print(f"已拷贝 ZIP: {dest_zip}")

    print(f"正在解压: {dest_zip} -> {TMP_DIR}")
    extract_zip(dest_zip, TMP_DIR)
    if not COURSE_DIR.is_dir():
        raise FileNotFoundError(f"解压后未找到目录: {COURSE_DIR}")
    print(f"已解压: {COURSE_DIR}")


def print_markdown_images(out_dir: Path, save_dir: Path) -> None:
    images = sorted(
        p for p in out_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    if not images:
        print("目标目录没有图片。")
        return

    lines = [
        f"![{img.stem.rsplit('_', 1)[0]}]({MARKDOWN_ASSET_DIR}/{img.name})"
        for img in images
    ]

    print("\nMarkdown 引用：")
    print("\n".join(lines))

    md_path = save_dir / f"{PDF_PATH.stem}.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Markdown 已保存: {md_path}")


def main() -> None:
    ensure_course_dir()
    if not PDF_PATH.is_file():
        raise FileNotFoundError(f"PDF 不存在: {PDF_PATH}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(PDF_PATH)
    matrix = fitz.Matrix(ZOOM, ZOOM)
    total = doc.page_count
    ts = int(time.time() * 1000)
    print(f"共 {total} 页，输出目录: {OUT_DIR}")

    for i in range(total):
        page = doc.load_page(i)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        out_path = OUT_DIR / f"page_{i + 1:03d}_{ts}.jpg"
        pix.save(str(out_path), output="jpeg", jpg_quality=JPEG_QUALITY)
        print(f"已保存: {out_path.name}")

    doc.close()
    print(f"完成，共导出 {total} 张图片。")
    print_markdown_images(OUT_DIR, PDF_PATH.parent)


if __name__ == "__main__":
    main()
