"""Create Kawasaki City search data from the original Medical Information Net CSVs."""

from __future__ import annotations

import argparse
import csv
import logging
import pathlib
from collections import Counter
from typing import Callable

from . import DATA_AS_OF


logger = logging.getLogger(__name__)
DAYS = ("月", "火", "水", "木", "金", "土", "日", "祝")
WEEKDAYS = DAYS[:7]
KAWASAKI_MUNICIPALITY_CODES = {f"{code:03d}" for code in range(131, 138)}
WARD_NAMES = ("川崎区", "幸区", "中原区", "高津区", "多摩区", "宮前区", "麻生区")

FACILITY_SOURCES = (
    ("病院", "01-1_hospital_facility_info_20260601.csv"),
    ("診療所", "02-1_clinic_facility_info_20260601.csv"),
)
HOURS_SOURCES = (
    ("病院", "01-2_hospital_speciality_hours_20260601.csv"),
    ("診療所", "02-2_clinic_speciality_hours_20260601.csv"),
)

FACILITY_COLUMNS = (
    "facility_id", "facility_type", "name", "address", "ward",
    "prefecture_code", "municipality_code", "latitude", "longitude", "data_as_of",
)
HOURS_COLUMNS = (
    "facility_id", "specialty_code", "specialty_name", "weekday",
    "start_time", "end_time", "start_minute", "end_minute", "time_band",
    "searchable", "exclusion_reason", "data_as_of",
)
CLOSURE_COLUMNS = (
    "facility_id", "closure_scope", "weekday", "week_number",
    "closure_flag", "data_as_of",
)


def identify_ward(address: str) -> str:
    """Return a ward only when its literal name occurs in the source address."""
    return next((ward for ward in WARD_NAMES if ward in (address or "")), "")


def parse_time(value: str) -> int | None:
    """Parse strict HH:MM in the range 00:00..23:59 to minutes since midnight."""
    if not value or len(value) != 5 or value[2] != ":":
        return None
    hour_text, minute_text = value[:2], value[3:]
    if not (hour_text.isascii() and hour_text.isdigit() and
            minute_text.isascii() and minute_text.isdigit()):
        return None
    hour, minute = int(hour_text), int(minute_text)
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def classify_time_pair(start: str, end: str) -> tuple[int | None, int | None, bool, str]:
    """Return parsed minutes, searchable status, and an explicit exclusion reason."""
    if not start and not end:
        return None, None, False, "missing_both"
    start_minute = parse_time(start) if start else None
    end_minute = parse_time(end) if end else None
    if (start and start_minute is None) or (end and end_minute is None):
        return start_minute, end_minute, False, "invalid_time_format"
    if not start:
        return None, end_minute, False, "missing_start"
    if not end:
        return start_minute, None, False, "missing_end"
    if start_minute > end_minute:
        return start_minute, end_minute, False, "start_after_end_uninterpreted"
    if start_minute == end_minute:
        return start_minute, end_minute, False, "start_equals_end_uninterpreted"
    return start_minute, end_minute, True, ""


def _read_city_facilities(data_dir: pathlib.Path, log: Callable[[str], None]):
    facilities: dict[str, dict[str, str]] = {}
    source_counts = Counter()
    unknown_wards = Counter()

    for facility_type, filename in FACILITY_SOURCES:
        path = data_dir / filename
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"ID", "都道府県コード", "市区町村コード", "所在地", "正式名称"}
            required.update(f"毎週決まった曜日に休診（{day}）" for day in WEEKDAYS)
            required.add("祝日に休診")
            required.update(
                f"決まった週に休診（定期週）第{week}週（{day}）"
                for week in range(1, 6) for day in WEEKDAYS
            )
            missing = required - set(reader.fieldnames or ())
            if missing:
                raise ValueError(f"{filename}: required columns missing: {sorted(missing)}")
            for row in reader:
                municipality_code = (row.get("市区町村コード") or "").strip()
                if row.get("都道府県コード") != "14" or municipality_code not in KAWASAKI_MUNICIPALITY_CODES:
                    continue

                facility_id = row.get("ID", "")
                address = row.get("所在地", "") or ""
                ward = identify_ward(address)
                if not ward:
                    unknown_wards[facility_type] += 1
                if facility_id in facilities:
                    raise ValueError(f"Duplicate facility ID across facility files: {facility_id}")

                facilities[facility_id] = {
                    "facility_id": facility_id,
                    "facility_type": facility_type,
                    "name": row.get("正式名称", "") or "",
                    "address": address,
                    "ward": ward,
                    "prefecture_code": row.get("都道府県コード", "") or "",
                    "municipality_code": municipality_code,
                    "latitude": row.get("所在地座標（緯度）", "") or "",
                    "longitude": row.get("所在地座標（経度）", "") or "",
                    "data_as_of": DATA_AS_OF,
                }
                source_counts[facility_type] += 1

    log(f"施設抽出: 病院={source_counts['病院']}, 診療所={source_counts['診療所']}, "
        f"合計={len(facilities)}")
    if source_counts["病院"] != 38 or source_counts["診療所"] != 886:
        log("警告: 期待件数（病院38・診療所886）と異なります。入力データ/地域コードを確認してください。")
    if unknown_wards:
        log(f"区名を所在地から特定できない施設: {dict(unknown_wards)}")
    return facilities


