#!/usr/bin/env python3
"""Read-only detailed audit of Kawasaki City Medical Information Net data.

Reads the four 2026-06-01 CSV files in --data-dir and prints aggregate results
to stdout. It does not modify source data, create report files, or contact any
external service. IDs, codes, and times remain strings when read from CSV.

Usage:
    python scripts/detailed_kawasaki_audit.py
    python scripts/detailed_kawasaki_audit.py --data-dir ./data
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
    "hospital": {
        "facility": "01-1_hospital_facility_info_20260601.csv",
        "hours": "01-2_hospital_speciality_hours_20260601.csv",
    },
    "clinic": {
        "facility": "02-1_clinic_facility_info_20260601.csv",
        "hours": "02-2_clinic_speciality_hours_20260601.csv",
    },
}
DAYS = ("月", "火", "水", "木", "金", "土", "日", "祝")
WEEKDAYS = DAYS[:7]
WARDS = ("川崎区", "幸区", "中原区", "高津区", "多摩区", "宮前区", "麻生区")
KAWASAKI_MUNICIPAL_CODES = {f"{code:03d}" for code in range(131, 138)}
QUERY_DAYS = ("月", "土", "日", "祝")
QUERY_TIMES = ("09:00", "12:00", "18:00", "20:00", "21:00", "22:00", "23:00", "00:00", "01:00")
TARGET_SPECIALTIES = ("内科", "小児科", "外科", "整形外科", "耳鼻咽喉科", "皮膚科")
DEFINITION_FILE = "001306376.xlsx"
XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
           "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def csv_rows(path: pathlib.Path):
    """Open a CSV as UTF-8 with BOM support; values are kept as strings."""
    return path.open("r", encoding="utf-8-sig", newline="")


def clock_minutes(value: str) -> int | None:
    """Return minutes since midnight for strict HH:MM values in 00:00..23:59."""
    if not re.fullmatch(r"\d{2}:\d{2}", value or ""):
        return None
    hour, minute = map(int, value.split(":"))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def kawasaki_facilities(path: pathlib.Path):
    facilities = {}
    wards = {}
    flags = {}
    raw_week_flags = {}
    with csv_rows(path) as stream:
        for row in csv.DictReader(stream):
            code = (row.get("市区町村コード") or "").strip()
            address = row.get("所在地") or ""
            is_kawasaki = row.get("都道府県コード") == "14" and (
                code in KAWASAKI_MUNICIPAL_CODES or "川崎市" in address
            )
            if not is_kawasaki:
                continue
            facility_id = row["ID"]
            facilities[facility_id] = row
            wards[facility_id] = next((ward for ward in WARDS if ward in address), "区不明")
            flags[facility_id] = {
                day: (row.get("祝日に休診", "") if day == "祝" else
                      row.get(f"毎週決まった曜日に休診（{day}）", ""))
                for day in DAYS
            }
            raw_week_flags[facility_id] = {
                (week, day): row.get(f"決まった週に休診（定期週）第{week}週（{day}）", "")
                for week in range(1, 6) for day in WEEKDAYS
            }
    return facilities, wards, flags, raw_week_flags


def read_headers_and_count(path: pathlib.Path):
    with csv_rows(path) as stream:
        reader = csv.reader(stream)
        header = next(reader, [])
        count = sum(1 for _ in reader)
    return header, count


def inspect_definition(path: pathlib.Path):
    """Read sheet names and relevant definition text from the workbook in-place."""
    with zipfile.ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.text or "" for node in item.findall(".//m:t", XLSX_NS))
                      for item in root.findall("m:si", XLSX_NS)]
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        names = [sheet.attrib.get("name", "")
                 for sheet in workbook.findall(".//m:sheet", XLSX_NS)]
    evidence = sorted({text for text in shared
                       if any(term in text for term in ("0：休診", "1：診療", "時間帯１", "時間帯２", "時間帯３", "HH:MM"))})
    return names, evidence


def empty_kind_result():
    return {
        "by_facility_day": collections.defaultdict(lambda: collections.defaultdict(list)),
        "daily_facilities": {day: set() for day in DAYS},
        "daily_records": collections.Counter(),
        "query_facilities": collections.defaultdict(set),
        "query_records": collections.Counter(),
        "query_end_boundary_facilities": collections.defaultdict(set),
        "query_end_boundary_records": collections.Counter(),
        "city_crossing": [],
        "city_equal": [],
        "city_invalid": [],
        "registered_specialty_facilities": collections.defaultdict(set),
        "scheduled_specialty_facilities": collections.defaultdict(set),
        "specialty_sunday": collections.defaultdict(set),
        "specialty_holiday": collections.defaultdict(set),
        "specialty_sunday_queries": collections.defaultdict(set),
        "specialty_codes": collections.defaultdict(set),
        "specialty_rows": collections.Counter(),
        "name_code_facilities": collections.defaultdict(lambda: collections.defaultdict(set)),
        "code_names": collections.defaultdict(set),
        "night_record_counts": collections.Counter(),
        "night_facilities": collections.defaultdict(set),
        "night_multiband_pairs": collections.defaultdict(lambda: collections.defaultdict(set)),
        "national": {
            "start_values": [], "end_values": [],
            "missing_start": 0, "missing_end": 0,
            "invalid_values": 0, "one_sided_pairs": 0,
            "valid_pairs": 0, "equal_pairs": 0, "crossing_pairs": 0,
            "zero_start": 0, "zero_end": 0, "24_value_fields": 0,
            "full_day_pairs": 0, "night_20_24_records": 0,
            "night_00_06_records": 0,
        },
        "rows": 0,
        "time_band_counts": collections.Counter(),
        "id_counts": collections.Counter(),
    }


def audit_hours(path: pathlib.Path, facility_ids: set[str]):
    result = empty_kind_result()
    query_minutes = {time: clock_minutes(time) for time in QUERY_TIMES}
    with csv_rows(path) as stream:
        for row in csv.DictReader(stream):
            result["rows"] += 1
            facility_id = row.get("ID", "")
            result["id_counts"][facility_id] += 1
            result["time_band_counts"][row.get("診療時間帯", "")] += 1
            is_city = facility_id in facility_ids
            name = row.get("診療科目名", "")
            code = row.get("診療科目コード", "")
            band = row.get("診療時間帯", "")
            if is_city:
                result["registered_specialty_facilities"][name].add(facility_id)
                result["specialty_codes"][name].add(code)
                result["specialty_rows"][name] += 1
                result["name_code_facilities"][name][code].add(facility_id)
                result["code_names"][code].add(name)

            for day in DAYS:
                start = row.get(f"{day}_診療開始時間", "") or ""
                end = row.get(f"{day}_診療終了時間", "") or ""
                national = result["national"]

                if start:
                    national["start_values"].append(start)
                else:
                    national["missing_start"] += 1
                if end:
                    national["end_values"].append(end)
                else:
                    national["missing_end"] += 1
                if start == "00:00":
                    national["zero_start"] += 1
                if end == "00:00":
                    national["zero_end"] += 1
                if start == "24:00":
                    national["24_value_fields"] += 1
                if end == "24:00":
                    national["24_value_fields"] += 1

                start_min = clock_minutes(start)
                end_min = clock_minutes(end)
                if not start and not end:
                    continue
                if bool(start) != bool(end):
                    national["one_sided_pairs"] += 1
                    if start_min is None and start:
                        national["invalid_values"] += 1
                    if end_min is None and end:
                        national["invalid_values"] += 1
                    if is_city:
                        result["city_invalid"].append((facility_id, name, band, day, start, end))
                    continue
                if start_min is None or end_min is None:
                    national["invalid_values"] += 1
                    if is_city:
                        result["city_invalid"].append((facility_id, name, band, day, start, end))
                    continue

                national["valid_pairs"] += 1
                if start_min == end_min:
                    national["equal_pairs"] += 1
                if end_min < start_min:
                    national["crossing_pairs"] += 1
                    if is_city:
                        result["city_crossing"].append((facility_id, name, code, band, day, start, end))
                    continue
                if start_min == 0 and end_min == 1439:
                    national["full_day_pairs"] += 1
                if start_min < 1440 and end_min > 1200:
                    national["night_20_24_records"] += 1
                if start_min < 360 and end_min > 0:
                    national["night_00_06_records"] += 1

                if not is_city:
                    continue
                interval = (start_min, end_min, name, code, band, start, end)
                result["by_facility_day"][facility_id][day].append(interval)
                result["daily_facilities"][day].add(facility_id)
                result["daily_records"][day] += 1
                result["scheduled_specialty_facilities"][name].add(facility_id)
                if day == "日":
                    result["specialty_sunday"][name].add(facility_id)
                elif day == "祝":
                    result["specialty_holiday"][name].add(facility_id)

                if start_min < 1440 and end_min > 1200:
                    result["night_record_counts"]["20-24"] += 1
                    result["night_facilities"]["20-24"].add(facility_id)
                    result["night_multiband_pairs"]["20-24"][(facility_id, name)].add(band)
                if start_min < 360 and end_min > 0:
                    result["night_record_counts"]["00-06"] += 1
                    result["night_facilities"]["00-06"].add(facility_id)
                    result["night_multiband_pairs"]["00-06"][(facility_id, name)].add(band)

                if day in QUERY_DAYS:
                    for query_time, query_min in query_minutes.items():
                        if start_min <= query_min < end_min:
                            key = (day, query_time)
                            result["query_facilities"][key].add(facility_id)
                            result["query_records"][key] += 1
                            if day == "日":
                                result["specialty_sunday_queries"][(name, query_time)].add(facility_id)
                        if query_min == end_min:
                            key = (day, query_time)
                            result["query_end_boundary_facilities"][key].add(facility_id)
                            result["query_end_boundary_records"][key] += 1

    return result


def audit_city_equal_pairs(path: pathlib.Path, facility_ids: set[str]):
    counts = {"records": 0, "facilities": set()}
    examples = []
    with csv_rows(path) as stream:
        for row in csv.DictReader(stream):
            if row.get("ID") not in facility_ids:
                continue
            for day in DAYS:
                start = row.get(f"{day}_診療開始時間", "") or ""
                end = row.get(f"{day}_診療終了時間", "") or ""
                start_min, end_min = clock_minutes(start), clock_minutes(end)
                if start_min is not None and start_min == end_min:
                    counts["records"] += 1
                    counts["facilities"].add(row["ID"])
                    if len(examples) < 5:
                        examples.append((row["ID"], row.get("診療科目名", ""), day, start, end))
    return counts, examples


def print_day_tables(kind: str, result, wards):
    print(f"\n{kind}: weekday/holiday schedule registrations")
    print("Each cell = facilities with >=1 valid-format time pair / time-pair records")
    header = "曜日 | " + " | ".join(WARDS) + " | 市全体"
    print(header)
    for day in DAYS:
        cells = []
        total_facilities = set()
        total_records = 0
        for ward in WARDS:
            ids = [facility_id for facility_id, value in wards.items() if value == ward]
            matched = {facility_id for facility_id in ids
                       if result["by_facility_day"][facility_id][day]}
            record_count = sum(len(result["by_facility_day"][facility_id][day]) for facility_id in ids)
            total_facilities.update(matched)
            total_records += record_count
            cells.append(f"{len(matched)}/{record_count}")
        cells.append(f"{len(total_facilities)}/{total_records}")
        print(day + " | " + " | ".join(cells))


def flag_summary(facilities, flags, schedules):
    summary = {}
    for day in DAYS:
        flag_counts = collections.Counter()
        conflicts = collections.Counter()
        for facility_id in facilities:
            flag = flags[facility_id][day]
            has_time = bool(schedules["by_facility_day"][facility_id][day])
            flag_counts[flag] += 1
            if flag == "0" and has_time:
                conflicts["0_with_time"] += 1
            elif flag == "1" and not has_time:
                conflicts["1_without_time"] += 1
            elif flag == "" and has_time:
                conflicts["blank_with_time"] += 1
        summary[day] = (flag_counts, conflicts)
    return summary


def print_queries(result):
    print("\nTime-point query estimate; each entry = unique facilities / matching interval records")
    print("Assumption: start <= query < end. End-inclusive behavior is not specified in the definition.")
    for day in QUERY_DAYS:
        values = []
        for time in QUERY_TIMES:
            key = (day, time)
            values.append(f"{time} {len(result['query_facilities'][key])}/"
                          f"{result['query_records'][key]} "
                          f"(end-boundary records={result['query_end_boundary_records'][key]})")
        print(day + ": " + "; ".join(values))


def print_specialties(kind: str, result):
    print(f"\n{kind}: exact specialty-name counts")
    print("Specialty facilities count any row; schedule counts require a valid-format time pair.")
    print("科目 | 登録施設 | 時間登録施設 | 日曜施設 | 祝日施設 | 日曜18時 | 20時 | 21時 | コード")
    for name in TARGET_SPECIALTIES:
        registered = result["registered_specialty_facilities"][name]
        scheduled = result["scheduled_specialty_facilities"][name]
        sunday = result["specialty_sunday"][name]
        holiday = result["specialty_holiday"][name]
        query_counts = [len(result["specialty_sunday_queries"][(name, time)])
                        for time in ("18:00", "20:00", "21:00")]
        codes = ",".join(sorted(result["specialty_codes"][name]))
        print(f"{name} | {len(registered)} | {len(scheduled)} | {len(sunday)} | "
              f"{len(holiday)} | {query_counts[0]} | {query_counts[1]} | "
              f"{query_counts[2]} | {codes}")

    related = sorted(name for name in result["registered_specialty_facilities"]
                     if any(term in name for term in ("内科", "小児", "外科", "整形", "耳鼻", "皮膚")))
    print("Related exact names (name: registered facilities; scheduled facilities; codes):")
    for name in related:
        if name in TARGET_SPECIALTIES:
            continue
        codes = ",".join(sorted(result["specialty_codes"][name]))
        print(f"  {name}: {len(result['registered_specialty_facilities'][name])}; "
              f"{len(result['scheduled_specialty_facilities'][name])}; {codes}")


def national_range(values):
    parsed = [(clock_minutes(value), value) for value in values]
    parsed = [(minute, value) for minute, value in parsed if minute is not None]
    if not parsed:
        return "なし"
    return f"{min(parsed)[1]}–{max(parsed)[1]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=pathlib.Path, default=pathlib.Path("data"),
                        help="directory containing the four source CSVs")
    args = parser.parse_args()
    data_dir = args.data_dir

    expected = [entry[key] for entry in FILES.values() for key in ("facility", "hours")]
    expected.append(DEFINITION_FILE)
    missing = [name for name in expected if not (data_dir / name).is_file()]
    if missing:
        print(f"Missing required input files in {data_dir}:", file=sys.stderr)
        for name in missing:
            print(f"  {name}", file=sys.stderr)
        return 2

    print("Medical Information Net detailed Kawasaki audit (data date: 2026-06-01)")
    print("Read-only; source CSVs are not modified. IDs and codes are read as strings.")
    print("\n== Input CSV rows and columns ==")
    for kind, filenames in FILES.items():
        for category, filename in filenames.items():
            headers, count = read_headers_and_count(data_dir / filename)
            print(f"{filename}: {count} data rows, {len(headers)} columns")
    definition_sheets, definition_evidence = inspect_definition(data_dir / DEFINITION_FILE)
    print(f"\nDefinition workbook: {DEFINITION_FILE}")
    print("Sheets: " + ", ".join(definition_sheets))
    print("Relevant definition text found in workbook shared strings:")
    for text in definition_evidence:
        print(f"  {text}")

    loaded = {}
    for kind, filenames in FILES.items():
        facilities, wards, flags, week_flags = kawasaki_facilities(data_dir / filenames["facility"])
        schedule = audit_hours(data_dir / filenames["hours"], set(facilities))
        equal_counts, equal_examples = audit_city_equal_pairs(data_dir / filenames["hours"], set(facilities))
        loaded[kind] = (facilities, wards, flags, week_flags, schedule, equal_counts, equal_examples)
        ward_counts = collections.Counter(wards.values())
        duplicate_facility_ids = len(facilities) - len(set(facilities))
        print(f"\n== {kind}: facilities and joins ==")
        print(f"Kawasaki facilities: {len(facilities)}; ward counts: {dict(ward_counts)}")
        print(f"Facility ID duplicate count: {duplicate_facility_ids}")
        print(f"Hours rows: {schedule['rows']}; distinct hours IDs: {len(schedule['id_counts'])}; "
              f"rows beyond unique ID count (expected multiple specialty/time-band rows): "
              f"{schedule['rows'] - len(schedule['id_counts'])}")
        print(f"Hours IDs without facility: {len(set(schedule['id_counts']) - set(facilities))}; "
              f"facility IDs without hours: {len(set(facilities) - set(schedule['id_counts']))}")
        print(f"Kawasaki schedule records by time-band code: {dict(schedule['time_band_counts'])}")
        print_day_tables(kind, schedule, wards)
        print_queries(schedule)
        print_specialties(kind, schedule)

        print("\nFacility closure-flag counts and comparison (unit: facility x day)")
        for day, (counts, conflicts) in flag_summary(facilities, flags, schedule).items():
            print(f"{day}: flags={dict(counts)}; comparison={dict(conflicts)}")

        special_facility_count = sum(any(value != "" for value in week_flags[facility_id].values())
                                     for facility_id in facilities)
        raw_week_counts = collections.Counter(value for row in week_flags.values()
                                              for value in row.values() if value != "")
        print(f"Facilities with >=1 nonblank week-specific flag: {special_facility_count}")
        print(f"Nonblank week-specific flag values across cells: {dict(raw_week_counts)}")

        sunday_facilities = set(schedule["daily_facilities"]["日"])
        holiday_facilities = set(schedule["daily_facilities"]["祝"])
        differing = 0
        holiday_zero_with_time = 0
        for facility_id in facilities:
            sun = sorted((x[0], x[1], x[2], x[3], x[4])
                         for x in schedule["by_facility_day"][facility_id]["日"])
            holiday = sorted((x[0], x[1], x[2], x[3], x[4])
                             for x in schedule["by_facility_day"][facility_id]["祝"])
            if sun != holiday:
                differing += 1
            if flags[facility_id]["祝"] == "0" and holiday:
                holiday_zero_with_time += 1
        print("Sunday vs holiday schedule lists differ for "
              f"{differing} facilities; Sunday facilities={len(sunday_facilities)}, "
              f"holiday facilities={len(holiday_facilities)}")
        print(f"Holiday flag 0 with holiday time: {holiday_zero_with_time} facilities")
        print(f"Start=end city records: {equal_counts['records']} records, "
              f"{len(equal_counts['facilities'])} facilities; examples={equal_examples}")
        print(f"Start>end city records (not interpreted as overnight): "
              f"{len(schedule['city_crossing'])}; examples={schedule['city_crossing'][:8]}")
        print(f"Invalid/one-sided city time pairs: {len(schedule['city_invalid'])}; "
              f"examples={schedule['city_invalid'][:5]}")

        print("\nNight interval registrations (normal intervals only; record count / facilities)")
        for band in ("20-24", "00-06"):
            pairs = schedule["night_multiband_pairs"][band]
            multiband = sum(len(time_bands) > 1 for time_bands in pairs.values())
            print(f"{band}: {schedule['night_record_counts'][band]} / "
                  f"{len(schedule['night_facilities'][band])}; "
                  f"facility-specialty pairs with >1 band: {multiband}")

        national = schedule["national"]
        print("\nNationwide time-field audit")
        print(f"Valid pairs={national['valid_pairs']}; equal={national['equal_pairs']}; "
              f"start>end={national['crossing_pairs']}; one-sided={national['one_sided_pairs']}; "
              f"invalid populated values={national['invalid_values']}")
        print(f"Start range={national_range(national['start_values'])}; "
              f"end range={national_range(national['end_values'])}; "
              f"00:00 starts={national['zero_start']}; 00:00 ends={national['zero_end']}; "
              f"24:00 fields={national['24_value_fields']}; "
              f"00:00-23:59 pairs={national['full_day_pairs']}")
        print(f"Nationwide normal-interval records overlapping 20:00-24:00="
              f"{national['night_20_24_records']}; 00:00-06:00="
              f"{national['night_00_06_records']}")
        print(f"Blank start fields={national['missing_start']}; blank end fields={national['missing_end']}")

    print("\n== Interpretation notes ==")
    print("- Time-point estimate uses start <= query < end; the definition does not specify end inclusion.")
    print("- Start>end and start=end values are reported separately and are not assigned overnight/24-hour meaning.")
    print("- Closure flags are facility-level; schedule rows are specialty/time-band-level.")
    print("- Blank values are not inferred to mean closed or open.")
    print("- Counts describe source registrations, not actual care availability.")
    print("- No files were written or modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
