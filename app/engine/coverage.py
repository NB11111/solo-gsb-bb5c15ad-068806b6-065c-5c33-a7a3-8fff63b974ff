"""文档覆盖分析：不落库的批量求值 + 覆盖单元汇总与确定性贪心选样。

覆盖单元（coverage unit）是一次成功求值观察到的最小事实：

- 规则命中单元 ``("rule", code)``：来自 ``fired_rule_codes``（含被冲突
  策略压制者）；
- 冲突组单元 ``("group", group, winner, suppressed_codes)``：来自
  priority / first_hit 组冲突解释，被压制规则按 evaluation_order 排列；
- 字段覆盖单元 ``("field", field, winner, previous_winner)``：来自
  collect_all 的字段级覆盖解释。

选样（representative_indexes）按确定性贪心产生：每轮在尚未选中的成功
项中选择“新增覆盖单元数”最多的一项，并列时取最小输入下标；直至没有
任何项能带来新增单元。求值失败的项不参与选择。
"""

from __future__ import annotations

from typing import Any, Iterable

# 覆盖单元：
# ("rule", code)
# ("group", group, winner, (suppressed_code, ...))
# ("field", field, winner, previous_winner)
CoverageUnit = tuple

_KIND_ORDER = {"field": 0, "group": 1, "rule": 2}


def extract_coverage_units(result: dict[str, Any]) -> set[CoverageUnit]:
    """从一次成功求值结果中提取覆盖单元集合。"""
    units: set[CoverageUnit] = {
        ("rule", code) for code in result.get("fired_rule_codes") or []
    }
    for conflict in result.get("conflicts") or []:
        unit = _conflict_unit(conflict)
        if unit is not None:
            units.add(unit)
    return units


def _conflict_unit(conflict: dict[str, Any]) -> CoverageUnit | None:
    """把单条冲突解释归一为覆盖单元；无法识别的形态返回 None。"""
    # collect_all 的字段覆盖：group 为 None、带 field
    if conflict.get("group") is None and conflict.get("field"):
        return (
            "field",
            conflict["field"],
            conflict.get("winner"),
            conflict.get("previous_winner"),
        )
    group = conflict.get("group")
    if group:
        suppressed = tuple(
            s.get("rule_code")
            for s in conflict.get("suppressed") or []
            if s.get("rule_code") is not None
        )
        return ("group", group, conflict.get("winner"), suppressed)
    return None


def _sort_key(unit: CoverageUnit) -> tuple:
    kind = _KIND_ORDER.get(unit[0], len(_KIND_ORDER))
    return (kind,) + tuple((0, x) if x is None else (1, x) for x in unit[1:])


def summarize_coverage(
    evaluation_order: list[str],
    item_units: Iterable[tuple[int, set[CoverageUnit]]],
) -> dict[str, Any]:
    """汇总规则命中次数、已观察冲突场景，并确定性贪心选出代表项下标。

    item_units 为 ``(输入下标, 覆盖单元集合)`` 的可迭代对象，仅包含成功项。
    """
    indexed = [(index, units) for index, units in item_units]

    hit_counts: dict[str, int] = {}
    scenario_counts: dict[CoverageUnit, int] = {}
    for _index, units in indexed:
        for unit in units:
            if unit[0] == "rule":
                hit_counts[unit[1]] = hit_counts.get(unit[1], 0) + 1
            else:
                scenario_counts[unit] = scenario_counts.get(unit, 0) + 1

    rule_coverage = [
        {
            "code": code,
            "hit_count": hit_counts.get(code, 0),
            "covered": code in hit_counts,
        }
        for code in evaluation_order
    ]
    unhit_rule_codes = [
        code for code in evaluation_order if code not in hit_counts
    ]
    observed_conflicts = [
        {**_serialize_conflict_unit(unit), "observed_count": count}
        for unit, count in sorted(
            scenario_counts.items(), key=lambda kv: (_sort_key(kv[0]),)
        )
    ]

    representative_indexes = _select_representatives(indexed)

    return {
        "rule_coverage": rule_coverage,
        "unhit_rule_codes": unhit_rule_codes,
        "observed_conflicts": observed_conflicts,
        "coverage_unit_count": len({u for _i, units in indexed for u in units}),
        "representative_indexes": representative_indexes,
    }


def _select_representatives(
    indexed: list[tuple[int, set[CoverageUnit]]],
) -> list[int]:
    """确定性贪心集合覆盖：每轮新增最多，并列取最小下标，直至无新增。"""
    remaining = [(index, set(units)) for index, units in indexed if units]
    covered: set[CoverageUnit] = set()
    chosen: list[int] = []

    while remaining:
        best_index: int | None = None
        best_units: set[CoverageUnit] = set()
        best_gain = 0
        for index, units in remaining:  # 按下标升序扫描，天然实现并列取小
            gain = len(units - covered)
            if gain > best_gain:
                best_gain = gain
                best_index = index
                best_units = units
        if best_index is None:  # 没有任何项还能带来新增单元
            break
        chosen.append(best_index)
        covered |= best_units
        remaining = [(index, units) for index, units in remaining if index != best_index]

    return chosen


def _serialize_conflict_unit(unit: CoverageUnit) -> dict[str, Any]:
    if unit[0] == "field":
        return {
            "kind": "field",
            "group": None,
            "field": unit[1],
            "winner": unit[2],
            "previous_winner": unit[3],
            "suppressed_rule_codes": [],
        }
    return {
        "kind": "group",
        "group": unit[1],
        "field": None,
        "winner": unit[2],
        "previous_winner": None,
        "suppressed_rule_codes": list(unit[3]),
    }