def _write_csv(path: pathlib.Path, columns: tuple[str, ...], rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def preprocess(data_dir: str | pathlib.Path = "data",
               output_dir: str | pathlib.Path = "data/processed",
               log: Callable[[str], None] | None = None) -> dict[str, int]:
    """Extract Kawasaki facilities and emit the three normalized CSV files.

    Source files are opened read-only. Nonempty source time pairs are retained,
    including unsearchable pairs, with an exclusion reason. Fully blank pairs
    are counted and omitted. Each specialty/time-band/day remains a separate row.
    """
    data_dir, output_dir = pathlib.Path(data_dir), pathlib.Path(output_dir)
    log = log or (lambda message: logger.info(message))
    required_files = [data_dir / filename for _, filename in FACILITY_SOURCES + HOURS_SOURCES]
    missing_files = [str(path) for path in required_files if not path.is_file()]
    if missing_files:
        raise FileNotFoundError("Required input file(s) not found: " + ", ".join(missing_files))

    facilities = _read_city_facilities(data_dir, log)
    _write_csv(output_dir / "facilities.csv", FACILITY_COLUMNS, facilities.values())

    closure_rows = []
    for facility_type, filename in FACILITY_SOURCES:
        path = data_dir / filename
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                facility_id = row.get("ID", "")
                facility = facilities.get(facility_id)
                if not facility or facility["facility_type"] != facility_type:
                    continue
                for day in DAYS:
                    source_column = "祝日に休診" if day == "祝" else f"毎週決まった曜日に休診（{day}）"
                    closure_rows.append({
                        "facility_id": facility_id,
                        "closure_scope": "weekly",
                        "weekday": day,
                        "week_number": "",
                        "closure_flag": row.get(source_column, "") or "",
                        "data_as_of": DATA_AS_OF,
                    })
                for week in range(1, 6):
                    for day in WEEKDAYS:
                        source_column = f"決まった週に休診（定期週）第{week}週（{day}）"
                        closure_rows.append({
                            "facility_id": facility_id,
                            "closure_scope": "specific_week",
                            "weekday": day,
                            "week_number": str(week),
                            "closure_flag": row.get(source_column, "") or "",
                            "data_as_of": DATA_AS_OF,
                        })
    _write_csv(output_dir / "closure_flags.csv", CLOSURE_COLUMNS, closure_rows)

    hours_rows = []
    counters = Counter()
    code_to_names: dict[str, set[str]] = {}
    for facility_type, filename in HOURS_SOURCES:
        path = data_dir / filename
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"ID", "診療科目コード", "診療科目名", "診療時間帯"}
            required.update(
                f"{day}_診療{edge}時間"
                for day in DAYS for edge in ("開始", "終了")
            )
            missing = required - set(reader.fieldnames or ())
            if missing:
                raise ValueError(f"{filename}: required columns missing: {sorted(missing)}")
            for row in reader:
                facility_id = row.get("ID", "")
                facility = facilities.get(facility_id)
                if not facility or facility["facility_type"] != facility_type:
                    continue
                counters["kawasaki_source_rows"] += 1
                specialty_code = row.get("診療科目コード", "") or ""
                specialty_name = row.get("診療科目名", "") or ""
                time_band = row.get("診療時間帯", "") or ""
                code_to_names.setdefault(specialty_code, set()).add(specialty_name)
                for day in DAYS:
                    start = row.get(f"{day}_診療開始時間", "") or ""
                    end = row.get(f"{day}_診療終了時間", "") or ""
                    start_minute, end_minute, searchable, reason = classify_time_pair(start, end)
                    if reason == "missing_both":
                        counters["blank_pairs_omitted"] += 1
                        continue
                    if not searchable:
                        counters[f"excluded_{reason}"] += 1
                    else:
                        counters["searchable_pairs"] += 1
                    counters["retained_pairs"] += 1
                    hours_rows.append({
                        "facility_id": facility_id,
                        "specialty_code": specialty_code,
                        "specialty_name": specialty_name,
                        "weekday": day,
                        "start_time": start,
                        "end_time": end,
                        "start_minute": "" if start_minute is None else start_minute,
                        "end_minute": "" if end_minute is None else end_minute,
                        "time_band": time_band,
                        "searchable": "true" if searchable else "false",
                        "exclusion_reason": reason,
                        "data_as_of": DATA_AS_OF,
                    })
    _write_csv(output_dir / "consultation_hours.csv", HOURS_COLUMNS, hours_rows)

    ambiguous_codes = {code: sorted(names) for code, names in code_to_names.items() if len(names) > 1}
    log(f"診療時間: 川崎市内の元レコード={counters['kawasaki_source_rows']}, "
        f"出力枠={counters['retained_pairs']}, 検索対象={counters['searchable_pairs']}, "
        f"空欄ペア省略={counters['blank_pairs_omitted']}")
    excluded = {key.removeprefix("excluded_"): value for key, value in counters.items()
                if key.startswith("excluded_")}
    log(f"検索対象外の非空枠: {excluded}")
    log(f"休診フラグ出力行数={len(closure_rows)}")
    if ambiguous_codes:
        log(f"複数名称が登録された診療科コード（統合せず保持）: {ambiguous_codes}")

    return {
        "facilities": len(facilities),
        "consultation_hours": len(hours_rows),
        "closure_flags": len(closure_rows),
        "searchable_hours": counters["searchable_pairs"],
        "blank_pairs_omitted": counters["blank_pairs_omitted"],
        **{key: value for key, value in counters.items() if key.startswith("excluded_")},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=pathlib.Path, default=pathlib.Path("data"))
    parser.add_argument("--output-dir", type=pathlib.Path, default=pathlib.Path("data/processed"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary = preprocess(args.data_dir, args.output_dir, log=logger.info)
    logger.info(f"出力件数: {summary}")


if __name__ == "__main__":
    main()
