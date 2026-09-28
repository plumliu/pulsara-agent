# /// script
# requires-python = ">=3.10"
# dependencies = ["xlsxwriter>=3.2,<4"]
# ///
"""Demonstrate XLSX cell types, formula caches, a table, and an editable chart."""

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
            integer = wb.add_format({"num_format": "0"})
            money = wb.add_format({"num_format": "#,##0.00"})
            date = wb.add_format({"num_format": "yyyy-mm-dd"})

            ws.write_string("A1", "Cell types, formulas, table and chart · demonstration")
            ws.write_string(
                "A2", "Sample data · amounts in CNY · edit quantity or unit price"
            )
            ws.set_column("A:A", 17, date)
            ws.set_column("B:B", 14)
            ws.set_column("C:C", 12, integer)
            ws.set_column("D:E", 16, money)
            ws.set_column("F:F", 3)

            # Fictional inputs. These calculations supply caches, not an engine run.
            rows = [
                (datetime(2026, 1, 1), "00123", 12, 25.0),
                (datetime(2026, 2, 1), "00124", 8, 30.0),
                (datetime(2026, 3, 1), "00125", 15, 20.0),
            ]
            for row, (day, code, quantity, price) in enumerate(rows, start=4):
                excel_row = row + 1
                ws.write_datetime(row, 0, day, date)
                ws.write_string(row, 1, code)
                ws.write_number(row, 2, quantity, integer)
                ws.write_number(row, 3, price, money)
                ws.write_formula(
                    row, 4, f"=C{excel_row}*D{excel_row}", money, quantity * price
                )
            ws.add_table(
                "A4:E7",
                {
                    "name": "OrdersData",
                    "columns": [
                        {"header": name}
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
            ws.write_string("D9", "Total (CNY)")
            ws.write_formula(
                "E9", "=SUM(E5:E7)", money, sum(q * p for _, _, q, p in rows)
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
                }
            )
            chart.set_title({"name": "Amount by month"})
            chart.set_x_axis(
                {"name": "Month", "date_axis": True, "num_format": "mmm yyyy"}
            )
            chart.set_y_axis({"name": "CNY", "min": 0, "num_format": "#,##0"})
            chart.set_legend({"none": True})
            ws.insert_chart("G4", chart)
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
