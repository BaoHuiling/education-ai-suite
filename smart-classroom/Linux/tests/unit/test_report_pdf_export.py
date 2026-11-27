"""Real WeasyPrint rendering of the class report (skipped where Pango is missing)."""
import struct
import zlib

import pytest

from components.report_generator import pdf_export

pytestmark = pytest.mark.skipif(
    not pdf_export.pdf_export_available(),
    reason="WeasyPrint/Pango not installed (the Linux app image has them)",
)

SID = "20261127-120000-abcd"

REPORT_ZH = f"""# 课后总结报告

河畔中学 三年级 - 二班

# 一、课堂概况

1. 时长：45 分钟

2. 平均语速：**562 字/分钟**

思维导图：

![Mind map](/report/{SID}/mindmap-image)
"""


def _png(path, w=40, h=20):
    raw = b"".join(b"\x00" + b"\x30\x80\xc0" * w for _ in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


def _pdf_text_and_images(path):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open(str(path))
    return "".join(p.get_text() for p in doc), sum(len(p.get_images()) for p in doc)


def test_renders_chinese_report_with_mindmap(tmp_path):
    png = _png(tmp_path / "mind map report.png")  # a space in the path must not break the link
    out = tmp_path / "report.pdf"
    pdf_export.markdown_to_pdf(REPORT_ZH, str(out), mindmap_image_path=str(png))

    assert out.read_bytes()[:5] == b"%PDF-"
    assert not (tmp_path / "report.pdf.tmp").exists()
    text, images = _pdf_text_and_images(out)
    assert "课后总结报告" in text and "562 字/分钟" in text
    assert images == 1


def test_missing_mindmap_drops_the_image(tmp_path):
    out = tmp_path / "report.pdf"
    pdf_export.markdown_to_pdf(REPORT_ZH, str(out), mindmap_image_path=str(tmp_path / "absent.png"))
    text, images = _pdf_text_and_images(out)
    assert "课后总结报告" in text
    assert images == 0


def test_other_resources_are_not_fetched(tmp_path):
    secret = tmp_path / "secret.png"
    _png(secret)
    hostile = REPORT_ZH + (
        f'\n<img src="{secret.resolve().as_uri()}">\n\n'
        '<img src="http://169.254.169.254/latest/meta-data">\n\n'
        f"![x]({secret.resolve().as_uri()})\n"
    )
    out = tmp_path / "report.pdf"
    mindmap = _png(tmp_path / "mindmap_report.png")
    pdf_export.markdown_to_pdf(hostile, str(out), mindmap_image_path=str(mindmap))
    _, images = _pdf_text_and_images(out)
    assert images == 1  # the mind map only; the injected <img>/![]() are refused


def test_url_fetcher_allows_only_the_mindmap(tmp_path):
    png = _png(tmp_path / "mindmap_report.png")
    fetcher = pdf_export._make_url_fetcher(str(png))
    assert fetcher.fetch(png.resolve().as_uri()).read() == png.read_bytes()
    for url in ["file:///etc/passwd", "http://example.com/a.png", "data:image/png;base64,AAAA"]:
        with pytest.raises(ValueError):
            fetcher.fetch(url)


def test_font_order_follows_report_language():
    english = """# Post-Class Summary Report

Riverside High School 河畔中学 Grade 3
""" + "Teacher asked questions. " * 20
    assert pdf_export._is_chinese(REPORT_ZH)
    assert not pdf_export._is_chinese(english)
    assert not pdf_export._is_chinese("")
