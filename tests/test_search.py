import csv

import pytest

from medical_hours_navi.preprocessing import (
    CLOSURE_COLUMNS,
    DAYS,
    FACILITY_COLUMNS,
    HOURS_COLUMNS,
    classify_time_pair,
    parse_time,
    preprocess,
)
from medical_hours_navi.search import load_search_index, search


def write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def processed_fixture(tmp_path, *, hours=None, closures=None):
    processed = tmp_path / "processed"
    facilities = [{
        "facility_id": "00001",
        "facility_type": "診療所",
        "name": "テスト施設",
        "address": "神奈川県川崎市川崎区テスト1-1",
        "ward": "川崎区",
        "prefecture_code": "14",
        "municipality_code": "131",
        "latitude": "35.0",
        "longitude": "139.0",
        "data_as_of": "2026-06-01",
    }]
    write_csv(processed / "facilities.csv", FACILITY_COLUMNS, facilities)
    default_hours = [{
        "facility_id": "00001", "specialty_code": "01001", "specialty_name": "内科",
        "weekday": "日", "start_time": "09:00", "end_time": "12:00",
        "start_minute": "540", "end_minute": "720", "time_band": "1",
        "searchable": "true", "exclusion_reason": "", "data_as_of": "2026-06-01",
    }]
    write_csv(processed / "consultation_hours.csv", HOURS_COLUMNS, hours or default_hours)
    default_closures = [{
        "facility_id": "00001", "closure_scope": "weekly", "weekday": "日",
        "week_number": "", "closure_flag": "1", "data_as_of": "2026-06-01",
    }]
    write_csv(processed / "closure_flags.csv", CLOSURE_COLUMNS,
              closures if closures is not None else default_closures)
    return processed


def run_search(index, time="09:00", weekday="日", code="01001"):
    return search(index, ward="川崎区", weekday=weekday, time=time, specialty_code=code)


def test_normal_search_returns_expected_facility(tmp_path):
    index = load_search_index(processed_fixture(tmp_path))
    results = run_search(index)
    assert len(results) == 1
    assert results[0]["facility_id"] == "00001"
    assert results[0]["specialty_name"] == "内科"
    assert results[0]["data_as_of"] == "2026-06-01"


def test_start_is_inclusive(tmp_path):
    index = load_search_index(processed_fixture(tmp_path))
    assert len(run_search(index, time="09:00")) == 1


def test_end_is_exclusive(tmp_path):
    index = load_search_index(processed_fixture(tmp_path))
    assert run_search(index, time="12:00") == []


def test_multiple_time_bands_are_kept_as_matches_and_facility_is_unique(tmp_path):
    first = {
        "facility_id": "00001", "specialty_code": "01001", "specialty_name": "内科",
        "weekday": "日", "start_time": "09:00", "end_time": "11:00",
        "start_minute": "540", "end_minute": "660", "time_band": "1",
        "searchable": "true", "exclusion_reason": "", "data_as_of": "2026-06-01",
    }
    second = {**first, "start_time": "09:30", "end_time": "10:30",
              "start_minute": "570", "end_minute": "630", "time_band": "2"}
    index = load_search_index(processed_fixture(tmp_path, hours=[first, second]))
    results = run_search(index, time="10:00")
    assert len(results) == 1
    assert [match["time_band"] for match in results[0]["matched_hours"]] == ["1", "2"]


def test_no_matches_returns_empty_list(tmp_path):
    index = load_search_index(processed_fixture(tmp_path))
    assert run_search(index, time="09:00", code="99999") == []


def test_all_search_dimensions_are_required(tmp_path):
    index = load_search_index(processed_fixture(tmp_path))
    assert search(index, ward="幸区", weekday="日", time="09:00", specialty_code="01001") == []
    assert search(index, ward="川崎区", weekday="土", time="09:00", specialty_code="01001") == []
    assert search(index, ward="川崎区", weekday="日", time="12:00", specialty_code="01001") == []


@pytest.mark.parametrize("value", ["9:00", "24:00", "12:60", "noon", ""])
def test_invalid_query_time_is_rejected(tmp_path, value):
    index = load_search_index(processed_fixture(tmp_path))
    with pytest.raises(ValueError):
        run_search(index, time=value)


