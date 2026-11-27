# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Markdown to PDF for class reports (WeasyPrint).

The Linux app image has no LibreOffice, so the PDF is rendered from the same
report markdown the UI shows, not converted from the .docx. Its layout and
styling therefore differ from the Word template; the UI says so next to the
PDF option.

WeasyPrint needs Pango at runtime (docker/system_requirements.txt), a CJK font
(fonts-wqy-microhei) for Chinese reports and a Latin font (fonts-dejavu-core).
"""

import logging
import os
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)
# WeasyPrint logs every layout step at INFO; keep the app log to warnings.
logging.getLogger("weasyprint.progress").setLevel(logging.WARNING)

# The report markdown embeds the mind map as the API URL the UI loads it from.
_MINDMAP_SRC_RE = re.compile(r"\]\(/report/[^)\s]+/mindmap-image\)")

# Han, CJK punctuation and full-width forms.
_CJK_RANGES = ((0x3000, 0x303F), (0x3400, 0x9FFF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF))
_CJK_RE = re.compile("[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _CJK_RANGES) + "]")
_LATIN_RE = re.compile(r"[A-Za-z]")

# Font order follows the report language: shared punctuation such as curly
# quotes takes the first font, and the CJK font draws it full-width.
_FONTS_ZH = '"WenQuanYi Micro Hei", "Noto Sans CJK SC", "DejaVu Sans", sans-serif'
_FONTS_EN = '"DejaVu Sans", "WenQuanYi Micro Hei", "Noto Sans CJK SC", sans-serif'

_CSS = """
@page {
  size: A4;
  margin: 20mm 18mm 20mm 18mm;
  @bottom-center { content: counter(page) " / " counter(pages); font-size: 9pt; color: #888; }
}
body {
  font-size: 10.5pt;
  line-height: 1.6;
  color: #222;
}
h1:first-child {
  text-align: center;
  font-size: 20pt;
  color: #1f3864;
  border-bottom: 1px solid #4472c4;
  padding-bottom: 6pt;
  margin: 0 0 12pt 0;
}
h1, h2, h3 { color: #2f5496; margin: 14pt 0 4pt 0; page-break-after: avoid; }
h1 { font-size: 14pt; }
h2 { font-size: 12.5pt; }
h3 { font-size: 11pt; }
p { margin: 3pt 0; }
ol, ul { margin: 3pt 0; padding-left: 18pt; }
img { max-width: 100%; page-break-inside: avoid; }
table { border-collapse: collapse; margin: 6pt 0; }
th, td { border: 1px solid #999; padding: 3pt 6pt; }
th { background: #eef2f8; }
"""


@lru_cache(maxsize=1)
def pdf_export_available() -> bool:
    """True when WeasyPrint and its Pango libraries load."""
    try:
        import weasyprint  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - ImportError, or OSError when Pango is missing
        logger.info("PDF export unavailable: %s", exc)
        return False
    return True


def _is_chinese(text: str) -> bool:
    # One Han character carries roughly a word; an English report with a typed
    # Chinese school name stays English.
    cjk = len(_CJK_RE.findall(text))
    return cjk > 0 and cjk * 3 >= len(_LATIN_RE.findall(text))


def _font_css(fonts: str) -> str:
    # Page-margin boxes (the page number) do not inherit from body.
    return (
        f"body {{ font-family: {fonts}; }}\n"
        f"@page {{ @bottom-center {{ font-family: {fonts}; }} }}\n"
    )


def _report_html(markdown_text: str, mindmap_uri: str | None, lang: str) -> str:
    import markdown

    if mindmap_uri:
        markdown_text = _MINDMAP_SRC_RE.sub(f"]({mindmap_uri})", markdown_text)
    else:
        # No uploaded image: drop the reference instead of a broken image.
        markdown_text = re.sub(r"!\[[^\]]*\]\(/report/[^)\s]+/mindmap-image\)", "", markdown_text)
    body = markdown.markdown(markdown_text, extensions=["tables"])
    return f'<!DOCTYPE html><html lang="{lang}"><head><meta charset="utf-8"></head><body>{body}</body></html>'


def _make_url_fetcher(mindmap_image_path: str | None):
    """A WeasyPrint URL fetcher that serves only the session's mind-map PNG.

    The markdown carries LLM output and teacher-typed fields, and Python-Markdown
    passes raw HTML through, so any other URL (http, file, data) is refused
    rather than fetched. WeasyPrint catches the refusal, logs a warning and
    leaves the resource out.
    """
    from weasyprint.urls import URLFetcher, URLFetcherResponse

    allowed = Path(mindmap_image_path).resolve().as_uri() if mindmap_image_path else None

    class _MindmapOnlyFetcher(URLFetcher):
        def fetch(self, url, headers=None):
            if allowed and url == allowed:
                with open(mindmap_image_path, "rb") as f:
                    return URLFetcherResponse(url, f.read(), {"Content-Type": "image/png"})
            raise ValueError(f"blocked resource in report PDF: {url[:80]}")

    return _MindmapOnlyFetcher()


def markdown_to_pdf(markdown_text: str, output_path: str, mindmap_image_path: str | None = None) -> str:
    """Render report markdown to ``output_path`` (written atomically). Returns the path."""
    import weasyprint

    if not (mindmap_image_path and os.path.exists(mindmap_image_path)):
        mindmap_image_path = None
    # A percent-encoded file URI, so a storage path with spaces stays one link.
    mindmap_uri = Path(mindmap_image_path).resolve().as_uri() if mindmap_image_path else None
    chinese = _is_chinese(markdown_text)
    html = _report_html(markdown_text, mindmap_uri, "zh-CN" if chinese else "en")
    css = _CSS + _font_css(_FONTS_ZH if chinese else _FONTS_EN)

    tmp_path = f"{output_path}.tmp"
    weasyprint.HTML(
        string=html,
        url_fetcher=_make_url_fetcher(mindmap_image_path),
    ).write_pdf(tmp_path, stylesheets=[weasyprint.CSS(string=css)])
    os.replace(tmp_path, output_path)
    return output_path
