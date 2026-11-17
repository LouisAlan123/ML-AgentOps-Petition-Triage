"""Generate the demo corpus PDFs from corpus/source/*.txt (text-layer PDFs, like a real university publishes).
Usage: python -m app.corpus.synth"""
from __future__ import annotations
import os, re, sys
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer


def build(src: str, dst: str) -> None:
    lines = [l.rstrip("\n") for l in open(src, encoding="utf-8") if l.strip()]
    title = lines[0].lstrip("# ").strip()
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=10, leading=14, spaceAfter=7)
    chap = ParagraphStyle("c", parent=ss["Heading2"], fontSize=12, spaceBefore=12, spaceAfter=8)
    head = " | ".join(title.split(" | ")[:2])

    def deco(canvas, doc):
        canvas.saveState(); canvas.setFont("Helvetica", 8)
        canvas.drawString(50, A4[1] - 30, head)
        canvas.drawString(A4[0] / 2 - 15, 25, f"Page {doc.page}")
        canvas.restoreState()

    story = [Paragraph(title.split(" | ")[0] + "<br/>" + title.split(" | ")[1], ss["Title"]), Spacer(1, 12)]
    for l in lines[1:]:
        if l.startswith("CHAPTER"):
            story.append(Paragraph(l.replace("&", "&amp;"), chap))
        else:
            story.append(Paragraph(l.replace("&", "&amp;"), body))
    SimpleDocTemplate(dst, pagesize=A4, title=head, author="synthetic", topMargin=55, bottomMargin=50).build(
        story, onFirstPage=deco, onLaterPages=deco)


if __name__ == "__main__":
    base = os.environ.get("CORPUS_DIR", "corpus")
    os.makedirs(f"{base}/raw", exist_ok=True)
    build(f"{base}/source/regulations.txt", f"{base}/raw/academic_regulations_2024.pdf")
    build(f"{base}/source/bye_laws.txt", f"{base}/raw/examination_bye_laws_2024.pdf")
    print("generated", os.listdir(f"{base}/raw"))
