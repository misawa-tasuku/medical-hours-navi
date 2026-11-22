"""Search normalized Kawasaki City facility and consultation-hour CSVs."""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
from collections import defaultdict
from dataclasses import dataclass

from .preprocessing import DAYS, WARD_NAMES, parse_time


@dataclass
class SearchIndex:
    facilities: dict[str, dict[str, str]]
    closures: dict[tuple[str, str, str, str], str]
    special_week_facilities: set[str]
    hours_by_query: dict[tuple[str, str, str], list[dict[str, str]]]


def load_search_index(processed_dir: str | pathlib.Path = "data/processed") -> SearchIndex:
    processed_dir = pathlib.Path(processed_dir)
    facilities: dict[str, dict[str, str]] = {}
    with (processed_dir / "facilities.csv").open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            facilities[row["facility_id"]] = row

    closures: dict[tuple[str, str, str, str], str] = {}
    special_week_facilities = set()
    with (processed_dir / "closure_flags.csv").open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            key = (row["facility_id"], row["closure_scope"], row["weekday"], row["week_number"])
            closures[key] = row.get("closure_flag", "") or ""
            if row["closure_scope"] == "specific_week" and row.get("closure_flag", ""):
                special_week_facilities.add(row["facility_id"])

    hours_by_query: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    with (processed_dir / "consultation_hours.csv").open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("searchable") != "true":
                continue
            facility = facilities.get(row.get("facility_id", ""))
            if not facility or not facility.get("ward"):
                continue
            key = (facility["ward"], row["weekday"], row["specialty_code"])
            hours_by_query[key].append(row)

    return SearchIndex(facilities, closures, special_week_facilities, dict(hours_by_query))


def _validate_query(ward: str, weekday: str, time: str, specialty_code: str) -> int:
    if ward not in WARD_NAMES:
        raise ValueError(f"ward must be one of: {', '.join(WARD_NAMES)}")
    if weekday not in DAYS:
        raise ValueError(f"weekday must be one of: {', '.join(DAYS)}")
    minute = parse_time(time)
    if minute is None:
        raise ValueError("time must use HH:MM in the range 00:00..23:59")
    if not specialty_code or not specialty_code.isascii() or not specialty_code.isdigit():
        raise ValueError("specialty_code must be a nonempty string of digits")
    return minute


def search(index: SearchIndex, *, ward: str, weekday: str, time: str,
           specialty_code: str) -> list[dict]:
    """Return one result per facility; all four query conditions are ANDed.

    Only preprocessing rows with searchable=true enter the index. For a valid
    interval, the MVP rule is start <= query < end. Results represent source
    data registrations, not a statement that care is currently available.
    """
    query_minute = _validate_query(ward, weekday, time, specialty_code)
    key = (ward, weekday, specialty_code)
    matched: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in index.hours_by_query.get(key, []):
        start, end = int(row["start_minute"]), int(row["end_minute"])
        if start <= query_minute < end:
            matched[row["facility_id"]].append({
                "specialty_code": row["specialty_code"],
                "specialty_name": row["specialty_name"],
                "time_band": row["time_band"],
                "weekday": row["weekday"],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
            })

    results = []
    for facility_id, matches in matched.items():
        facility = index.facilities[facility_id]
        flag_key = (facility_id, "weekly", weekday, "")
        closure_flag = index.closures.get(flag_key, "")
        warnings = []
        if closure_flag == "0":
            warnings.append("休診フラグは0（休診）ですが診療時間の登録があります。優先関係は未確認です。")
        elif closure_flag == "":
            warnings.append("該当曜日の休診フラグは空欄で、意味は未確認です。")
        if facility_id in index.special_week_facilities:
            warnings.append("特定週の休診情報があります。実際の日付による確認が必要です。")

        matches.sort(key=lambda item: (item["specialty_name"], item["time_band"], item["start_time"], item["end_time"]))
        names = sorted({item["specialty_name"] for item in matches})
        results.append({
            "facility_id": facility_id,
            "facility_type": facility["facility_type"],
            "name": facility["name"],
            "address": facility["address"],
            "ward": facility["ward"],
            "specialty_code": specialty_code,
            "specialty_name": names[0] if len(names) == 1 else None,
            "specialty_names": names,
            "matched_hours": matches,
            "closure_warnings": warnings,
            "data_as_of": facility.get("data_as_of", "2026-06-01"),
        })
    return sorted(results, key=lambda item: (item["name"], item["facility_id"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processed-dir", type=pathlib.Path, default=pathlib.Path("data/processed"))
    parser.add_argument("--ward", required=True, choices=WARD_NAMES)
    parser.add_argument("--weekday", required=True, choices=DAYS)
    parser.add_argument("--time", required=True, help="HH:MM")
    parser.add_argument("--specialty-code", required=True)
    args = parser.parse_args()
    index = load_search_index(args.processed_dir)
    results = search(index, ward=args.ward, weekday=args.weekday, time=args.time,
                     specialty_code=args.specialty_code)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
