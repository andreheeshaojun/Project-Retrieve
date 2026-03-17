"""Generate clean PDFs from Substack articles, YouTube transcripts, and website articles."""

import io
import logging
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from fpdf import FPDF
from PIL import Image

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


class _RetrievePDF(FPDF):
    """Base PDF with header/footer branding."""

    doc_title: str = ""

    def header(self):
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(160, 160, 160)
        self.cell(0, 8, self.doc_title[:80], align="R")
        self.ln(12)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(160, 160, 160)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")


# ------------------------------------------------------------------ helpers


def _sanitize(text: str) -> str:
    """Replace unicode characters that latin-1 cannot encode."""
    if not text:
        return ""
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "--",
        "\u2026": "...",
        "\u00a0": " ",
        "\u200b": "",
        "\u2022": "-",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _html_to_elements(html: str) -> list[dict]:
    """Convert HTML to an ordered list of text paragraphs and image references."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "svg"]):
        tag.decompose()

    elements: list[dict] = []
    seen_imgs: set[str] = set()

    tags = ["h1", "h2", "h3", "h4", "p", "li", "blockquote", "div", "figure", "img"]
    for el in soup.find_all(tags):
        if el.name == "img":
            src = el.get("src", "")
            if src and src not in seen_imgs and not src.startswith("data:"):
                seen_imgs.add(src)
                elements.append({"type": "image", "src": src})
            continue

        if el.name == "figure":
            img = el.find("img")
            if img:
                src = img.get("src", "")
                if src and src not in seen_imgs and not src.startswith("data:"):
                    seen_imgs.add(src)
                    elements.append({"type": "image", "src": src})
            cap = el.find("figcaption")
            if cap and cap.get_text(strip=True):
                elements.append({"type": "text", "content": cap.get_text(strip=True)})
            continue

        for img in el.find_all("img", recursive=False):
            src = img.get("src", "")
            if src and src not in seen_imgs and not src.startswith("data:"):
                seen_imgs.add(src)
                elements.append({"type": "image", "src": src})

        txt = el.get_text(strip=True)
        if not txt:
            continue
        tag = el.name
        if tag in ("h1", "h2"):
            elements.append({"type": "text", "content": f"## {txt}"})
        elif tag in ("h3", "h4"):
            elements.append({"type": "text", "content": f"### {txt}"})
        elif tag == "blockquote":
            elements.append({"type": "text", "content": f"> {txt}"})
        elif tag == "li":
            elements.append({"type": "text", "content": f"  - {txt}"})
        else:
            elements.append({"type": "text", "content": txt})

    if not elements:
        elements = [
            {"type": "text", "content": line.strip()}
            for line in soup.get_text(separator="\n").split("\n")
            if line.strip()
        ]
    return elements


def _fetch_image(src: str, max_width: int = 1200) -> io.BytesIO | None:
    """Download an image, convert to compressed JPEG, return as BytesIO."""
    try:
        resp = requests.get(src, timeout=15, headers=_HEADERS)
        resp.raise_for_status()
        if len(resp.content) > 10_000_000:
            return None
        img = Image.open(io.BytesIO(resp.content))
        if img.width < 50 or img.height < 50:
            return None
        img = img.convert("RGB")
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75, optimize=True)
        buf.seek(0)
        return buf
    except Exception:
        return None


def _write_body(pdf: _RetrievePDF, elements: list[dict]):
    """Render text paragraphs and images into the PDF body."""
    for el in elements:
        if el["type"] == "image":
            img_data = _fetch_image(el["src"])
            if img_data:
                usable_w = pdf.w - pdf.l_margin - pdf.r_margin
                try:
                    pdf.image(img_data, w=usable_w)
                except Exception:
                    logger.debug("Could not embed image: %s", el["src"][:80])
                pdf.ln(4)
            continue

        para = el.get("content", "")
        if not para:
            pdf.ln(4)
            continue

        if para.startswith("## "):
            pdf.set_font("Helvetica", "B", 14)
            pdf.ln(4)
            pdf.multi_cell(0, 8, _sanitize(para[3:]))
            pdf.set_font("Helvetica", "", 11)
        elif para.startswith("### "):
            pdf.set_font("Helvetica", "B", 12)
            pdf.ln(3)
            pdf.multi_cell(0, 7, _sanitize(para[4:]))
            pdf.set_font("Helvetica", "", 11)
        elif para.startswith("> "):
            pdf.set_font("Helvetica", "I", 11)
            pdf.set_text_color(90, 90, 90)
            pdf.multi_cell(0, 6, _sanitize(para[2:]))
            pdf.set_font("Helvetica", "", 11)
            pdf.set_text_color(40, 40, 40)
        else:
            pdf.multi_cell(0, 6, _sanitize(para))
        pdf.ln(2)


def _init_pdf(title: str) -> _RetrievePDF:
    pdf = _RetrievePDF()
    pdf.doc_title = _sanitize(title[:80])
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=20)
    return pdf


def _write_meta(pdf: _RetrievePDF, parts: list[str]):
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(130, 130, 130)
    pdf.multi_cell(0, 5, _sanitize(" | ".join(parts)))
    pdf.ln(2)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(8)
    pdf.set_text_color(40, 40, 40)


# ------------------------------------------------------------------ public


def generate_substack_pdf(
    title: str,
    subtitle: str,
    author: str,
    date: str,
    html_content: str,
    url: str,
    output_path: Path,
) -> Path:
    """Render a Substack article to a PDF file."""
    pdf = _init_pdf(title)

    # title
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(30, 30, 30)
    pdf.multi_cell(0, 10, _sanitize(title))
    pdf.ln(3)

    # subtitle
    if subtitle:
        pdf.set_font("Helvetica", "I", 12)
        pdf.set_text_color(100, 100, 100)
        pdf.multi_cell(0, 7, _sanitize(subtitle))
        pdf.ln(3)

    # meta
    meta = [p for p in [f"By {author}" if author else "", date[:10], url] if p]
    _write_meta(pdf, meta)

    # body
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(40, 40, 40)
    _write_body(pdf, _html_to_elements(html_content))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output_path))
    return output_path


def generate_youtube_pdf(
    title: str,
    channel: str,
    date: str,
    transcript: str,
    url: str,
    output_path: Path,
) -> Path:
    """Render a YouTube transcript to a PDF file."""
    pdf = _init_pdf(title)

    # title
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(30, 30, 30)
    pdf.multi_cell(0, 10, _sanitize(title))
    pdf.ln(3)

    # channel
    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 7, _sanitize(f"Channel: {channel}"))
    pdf.ln(5)

    # meta
    meta = [date[:10] if date else "Unknown date", url]
    _write_meta(pdf, meta)

    # transcript label
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(40, 40, 40)
    pdf.cell(0, 8, "Transcript")
    pdf.ln(8)

    # transcript body
    pdf.set_font("Helvetica", "", 11)
    if transcript:
        for line in transcript.split("\n"):
            line = line.strip()
            if not line:
                pdf.ln(3)
                continue
            pdf.multi_cell(0, 6, _sanitize(line))
            pdf.ln(2)
    else:
        pdf.set_font("Helvetica", "I", 11)
        pdf.set_text_color(150, 150, 150)
        pdf.multi_cell(0, 6, "No transcript available for this video.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output_path))
    return output_path


def generate_website_pdf(
    title: str,
    author: str,
    date: str,
    text_content: str,
    url: str,
    source: str,
    output_path: Path,
) -> Path:
    """Render a generic website article to a PDF file."""
    pdf = _init_pdf(title)

    # title
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(30, 30, 30)
    pdf.multi_cell(0, 10, _sanitize(title))
    pdf.ln(3)

    # source
    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 7, _sanitize(f"Source: {source}"))
    pdf.ln(5)

    # meta
    meta_parts = []
    if author:
        meta_parts.append(f"By {author}")
    meta_parts.append(date[:10] if date else "Unknown date")
    meta_parts.append(url)
    _write_meta(pdf, meta_parts)

    # body
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(40, 40, 40)
    if text_content:
        paragraphs = [p.strip() for p in text_content.split("\n") if p.strip()]
        for para in paragraphs:
            pdf.multi_cell(0, 6, _sanitize(para))
            pdf.ln(2)
    else:
        pdf.set_font("Helvetica", "I", 11)
        pdf.set_text_color(150, 150, 150)
        pdf.multi_cell(0, 6, "No content could be extracted for this article.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output_path))
    return output_path
