"""engine.coverage 单元测试：覆盖单元提取、贪心选样与汇总排序。"""

from app.engine import extract_coverage_units, summarize_coverage


ORDER = ["a", "b", "c", "d"]


def _units(spec):
    """spec: {index: (fired_codes, conflicts)}"""
    units = []
    for index, (fired, conflicts) in spec.items():
        result = {"fired_rule_codes": fired, "conflicts": conflicts}
        units.append((index, extract_coverage_units(result)))
    return units


def test_extract_rule_group_and_field_units():
    result = {
        "fired_rule_codes": ["a", "b"],
        "conflicts": [
            {
                "group": "g",
                "policy": "priority",
                "winner": "a",
                "suppressed": [
                    {"rule_code": "b", "reason": "lower_priority", "priority": 0}
                ],
            },
            {
                "group": None,
                "policy": "collect_all",
                "field": "f",
                "previous_winner": "a",
                "winner": "b",
                "reason": "field_overwritten",
            },
        ],
    }
    assert extract_coverage_units(result) == {
        ("rule", "a"),
        ("rule", "b"),
        ("group", "g", "a", ("b",)),
        ("field", "f", "b", "a"),
    }


def test_group_unit_distinguishes_suppressed_set_order():
    base = {
        "fired_rule_codes": [],
        "conflicts": [
            {"group": "g", "policy": "first_hit", "winner": "a",
             "suppressed": [{"rule_code": "b"}, {"rule_code": "c"}]},
        ],
    }
    swapped = {
        "fired_rule_codes": [],
        "conflicts": [
            {"group": "g", "policy": "first_hit", "winner": "a",
             "suppressed": [{"rule_code": "c"}, {"rule_code": "b"}]},
        ],
    }
    # 被压制集合顺序不同 → 视为不同场景
    assert extract_coverage_units(base) != extract_coverage_units(swapped)


def test_greedy_most_new_units_then_smallest_index():
    # 0: {a}  1: {a,b}  2: {c} —— 第一轮 1 号新增 2 个胜出；再补 2 号
    spec = {
        0: (["a"], []),
        1: (["a", "b"], []),
        2: (["c"], []),
    }
    summary = summarize_coverage(ORDER, _units(spec))
    assert summary["representative_indexes"] == [1, 2]
    assert summary["coverage_unit_count"] == 3
    assert summary["unhit_rule_codes"] == ["d"]
    assert summary["rule_coverage"] == [
        {"code": "a", "hit_count": 2, "covered": True},
        {"code": "b", "hit_count": 1, "covered": True},
        {"code": "c", "hit_count": 1, "covered": True},
        {"code": "d", "hit_count": 0, "covered": False},
    ]


def test_greedy_tie_breaks_smallest_index_and_stops_without_gain():
    # 并列：0 与 2 各一个新单元，先取 0 再取 2；1 号无单元永不入选
    spec = {
        0: (["a"], []),
        1: ([], []),
        2: (["b"], []),
    }
    summary = summarize_coverage(ORDER, _units(spec))
    assert summary["representative_indexes"] == [0, 2]


def test_observed_conflicts_sorted_with_count():
    conflict_a = {
        "group": "zeta", "policy": "priority", "winner": "a",
        "suppressed": [{"rule_code": "b"}],
    }
    conflict_b = {
        "group": None, "policy": "collect_all", "field": "alpha",
        "previous_winner": "c", "winner": "d",
    }
    spec = {
        0: (["a", "b"], [conflict_a, conflict_b]),
        1: (["a", "b"], [conflict_a]),
    }
    summary = summarize_coverage(ORDER, _units(spec))
    observed = summary["observed_conflicts"]
    # field 在前、group 在后
    assert [o["kind"] for o in observed] == ["field", "group"]
    assert observed[0]["field"] == "alpha"
    assert observed[0]["observed_count"] == 1
    assert observed[1]["group"] == "zeta"
    assert observed[1]["observed_count"] == 2
    assert observed[1]["suppressed_rule_codes"] == ["b"]


def test_empty_inputs():
    summary = summarize_coverage(ORDER, [])
    assert summary["representative_indexes"] == []
    assert summary["coverage_unit_count"] == 0
    assert summary["unhit_rule_codes"] == ORDER
    assert summary["observed_conflicts"] == []
    assert all(r["hit_count"] == 0 for r in summary["rule_coverage"])
