# -*- coding: utf-8 -*-
"""将 PDF 按页渲染为 JPG 图片。"""

import time
from pathlib import Path

import fitz  # PyMuPDF

# 只需改这一处名称；PDF / 输出目录会据此推导
NAME = "22-神经网络基础与Tensorflow实战"
WORK_DIR = Path(r"e:\tmp") / NAME / NAME
PDF_PATH = WORK_DIR / f"{NAME}.pdf"
OUT_DIR = WORK_DIR / f"{NAME}_pdf_2_imgs"

# 渲染缩放（2.0 ≈ 144 DPI，清晰度足够）
ZOOM = 2.0
JPEG_QUALITY = 90
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MARKDOWN_ASSET_DIR = "Assets"


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
