"""Streamlit UI for the Level 1 Medical Information Net search."""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import time as datetime_time
from pathlib import Path

import streamlit as st

from medical_hours_navi import DATA_AS_OF
from medical_hours_navi.preprocessing import DAYS, WARD_NAMES
from medical_hours_navi.search import load_search_index, search


ROOT = Path(__file__).resolve().parent
PROCESSED_DIR = ROOT / "data" / "processed"
HOURS_FILE = PROCESSED_DIR / "consultation_hours.csv"
DAY_LABELS = {
    "月": "月曜日", "火": "火曜日", "水": "水曜日", "木": "木曜日",
    "金": "金曜日", "土": "土曜日", "日": "日曜日", "祝": "祝日",
}

st.set_page_config(
    page_title="休日・夜間 医療機関ナビ",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="collapsed",
)


@st.cache_resource(show_spinner="検索用データを読み込んでいます…")
def get_search_index(processed_dir: str):
    """Load the normalized CSVs once and reuse the in-memory search index."""
    return load_search_index(processed_dir)


@st.cache_data(show_spinner=False)
def get_specialties(hours_path: str) -> dict[str, tuple[str, ...]]:
    """Build code-to-label choices from processed rows, preserving each name."""
    names_by_code: dict[str, set[str]] = defaultdict(set)
    with Path(hours_path).open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            code = row.get("specialty_code", "") or ""
            name = row.get("specialty_name", "") or ""
            if code:
                names_by_code[code].add(name)
    return {code: tuple(sorted(names)) for code, names in names_by_code.items()}


def specialty_label(code: str, names_by_code: dict[str, tuple[str, ...]]) -> str:
    names = names_by_code[code]
    if len(names) == 1:
        return f"{names[0]}（{code}）"
    return f"複数の登録名称（{code}）"


def render_result(result: dict) -> None:
    with st.container(border=True):
        title_left, type_right = st.columns([4, 1])
        with title_left:
            st.subheader(result["name"] or "名称未登録")
        with type_right:
            st.caption(result["facility_type"])

        st.write(result["address"])
        st.caption(f"{result['ward']} ・データ基準日 {result['data_as_of']}")

        st.markdown("**条件に一致した診療科・登録時間**")
        for match in result["matched_hours"]:
            name = match["specialty_name"] or "診療科名未登録"
            st.markdown(
                f"- {name}　{match['start_time']}〜{match['end_time']} "
                f"（時間帯{match['time_band']}）"
            )

        warnings = result["closure_warnings"]
        if warnings:
            for warning in warnings:
                st.warning(warning, icon="⚠️")
        else:
            st.caption("休診フラグ・特定週情報から返された追加の注意事項はありません。")


st.title("休日・夜間 医療機関ナビ")
st.caption("川崎市内の公開診療予定を、地域・曜日・時刻・診療科から検索します。")

st.info(
    "検索結果は厚生労働省の公開データに登録された診療予定に基づいており、 "
    "当日の診療実施や患者の受入状況を保証するものではありません。 "
    "受診前に医療機関へ電話などで確認してください。緊急時は119番をご利用ください。",
    icon="ℹ️",
)
st.caption(f"出典：厚生労働省「医療情報ネット」オープンデータ ・データ基準日：{DATA_AS_OF}")

required = [PROCESSED_DIR / filename for filename in
            ("facilities.csv", "consultation_hours.csv", "closure_flags.csv")]
missing = [path.name for path in required if not path.is_file()]
if missing:
    st.error("検索用データがありません: " + ", ".join(missing))
    st.code("python -m medical_hours_navi.preprocessing --data-dir data --output-dir data/processed")
    st.stop()

try:
    search_index = get_search_index(str(PROCESSED_DIR))
    specialties = get_specialties(str(HOURS_FILE))
except (OSError, csv.Error, KeyError, ValueError) as exc:
    st.error(f"検索用データを読み込めませんでした: {exc}")
    st.stop()

if not specialties:
    st.error("検索用データに診療科目コードがありません。前処理データを確認してください。")
    st.stop()

st.subheader("検索条件")
with st.form("medical-hours-search", clear_on_submit=False):
    area_col, day_col = st.columns(2)
    with area_col:
        ward = st.selectbox("地域（区）", options=WARD_NAMES, format_func=lambda value: value)
        st.caption("全域検索は既存の検索関数に対応していないため、区を選択してください。")
    with day_col:
        weekday = st.selectbox(
            "曜日",
            options=DAYS,
            format_func=lambda value: DAY_LABELS[value],
        )

    time_col, specialty_col = st.columns(2)
    with time_col:
        selected_time = st.time_input("時刻", value=datetime_time(9, 0), step=60, format="24h")
    with specialty_col:
        codes = sorted(specialties, key=lambda code: (specialties[code], code))
        specialty_code = st.selectbox(
            "診療科",
            options=codes,
            format_func=lambda code: specialty_label(code, specialties),
        )

    names_for_code = specialties[specialty_code]
    if len(names_for_code) > 1:
        st.caption(
            f"このコードに登録された名称: {'、'.join(names_for_code)}。 "
            "検索関数の仕様に従い、名称ごとの統合はせずコードで検索します。"
        )

    submitted = st.form_submit_button("この条件で検索", type="primary", use_container_width=True)

if submitted:
    query_time = selected_time.strftime("%H:%M")
    try:
        results = search(
            search_index,
            ward=ward,
            weekday=weekday,
            time=query_time,
            specialty_code=specialty_code,
        )
    except ValueError as exc:
        st.error(f"検索条件を確認してください: {exc}")
    else:
        st.divider()
        st.subheader("検索結果")
        st.write(f"**{len(results)}施設**　{ward}・{DAY_LABELS[weekday]}・{query_time}・{specialty_label(specialty_code, specialties)}")
        if not results:
            st.info("条件に一致する登録診療時間は見つかりませんでした。地域・曜日・時刻・診療科を変更して再検索してください。")
        else:
            for result in results:
                render_result(result)

st.divider()
st.caption(
    "表示内容は公開データの登録状況です。時間境界は開始時刻を含み、終了時刻を含まない仮ルールです。 "
    "日付に応じた特定週の休診判定や、実際の受入状況の確認には対応していません。"
)
