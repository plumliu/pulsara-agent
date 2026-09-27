# /// script
# requires-python = ">=3.10"
# dependencies = ["python-docx>=1.2,<2"]
# ///
"""Small styled report with sample content; adapt it to the user's document."""
from __future__ import annotations

import argparse
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor


def create(output: Path, latin_font=None, cjk_font=None):
    if output.exists():
        raise FileExistsError(f"Choose a new output: {output}")
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    section.top_margin = section.bottom_margin = Mm(22)
    section.left_margin = section.right_margin = Mm(24)
    for name, size in (("Normal", 11), ("Title", 26), ("Heading 1", 17), ("Heading 2", 13)):
        style = doc.styles[name]
        style.font.size = Pt(size)
        if latin_font:
            style.font.name = latin_font
        if cjk_font:
            fonts = style.element.get_or_add_rPr().get_or_add_rFonts()
            fonts.set(qn("w:eastAsia"), cjk_font)
        style.font.color.rgb = RGBColor.from_string("202832")
    normal = doc.styles["Normal"].paragraph_format
    normal.space_after, normal.line_spacing = Pt(8), 1.25
    header = section.header.paragraphs[0]
    header.text = "PROJECT UPDATE"
    for run in header.runs:
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor.from_string("65717E")
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run("Page ")
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    field.set(qn("w:dirty"), "true")
    footer._p.append(field)

    doc.add_heading("项目进展报告", 0)
    doc.add_paragraph("阶段进展与后续安排")
    doc.add_heading("1. 本期概览", 1)
    doc.add_paragraph("本期已完成需求梳理，原型设计正在进行。")
    table = doc.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    table.autofit = False
    usable_width = section.page_width - section.left_margin - section.right_margin
    widths = [int(usable_width * ratio) for ratio in (0.24, 0.20)]
    widths.append(usable_width - sum(widths))
    for column, width in zip(table.columns, widths):
        column.width = width
    for cell, text in zip(table.rows[0].cells, ["工作项", "状态", "说明"]):
        cell.text = text
        for run in cell.paragraphs[0].runs:
            run.bold = True
    repeat = OxmlElement("w:tblHeader")
    table.rows[0]._tr.get_or_add_trPr().append(repeat)
    for row in [("需求梳理", "已完成", "已确认主要使用场景。"),
                ("原型设计", "进行中", "关键页面等待评审。")]:
        for cell, text in zip(table.add_row().cells, row):
            cell.text = text
    for row in table.rows:
        for cell, width in zip(row.cells, widths):
            cell.width = width
    doc.add_heading("2. 下一步", 1)
    for text in ["完成关键页面评审。", "确定下一阶段的实施顺序。"]:
        doc.add_paragraph(text, style="List Bullet")
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--latin-font")
    parser.add_argument("--cjk-font")
    args = parser.parse_args()
    print(create(args.output, args.latin_font, args.cjk_font).resolve())


if __name__ == "__main__":
    main()