@pytest.mark.parametrize("start,end,reason", [
    ("19:00", "07:00", "start_after_end_uninterpreted"),
    ("09:00", "09:00", "start_equals_end_uninterpreted"),
    ("", "10:00", "missing_start"),
    ("09:00", "", "missing_end"),
    ("24:00", "10:00", "invalid_time_format"),
])
def test_ambiguous_or_invalid_pairs_are_not_searchable(start, end, reason):
    start_minute, end_minute, searchable, actual_reason = classify_time_pair(start, end)
    assert not searchable
    assert actual_reason == reason


def test_parse_time_is_strict_and_keeps_minute_value():
    assert parse_time("00:00") == 0
    assert parse_time("09:05") == 545
    assert parse_time("23:59") == 1439
    assert parse_time("24:00") is None
    assert parse_time("9:05") is None


def test_closure_flag_conflict_and_specific_week_create_warnings(tmp_path):
    closures = [
        {"facility_id": "00001", "closure_scope": "weekly", "weekday": "日",
         "week_number": "", "closure_flag": "0", "data_as_of": "2026-06-01"},
        {"facility_id": "00001", "closure_scope": "specific_week", "weekday": "日",
         "week_number": "1", "closure_flag": "0", "data_as_of": "2026-06-01"},
    ]
    index = load_search_index(processed_fixture(tmp_path, closures=closures))
    warnings = run_search(index)[0]["closure_warnings"]
    assert any("優先関係は未確認" in warning for warning in warnings)
    assert any("実際の日付による確認が必要" in warning for warning in warnings)


def test_blank_closure_flag_is_reported_as_unconfirmed(tmp_path):
    closures = [{
        "facility_id": "00001", "closure_scope": "weekly", "weekday": "日",
        "week_number": "", "closure_flag": "", "data_as_of": "2026-06-01",
    }]
    index = load_search_index(processed_fixture(tmp_path, closures=closures))
    warnings = run_search(index)[0]["closure_warnings"]
    assert any("空欄" in warning and "未確認" in warning for warning in warnings)


def test_preprocessing_emits_three_csvs_and_excludes_ambiguous_hours(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    facility_columns = [
        "ID", "正式名称", "都道府県コード", "市区町村コード", "所在地",
        "所在地座標（緯度）", "所在地座標（経度）",
        *[f"毎週決まった曜日に休診（{day}）" for day in ("月", "火", "水", "木", "金", "土", "日")],
        "祝日に休診",
        *[f"決まった週に休診（定期週）第{week}週（{day}）"
          for week in range(1, 6) for day in ("月", "火", "水", "木", "金", "土", "日")],
    ]
    facility_row = {
        "ID": "00001", "正式名称": "テスト施設", "都道府県コード": "14",
        "市区町村コード": "131", "所在地": "神奈川県川崎市川崎区テスト1-1",
        "所在地座標（緯度）": "35.0", "所在地座標（経度）": "139.0",
        "毎週決まった曜日に休診（日）": "1", "祝日に休診": "0",
        "決まった週に休診（定期週）第1週（日）": "0",
    }
    write_csv(source / "01-1_hospital_facility_info_20260601.csv", facility_columns, [])
    write_csv(source / "02-1_clinic_facility_info_20260601.csv", facility_columns, [facility_row])
    hour_columns = ["ID", "診療科目コード", "診療科目名", "診療時間帯"]
    hour_columns += [f"{day}_診療{edge}時間" for day in DAYS for edge in ("開始", "終了")]
    hour_row = {column: "" for column in hour_columns}
    hour_row.update({"ID": "00001", "診療科目コード": "01001", "診療科目名": "内科", "診療時間帯": "1",
                     "日_診療開始時間": "09:00", "日_診療終了時間": "10:00",
                     "月_診療開始時間": "19:00", "月_診療終了時間": "07:00"})
    write_csv(source / "01-2_hospital_speciality_hours_20260601.csv", hour_columns, [])
    write_csv(source / "02-2_clinic_speciality_hours_20260601.csv", hour_columns, [hour_row])

    summary = preprocess(source, output, log=lambda _: None)
    assert summary["facilities"] == 1
    assert summary["searchable_hours"] == 1
    assert summary["excluded_start_after_end_uninterpreted"] == 1
    assert (output / "facilities.csv").is_file()
    assert (output / "consultation_hours.csv").is_file()
    assert (output / "closure_flags.csv").is_file()
    with (output / "consultation_hours.csv").open(encoding="utf-8", newline="") as stream:
        output_hours = list(csv.DictReader(stream))
    assert len(output_hours) == 2
    assert {row["searchable"] for row in output_hours} == {"true", "false"}
