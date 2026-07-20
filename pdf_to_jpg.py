# -*- coding: utf-8 -*-
"""将 PDF 按页渲染为 JPG 图片。"""

from pathlib import Path

import fitz  # PyMuPDF

PDF_PATH = Path(
    r"e:\tmp\17-SGLang 深度优化：Radix 缓存与复杂任务的极致吞吐"
    r"\17-SGLang 深度优化：Radix 缓存与复杂任务的极致吞吐"
    r"\3-SGLang 深度优化：Radix 缓存与复杂任务的极致吞吐.pdf"
)
OUT_DIR = Path(
    r"e:\tmp\17-SGLang 深度优化：Radix 缓存与复杂任务的极致吞吐"
    r"\17-SGLang 深度优化：Radix 缓存与复杂任务的极致吞吐"
    r"\17-SGLang 深度优化：Radix 缓存与复杂任务的极致吞吐_pdf_2_imgs"
)

# 渲染缩放（2.0 ≈ 144 DPI，清晰度足够）
ZOOM = 2.0
JPEG_QUALITY = 90


def main() -> None:
    if not PDF_PATH.is_file():
        raise FileNotFoundError(f"PDF 不存在: {PDF_PATH}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(PDF_PATH)
    matrix = fitz.Matrix(ZOOM, ZOOM)
    total = doc.page_count
    print(f"共 {total} 页，输出目录: {OUT_DIR}")

    for i in range(total):
        page = doc.load_page(i)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        out_path = OUT_DIR / f"page_{i + 1:03d}.jpg"
        pix.save(str(out_path), output="jpeg", jpg_quality=JPEG_QUALITY)
        print(f"已保存: {out_path.name}")

    doc.close()
    print(f"完成，共导出 {total} 张图片。")


if __name__ == "__main__":
    main()
