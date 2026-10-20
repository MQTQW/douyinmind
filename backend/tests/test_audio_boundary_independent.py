"""Independent black-box boundaries. No model/worker/production DB is used."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.services.audio_boundary import propose_boundary_splice, select_boundary_candidates


PREFIX = "开头内容必须完整保留甲乙丙丁戊己庚辛壬癸天地"
SUFFIX = "玄黄宇宙洪荒日月盈昃辰宿结尾内容必须完整保留"


def part(text, start=0, end=60):
    return {"text": text, "start_seconds": start, "end_seconds": end}


def proposal(left, right, heard):
    return propose_boundary_splice(
        [part(PREFIX + left + "。", 0, 60)],
        [part(right + SUFFIX, 60, 120)],
        [part(PREFIX + heard + SUFFIX, 52, 68)],
    )


@pytest.mark.parametrize(("before", "after"), [
    ("３", "４"), ("壹", "贰"), ("①", "②"), ("Ⅲ", "Ⅳ"),
    ("三", "四"), ("3", "4"), ("〇", "零"), ("两", "俩"),
])
def test_changed_numeric_forms_are_never_adopted(before, after):
    result = proposal("实际为" + before, "公斤重量", "实际为" + after + "公斤重量")
    assert not result["accepted"]
    assert result["after_parts"] == result["before_parts"]


@pytest.mark.parametrize("symbol", ["%", "％", "‰", "℃", "℉", "°", "+", "−"])
def test_non_alnum_units_and_signs_cannot_disappear_in_normalization(symbol):
    result = proposal("比例为三", "份的物品", "比例为三" + symbol + "份的物品")
    assert not result["accepted"]
    assert result["after_parts"] == result["before_parts"]


@pytest.mark.parametrize("negation", ["不", "没", "无", "未", "非", "勿", "别", "莫"])
def test_negation_insertion_never_adopted(negation):
    result = proposal("目前可以", "正常通过", "目前" + negation + "可以正常通过")
    assert not result["accepted"]
    assert result["after_parts"] == result["before_parts"]


def test_real_repetition_not_deleted_just_to_make_text_fluent():
    result = proposal("这种重复重", "复不能删除", "这种重复不能删除")
    assert not result["accepted"]
    assert result["after_parts"] == result["before_parts"]


def test_english_apostrophe_is_not_disposable_punctuation():
    result = proposal("原句为don'", "t接受处理", "原句为dont接受处理")
    assert not result["accepted"]
    assert result["after_parts"] == result["before_parts"]


def test_negation_scope_punctuation_change_conservatively_rejected():
    result = proposal("目前还不", "可能接受", "目前还不可能接受")
    assert not result["accepted"]
    assert result["after_parts"] == result["before_parts"]


@pytest.mark.parametrize("left,heard", [
    ("不，甲乙丙", "不甲乙丙丁戊己庚"),
    ("不：甲乙丙", "不，甲乙丙丁戊己庚"),
    ("还不，甲乙丙", "还不甲乙丙丁戊己庚"),
])
def test_punctuation_next_to_negation_outside_shortest_center_is_protected(left, heard):
    result = proposal(left, "丁戊己庚", heard)
    assert not result["accepted"]
    assert result["after_parts"] == result["before_parts"]


def test_unchanged_number_does_not_allow_counting_unit_to_change():
    result = proposal("实际为四", "台设备上", "实际为四套设备上")
    assert not result["accepted"]
    assert result["after_parts"] == result["before_parts"]


def test_written_temperature_unit_is_not_only_the_character_degree():
    result = proposal("温度为摄", "氏度范围", "温度为华氏度范围")
    assert not result["accepted"]
    assert result["after_parts"] == result["before_parts"]


def test_unique_anchors_reject_repeated_heard_context():
    heard = PREFIX + "转变恰好为土豆的逆袭创造" + SUFFIX
    result = proposal("转变恰好为土", "的逆袭创造", "转变恰好为土豆的逆袭创造")
    assert result["accepted"]  # Controlled positive; the doubled review is ambiguous.
    ambiguous = propose_boundary_splice(
        result["before_parts"][:1], result["before_parts"][1:], [part(heard + heard, 52, 68)])
    assert not ambiguous["accepted"]
    assert ambiguous["after_parts"] == ambiguous["before_parts"]


def test_reversed_two_sided_anchors_rejected():
    result = propose_boundary_splice(
        [part(PREFIX + "转变恰好为土。")],
        [part("的逆袭创造" + SUFFIX, 60, 120)],
        [part(SUFFIX + "土豆" + PREFIX, 52, 68)],
    )
    assert not result["accepted"]


@pytest.mark.parametrize("left,right", [
    ([part(PREFIX + "转变恰好为土。", 0, 65)], [part("的逆袭创造" + SUFFIX, 60, 120)]),
    ([part(PREFIX + "转变恰好为土。", 60, 120)], [part("的逆袭创造" + SUFFIX, 0, 60)]),
])
def test_cross_root_overlap_or_reversal_cannot_authorize_splice(left, right):
    result = propose_boundary_splice(left, right, [part(PREFIX + "转变恰好为土豆的逆袭创造" + SUFFIX, 52, 68)])
    assert not result["accepted"]
    assert result["reason"] == "unordered_original_records"
    assert result["after_parts"] == result["before_parts"]


def test_only_bounded_center_is_changed_and_original_envelope_preserved():
    left = [part("完全在外的前一句。", 0, 15), part(PREFIX + "转变恰好为土。", 15, 60)]
    right = [part("的逆袭创造" + SUFFIX, 60, 90), part("完全在外的后一句！", 90, 120)]
    original = deepcopy((left, right))
    heard = [part("错误窗口开头。" + PREFIX + "转变恰好为土豆的逆袭创造" + SUFFIX + "错误窗口结尾。", 52, 68)]
    result = propose_boundary_splice(left, right, heard)
    assert result["accepted"]
    assert (left, right) == original
    assert result["after_parts"][0] == left[0]
    assert result["after_parts"][-1] == right[-1]
    merged = result["after_parts"][1]
    assert merged["start_seconds"] == 15 and merged["end_seconds"] == 90
    assert merged["text"].startswith(PREFIX) and merged["text"].endswith(SUFFIX)
    assert "错误窗口" not in "".join(p["text"] for p in result["after_parts"])
    old = "\n".join(p["text"] for p in left + right)
    new = "\n".join(p["text"] for p in result["after_parts"])
    a = result["anchors"]
    assert new == old[:a["raw_start"]] + a["review_center"] + old[a["raw_end"]:]
    assert result["acoustic_status"] == "unverified_acoustic"


def test_frozen_real_four_windows_keep_outer_original_not_whole_window():
    folder = Path(__file__).parents[1] / "app/storage/continuous_testing/2026-10-19/round-05/audio"
    if not folder.exists():
        pytest.skip("Local frozen real experiment not present")
    decisions = {}
    for boundary in (120, 240, 420, 720):
        runs = {arm: json.loads((folder / f"{boundary}-{arm}.result.json").read_text(encoding="utf-8"))
                for arm in ("A-left", "A-right", "B-continuous")}
        left, right, review = [runs[arm]["absolute_parts"] for arm in ("A-left", "A-right", "B-continuous")]
        result = propose_boundary_splice(left, right, review)
        decisions[boundary] = result["accepted"]
        if not result["accepted"]:
            assert result["after_parts"] == left + right
        else:
            old = "\n".join(p["text"] for p in left + right)
            new = "\n".join(p["text"] for p in result["after_parts"])
            a = result["anchors"]
            assert new == old[:a["raw_start"]] + a["review_center"] + old[a["raw_end"]:]
    assert decisions == {120: False, 240: False, 420: True, 720: True}


def test_candidate_limit_and_source_order_no_input_mutation():
    roots = [{"start_seconds": i * 60, "parts": [part("中心片。" if i % 2 == 0 else "正常的长句不应乱截结束。", i * 60, (i + 1) * 60)]}
             for i in range(10)]
    original = deepcopy(roots)
    result = select_boundary_candidates(roots, max_windows=4)
    assert len(result) <= 4
    assert [c["boundary_seconds"] for c in result] == sorted(c["boundary_seconds"] for c in result)
    assert len({c["left_root_index"] for c in result}) == len(result)
    assert select_boundary_candidates(roots, max_windows=0) == []
    assert roots == original
