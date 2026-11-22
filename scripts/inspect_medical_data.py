#!/usr/bin/env python3
"""Read-only inspection of the 2026-06-01 Medical Information Net CSV files.

The script reads source CSVs and the definition workbook, then prints a report
to stdout. It does not extract archives, modify source data, or write files.
Only the Python standard library is used.

Usage:
    python scripts/inspect_medical_data.py
    python scripts/inspect_medical_data.py --data-dir ./data
"""

from __future__ import annotations

import argparse
import collections
import csv
import pathlib
import re
import sys
import zipfile
import xml.etree.ElementTree as ET


FILES = {
    "hospital_facility": "01-1_hospital_facility_info_20260601.csv",
    "hospital_hours": "01-2_hospital_speciality_hours_20260601.csv",
    "clinic_facility": "02-1_clinic_facility_info_20260601.csv",
    "clinic_hours": "02-2_clinic_speciality_hours_20260601.csv",
}
DEFINITION_FILE = "001306376.xlsx"
DAYS = ("月", "火", "水", "木", "金", "土", "日", "祝")
KAWASAKI_CODES = {f"{n:03d}" for n in range(131, 138)}
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def csv_headers_and_count(path: pathlib.Path) -> tuple[list[str], int]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        headers = next(reader, [])
        rows = sum(1 for _ in reader)
    return headers, rows


def inspect_kawasaki(path: pathlib.Path) -> tuple[int, collections.Counter, collections.Counter, set[str]]:
    count = 0
    codes: collections.Counter[str] = collections.Counter()
    wards: collections.Counter[str] = collections.Counter()
    ids: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            code = (row.get("市区町村コード") or "").strip()
            address = row.get("所在地") or ""
            is_kawasaki = row.get("都道府県コード") == "14" and (
                code in KAWASAKI_CODES or "川崎市" in address
            )
            if not is_kawasaki:
                continue
            count += 1
            codes[code] += 1
            ids.add(row.get("ID", ""))
            for ward in ("川崎区", "幸区", "中原区", "高津区", "多摩区", "宮前区", "麻生区"):
                if ward in address:
                    wards[ward] += 1
    return count, codes, wards, ids


def inspect_hours(path: pathlib.Path, facility_ids: set[str]):
    rows = 0
    ids: set[str] = set()
    id_counts: collections.Counter[str] = collections.Counter()
    categories: collections.Counter[str] = collections.Counter()
    specialties: collections.Counter[str] = collections.Counter()
    pairs: collections.Counter[tuple[bool, bool]] = collections.Counter()
    frequent_pairs: collections.Counter[tuple[str, str]] = collections.Counter()
    odd_values: list[tuple[str, str, str, str]] = []
    one_sided = 0
    midnight_values = 0
    overnight_examples: list[tuple[str, str, str, str, str]] = []
    kawasaki_rows = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            rows += 1
            facility_id = row.get("ID", "")
            ids.add(facility_id)
            id_counts[facility_id] += 1
            if facility_id in facility_ids:
                kawasaki_rows += 1
            categories[row.get("診療時間帯", "")] += 1
            specialties[row.get("診療科目名", "")] += 1
            for day in DAYS:
                for kind in ("診療", "外来受付"):
                    start = row.get(f"{day}_{kind}開始時間", "") or ""
                    end = row.get(f"{day}_{kind}終了時間", "") or ""
                    pairs[(bool(start), bool(end))] += 1
                    if bool(start) != bool(end):
                        one_sided += 1
                    for value in (start, end):
                        if not value:
                            continue
                        if value == "00:00":
                            midnight_values += 1
                        if not re.fullmatch(r"\d{2}:\d{2}", value):
                            if len(odd_values) < 10:
                                odd_values.append((facility_id, day, kind, value))
                            continue
                        hour, minute = map(int, value.split(":"))
                        if hour > 23 or minute > 59:
                            if len(odd_values) < 10:
                                odd_values.append((facility_id, day, kind, value))
                    if start and end:
                        frequent_pairs[(start, end)] += 1
                        try:
                            sh, sm = map(int, start.split(":"))
                            eh, em = map(int, end.split(":"))
                            if eh * 60 + em < sh * 60 + sm and len(overnight_examples) < 10:
                                overnight_examples.append((facility_id, row.get("診療科目名", ""), day, start, end))
                        except ValueError:
                            pass
    return {
        "rows": rows,
        "unique_ids": len(ids),
        "duplicate_id_rows": rows - len(ids),
        "ids_without_facility": len(ids - facility_ids),
        "facility_ids_without_hours": len(facility_ids - ids),
        "categories": categories,
        "specialty_count": len(specialties),
        "top_specialties": specialties.most_common(10),
        "pair_presence": pairs,
        "one_sided_pairs": one_sided,
        "midnight_values": midnight_values,
        "invalid_time_examples": odd_values,
        "overnight_examples": overnight_examples,
        "common_time_pairs": frequent_pairs.most_common(10),
        "kawasaki_rows": kawasaki_rows,
    }


