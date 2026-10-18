from __future__ import annotations

import argparse
import json
from pathlib import Path


def row_text(row: dict) -> str:
    cells = []
    for cell in row["cells"]:
        text = f'{cell["cell"]}={cell["value"]}'
        if "cached" in cell:
            text += f' [cached={cell["cached"]}]'
        cells.append(text)
    return " | ".join(cells)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--sheet")
    parser.add_argument("--pdf")
    parser.add_argument("--heads", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.path.read_text(encoding="utf-8"))
    if args.sheet:
        sheet = next(
            item for item in payload["workbook"]["sheets"] if item["name"] == args.sheet
        )
        print(f'## {sheet["name"]} {sheet["dimensions"]}')
        for row in sheet["nonempty_rows"]:
            print(f'{row["row"]}: {row_text(row)}')
        return
    if args.pdf:
        pdf = next(item for item in payload["pdfs"] if args.pdf in item["path"])
        print(f'## {pdf["path"]} ({pdf["page_count"]} pages)')
        for page in pdf["pages"]:
            print(f'\n### PAGE {page["page"]}\n{page["text"]}')
        return
    if args.heads:
        for sheet in payload["workbook"]["sheets"]:
            print(f'\n## {sheet["name"]} {sheet["dimensions"]}')
            for row in sheet["nonempty_rows"][:5]:
                print(f'{row["row"]}: {row_text(row)}')


if __name__ == "__main__":
    main()
