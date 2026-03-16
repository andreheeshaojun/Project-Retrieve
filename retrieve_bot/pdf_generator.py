"""Generate clean PDFs from Substack HTML articles and YouTube transcripts."""

from pathlib import Path

from bs4 import BeautifulSoup
from fpdf import FPDF


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


def _html_to_paragraphs(html: str) -> list[str]:
    """Convert HTML to a list of plain-text paragraphs."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "svg"]):
        tag.decompose()

    paragraphs: list[str] = []
    block_tags = ["h1", "h2", "h3", "h4", "p", "li", "blockquote", "div"]
    for el in soup.find_all(block_tags):
        txt = el.get_text(strip=True)
        if not txt:
            continue
        tag = el.name
        if tag in ("h1", "h2"):
            paragraphs.append(f"## {txt}")
        elif tag in ("h3", "h4"):
            paragraphs.append(f"### {txt}")
        elif tag == "blockquote":
            paragraphs.append(f"> {txt}")
        elif tag == "li":
            paragraphs.append(f"  - {txt}")
        else:
            paragraphs.append(txt)

    if not paragraphs:
        paragraphs = [
            line.strip()
            for line in soup.get_text(separator="\n").split("\n")
            if line.strip()
        ]
    return paragraphs


def _write_body(pdf: _RetrievePDF, paragraphs: list[str]):
    """Render a list of plain-text paragraphs into the PDF body."""
    for para in paragraphs:
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
    _write_body(pdf, _html_to_paragraphs(html_content))

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