def inspect_workbook(path: pathlib.Path):
    """Read workbook sheet names and declared column names without extraction."""
    with zipfile.ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in item.findall(".//m:t", NS))
                      for item in root.findall("m:si", NS)]
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheets = []
        for sheet in workbook.findall(".//m:sheet", NS):
            sheets.append((sheet.attrib.get("name", ""), sheet.attrib.get(f"{{{NS['r']}}}id", "")))
        rel_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_ns = {"p": "http://schemas.openxmlformats.org/package/2006/relationships"}
        targets = {rel.attrib["Id"]: rel.attrib["Target"]
                   for rel in rel_root.findall("p:Relationship", rel_ns)}
        declared = {}
        for name, rel_id in sheets:
            if "施設票" not in name and "診療科・診療時間票" not in name:
                continue
            target = targets.get(rel_id, "")
            sheet_path = target.lstrip("/")
            if not sheet_path.startswith("xl/"):
                sheet_path = "xl/" + sheet_path
            root = ET.fromstring(archive.read(sheet_path))
            columns = []
            for row in root.findall(".//m:sheetData/m:row", NS):
                row_num = int(row.attrib.get("r", "0"))
                if row_num < 5:
                    continue
                for cell in row.findall("m:c", NS):
                    ref = cell.attrib.get("r", "")
                    if not ref.startswith("B"):
                        continue
                    value = cell.find("m:v", NS)
                    text = value.text if value is not None else ""
                    if cell.attrib.get("t") == "s" and text:
                        text = shared[int(text)]
                    inline = cell.find("m:is/m:t", NS)
                    if inline is not None:
                        text = inline.text or ""
                    # Remove phonetic guides appended in some workbook cells.
                    text = re.sub(r"[ァ-ヶー]+$", "", text).strip()
                    if text and text != "オープンデータ項目名":
                        columns.append(text)
            declared[name] = columns
    return [name for name, _ in sheets], declared


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=pathlib.Path, default=pathlib.Path("data"),
                        help="directory containing the source CSVs and definition workbook")
    args = parser.parse_args()
    data_dir = args.data_dir

    expected = list(FILES.values()) + [DEFINITION_FILE]
    missing = [name for name in expected if not (data_dir / name).is_file()]
    if missing:
        print(f"Missing files under {data_dir}:", file=sys.stderr)
        for name in missing:
            print(f"  {name}", file=sys.stderr)
        return 2

    print("== Files, encoding, rows, columns ==")
    csv_meta = {}
    for key, filename in FILES.items():
        path = data_dir / filename
        headers, row_count = csv_headers_and_count(path)
        csv_meta[key] = headers
        bom = path.open("rb").read(3) == b"\xef\xbb\xbf"
        print(f"{filename}: rows={row_count}, columns={len(headers)}, UTF-8 BOM={bom}")
        print("  " + ", ".join(headers))
    print(f"{DEFINITION_FILE}: Excel workbook")

    sheets, declared = inspect_workbook(data_dir / DEFINITION_FILE)
    print("\n== Definition workbook ==")
    print("Sheets: " + ", ".join(sheets))
    for sheet, columns in declared.items():
        print(f"{sheet}: {len(columns)} declared items")
        print("  " + ", ".join(columns))

    print("\n== Kawasaki facility counts and ward labels ==")
    facility_results = {}
    for kind in ("hospital", "clinic"):
        key = f"{kind}_facility"
        count, codes, wards, ids = inspect_kawasaki(data_dir / FILES[key])
        facility_results[kind] = ids
        print(f"{kind}: {count} facilities; municipality codes={dict(sorted(codes.items()))}")
        print(f"  ward counts from address text={dict(wards)}")

    print("\n== ID joins and hours structure ==")
    for kind in ("hospital", "clinic"):
        result = inspect_hours(data_dir / FILES[f"{kind}_hours"], facility_results[kind])
        print(f"{kind}: rows={result['rows']}, distinct facility IDs={result['unique_ids']}, "
              f"duplicate ID rows={result['duplicate_id_rows']}")
        print(f"  IDs without facility={result['ids_without_facility']}; "
              f"facilities without hours={result['facility_ids_without_hours']}; "
              f"Kawasaki hours rows={result['kawasaki_rows']}")
        print(f"  time-band values={dict(result['categories'])}; "
              f"distinct specialty names={result['specialty_count']}")
        print(f"  time pair presence (start-filled, end-filled)={dict(result['pair_presence'])}; "
              f"one-sided pairs={result['one_sided_pairs']}")
        print(f"  00:00 values={result['midnight_values']}; "
              f"invalid time examples={result['invalid_time_examples']}")
        print(f"  overnight examples (end earlier than start)={result['overnight_examples']}")
        print(f"  common start/end pairs={result['common_time_pairs']}")
        print(f"  top specialty names={result['top_specialties']}")

    print("\nNo source files were written or modified. ZIP archives are not expected by this script;")
    print("it reports only the CSVs and definition workbook present in --data-dir.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
