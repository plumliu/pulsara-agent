# /// script
# requires-python = ">=3.10"
# dependencies = ["xlsxwriter>=3.2,<4"]
# ///
"""Create an editable sample workbook; adapt the sample data for real tasks."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import xlsxwriter


def create(output: Path) -> None:
    if output.suffix.lower() != ".xlsx":
        raise ValueError("This example creates ordinary .xlsx files")
    # Exclusive creation keeps an existing user file intact.
    with output.open("xb") as stream:
        with xlsxwriter.Workbook(
            stream, {"strings_to_formulas": False, "strings_to_urls": False}
        ) as wb:
            ws = wb.add_worksheet("Orders")
            font = {"font_name": "Arial", "font_size": 11}
            title = wb.add_format({**font, "bold": True, "font_size": 18})
            note = wb.add_format({**font, "font_color": "#666666"})
            text = wb.add_format({**font, "indent": 1})
            header = wb.add_format(
                {
                    **font,
                    "bold": True,
                    "bg_color": "#E8EEF4",
                    "font_color": "#243B50",
                    "valign": "vcenter",
                    "indent": 1,
                }
            )
            integer = wb.add_format({**font, "num_format": "0"})
            money = wb.add_format({**font, "num_format": "#,##0.00"})
            date = wb.add_format({**font, "num_format": "yyyy-mm-dd", "align": "left"})
            total = wb.add_format({**font, "bold": True, "num_format": "#,##0.00"})

            ws.write_string("A1", "Orders · demonstration", title)
            ws.write_string(
                "A2", "Sample data · amounts in CNY · edit quantity or unit price", note
            )
            ws.set_column("A:A", 17, date)
            ws.set_column("B:B", 14, text)
            ws.set_column("C:C", 12, integer)
            ws.set_column("D:E", 16, money)
            ws.set_column("F:F", 3)
            ws.set_column("G:O", 10)
            ws.set_row(0, 30)
            ws.set_row(3, 25)

            # Fictional inputs. These calculations supply caches, not an engine run.
            rows = [
                (datetime(2026, 1, 1), "00123", 12, 25.0),
                (datetime(2026, 2, 1), "00124", 8, 30.0),
                (datetime(2026, 3, 1), "00125", 15, 20.0),
            ]
            for row, (day, code, quantity, price) in enumerate(rows, start=4):
                excel_row = row + 1
                ws.write_datetime(row, 0, day, date)
                ws.write_string(row, 1, code, text)
                ws.write_number(row, 2, quantity, integer)
                ws.write_number(row, 3, price, money)
                ws.write_formula(
                    row, 4, f"=C{excel_row}*D{excel_row}", money, quantity * price
                )
                ws.set_row(row, 23)
            ws.add_table(
                "A4:E7",
                {
                    "name": "OrdersData",
                    "style": "Table Style Medium 2",
                    "columns": [
                        {"header": name, "header_format": header}
                        for name in (
                            "Month",
                            "Item code",
                            "Quantity",
                            "Unit price (CNY)",
                            "Amount (CNY)",
                        )
                    ],
                },
            )
            ws.write_string("D9", "Total (CNY)", total)
            ws.write_formula(
                "E9", "=SUM(E5:E7)", total, sum(q * p for _, _, q, p in rows)
            )
            ws.freeze_panes(4, 2)
            ws.data_validation(
                "C5:C7",
                {
                    "validate": "integer",
                    "criteria": ">=",
                    "value": 0,
                    "input_title": "Quantity",
                    "input_message": "Enter a nonnegative whole number.",
                },
            )

            chart = wb.add_chart({"type": "column"})
            chart.add_series(
                {
                    "name": ["Orders", 3, 4],
                    "categories": ["Orders", 4, 0, 6, 0],
                    "values": ["Orders", 4, 4, 6, 4],
                    "fill": {"color": "#476E91"},
                    "border": {"none": True},
                }
            )
            chart.set_title({"name": "Amount by month"})
            chart.set_x_axis(
                {"name": "Month", "date_axis": True, "num_format": "mmm yyyy"}
            )
            chart.set_y_axis({"name": "CNY", "min": 0, "num_format": "#,##0"})
            chart.set_legend({"none": True})
            chart.set_size({"width": 600, "height": 320})
            ws.insert_chart("G4", chart)
            ws.hide_gridlines(2)
            ws.set_landscape()
            ws.set_paper(9)
            ws.fit_to_pages(1, 0)
            ws.print_area("A1:O21")
            ws.repeat_rows(3)
    print(f"Created {output}; supplied sample caches, no calculation engine run.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    create(parser.parse_args().output)


if __name__ == "__main__":
    main()
