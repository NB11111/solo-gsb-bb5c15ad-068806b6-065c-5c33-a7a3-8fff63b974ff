"""POST /api/rulesets/{id}/coverage 接口测试。

覆盖：命中汇总、未命中规则、组压制 / 字段覆盖场景、确定性贪心选样
（并列取下标、空单元永不入选）、失败项隔离、不落库、404 与 422。
"""

import pytest


def _conflict_groups_payload():
    return {
        "name": "conflicts",
        # discount 组继承 priority；coupon 组显式 first_hit；
        # r_first/r_later 无冲突组，走 collect_all 字段覆盖
        "default_conflict_policy": "priority",
        "rules": [
            # discount 组：priority，r_hi 胜出压制 r_lo
            {
                "code": "r_lo",
                "priority": 10,
                "conflict_group": "discount",
                "condition": 'doc["amount"] >= 1000',
                "action": '{"discount": 0.1}',
            },
            {
                "code": "r_hi",
                "priority": 20,
                "conflict_group": "discount",
                "condition": 'doc["amount"] >= 1000',
                "action": '{"discount": 0.2}',
            },
            # coupon 组：first_hit，拓扑序先者胜出
            {
                "code": "coupon_a",
                "conflict_group": "coupon",
                "conflict_policy": "first_hit",
                "condition": 'contains(doc["coupons"], "A")',
                "action": '{"coupon": "A"}',
            },
            {
                "code": "coupon_b",
                "conflict_group": "coupon",
                "conflict_policy": "first_hit",
                "condition": 'contains(doc["coupons"], "B")',
                "action": '{"coupon": "B"}',
            },
            # collect_all 同字段覆盖：r_later 覆盖 r_first
            {
                "code": "r_first",
                "condition": 'doc["need_flag"] == true',
                "action": '{"flag": "first"}',
            },
            {
                "code": "r_later",
                "condition": 'doc["need_flag"] == true',
                "action": '{"flag": "later"}',
            },
        ],
    }


def test_coverage_rule_hits_unhit_and_counts(client):
    payload = {
        "name": "simple",
        "rules": [
            {"code": "r1", "condition": 'doc["x"] >= 10',
             "action": '{"level": "high"}'},
            {"code": "r2", "condition": "r1.hit", "depends_on": ["r1"],
             "action": '{"bonus": 1}'},
            {"code": "r3", "condition": 'doc["x"] < 0'},
        ],
    }
    rid = client.post("/api/rulesets", json=payload).json()["ruleset_id"]

    resp = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={
            "version": 1,
            "documents": [{"x": 42}, {"x": 1}, {"x": 100}, {"x": -1}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["ruleset_id"] == rid
    assert body["version"] == 1
    assert body["total"] == 4
    assert body["success_count"] == 4
    assert body["error_count"] == 0
    assert [item["index"] for item in body["results"]] == [0, 1, 2, 3]
    # 成功项均不落库
    assert all(item["result"]["id"] == "" for item in body["results"])

    # 规则命中次数按 evaluation_order 汇总；被压制与否不影响命中统计
    assert body["rule_coverage"] == [
        {"code": "r1", "hit_count": 2, "covered": True},
        {"code": "r2", "hit_count": 2, "covered": True},
        {"code": "r3", "hit_count": 1, "covered": True},
    ]
    assert body["unhit_rule_codes"] == []

    # 全部失败/无命中场景时 unhit 为全集
    resp2 = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 1, "documents": [{"x": 5}]},
    )
    body2 = resp2.json()
    assert body2["unhit_rule_codes"] == ["r1", "r2", "r3"]
    assert body2["rule_coverage"] == [
        {"code": "r1", "hit_count": 0, "covered": False},
        {"code": "r2", "hit_count": 0, "covered": False},
        {"code": "r3", "hit_count": 0, "covered": False},
    ]


def test_coverage_group_and_field_conflict_units(client):
    rid = client.post("/api/rulesets", json=_conflict_groups_payload()).json()[
        "ruleset_id"
    ]
    resp = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={
            "version": 1,
            "documents": [
                {"amount": 2000, "coupons": ["A", "B"], "need_flag": True},
            ],
        },
    )
    body = resp.json()

    # 六条规则全部 fired（含被压制者）
    fired = body["results"][0]["result"]["fired_rule_codes"]
    assert fired == ["r_lo", "r_hi", "coupon_a", "coupon_b", "r_first", "r_later"]

    # 三个冲突场景：discount 组压制、coupon 组 first_hit、flag 字段覆盖
    assert body["observed_conflicts"] == [
        {
            "kind": "field",
            "group": None,
            "field": "flag",
            "winner": "r_later",
            "previous_winner": "r_first",
            "suppressed_rule_codes": [],
            "observed_count": 1,
        },
        {
            "kind": "group",
            "group": "coupon",
            "field": None,
            "winner": "coupon_a",
            "previous_winner": None,
            "suppressed_rule_codes": ["coupon_b"],
            "observed_count": 1,
        },
        {
            "kind": "group",
            "group": "discount",
            "field": None,
            "winner": "r_hi",
            "previous_winner": None,
            "suppressed_rule_codes": ["r_lo"],
            "observed_count": 1,
        },
    ]
    # 6 个规则单元 + 3 个冲突单元
    assert body["coverage_unit_count"] == 9
    # 单份文档覆盖全部单元，代表项只有下标 0
    assert body["representative_indexes"] == [0]


def test_coverage_observed_count_accumulates(client):
    rid = client.post("/api/rulesets", json=_conflict_groups_payload()).json()[
        "ruleset_id"
    ]
    body = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={
            "version": 1,
            "documents": [
                {"amount": 2000},
                {"amount": 3000},
                {"amount": 1},
            ],
        },
    ).json()
    discount = [
        c for c in body["observed_conflicts"] if c["group"] == "discount"
    ][0]
    assert discount["observed_count"] == 2
    assert body["rule_coverage"][0]["hit_count"] == 2  # r_lo
    assert body["rule_coverage"][1]["hit_count"] == 2  # r_hi
    # unhit：其余四条规则一次未命中，按 evaluation_order
    assert body["unhit_rule_codes"] == [
        "coupon_a",
        "coupon_b",
        "r_first",
        "r_later",
    ]


def test_coverage_greedy_picks_most_new_units_then_tie_smallest_index(client):
    payload = {
        "name": "independent",
        "rules": [
            {"code": "r1", "condition": 'doc["a"] == true'},
            {"code": "r2", "condition": 'doc["b"] == true'},
        ],
    }
    rid = client.post("/api/rulesets", json=payload).json()["ruleset_id"]

    # 下标 2 同时命中 r1/r2，第一轮新增最多直接覆盖全部单元
    body = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={
            "version": 1,
            "documents": [{"a": True}, {"b": True}, {"a": True, "b": True}],
        },
    ).json()
    assert body["representative_indexes"] == [2]
    assert body["coverage_unit_count"] == 2

    # 两份文档各覆盖一个且新增数相同：并列取最小下标，随后再补另一个
    body = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 1, "documents": [{"a": True}, {"b": True}]},
    ).json()
    assert body["representative_indexes"] == [0, 1]

    # 无命中的文档覆盖 0 个单元，永不入选
    body = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={
            "version": 1,
            "documents": [{}, {"a": True}, {}],
        },
    ).json()
    assert body["representative_indexes"] == [1]


def test_coverage_greedy_runs_until_no_new_units(client):
    # 三个互相独立的单元，三份文档各带一个：贪心三轮后无新增即停止
    payload = {
        "name": "three",
        "rules": [
            {"code": "r1", "condition": 'doc["a"] == true'},
            {"code": "r2", "condition": 'doc["b"] == true'},
            {"code": "r3", "condition": 'doc["c"] == true'},
        ],
    }
    rid = client.post("/api/rulesets", json=payload).json()["ruleset_id"]
    body = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={
            "version": 1,
            "documents": [{"a": True}, {"b": True}, {"c": True}, {}],
        },
    ).json()
    assert body["representative_indexes"] == [0, 1, 2]
    assert body["coverage_unit_count"] == 3


def test_coverage_failed_item_isolated_and_excluded_from_selection(
    client, monkeypatch
):
    from app.engine import EvaluationFailed
    import app.main as main_module

    real_evaluate = main_module.evaluate

    def flaky_evaluate(compiled, document):
        if document == {"boom": True}:
            raise EvaluationFailed(
                "NOT_CONVERGED", "模拟不收敛", {"max_iterations": 100}
            )
        return real_evaluate(compiled, document)

    monkeypatch.setattr(main_module, "evaluate", flaky_evaluate)

    payload = {
        "name": "simple",
        "rules": [
            {"code": "r1", "condition": 'doc["x"] >= 10',
             "action": '{"level": "high"}'},
        ],
    }
    rid = client.post("/api/rulesets", json=payload).json()["ruleset_id"]

    resp = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={
            "version": 1,
            # 失败项夹在中间：成功下标 0、2，失败下标 1
            "documents": [{"x": 42}, {"boom": True}, {"x": 100}, {"boom": True}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["total"] == 4
    assert body["success_count"] == 2
    assert body["error_count"] == 2

    # 逐项按原下标返回；失败项不中止整批
    items = body["results"]
    assert [i["index"] for i in items] == [0, 1, 2, 3]
    assert items[0]["status"] == "success"
    assert items[2]["status"] == "success"
    for fail_index in (1, 3):
        assert items[fail_index]["status"] == "error"
        assert items[fail_index]["result"] is None
        assert items[fail_index]["error"] == {
            "code": "NOT_CONVERGED",
            "message": "模拟不收敛",
            "details": {"max_iterations": 100},
        }

    # 汇总只基于成功项；失败项不参与代表选择
    assert body["rule_coverage"] == [
        {"code": "r1", "hit_count": 2, "covered": True}
    ]
    assert body["representative_indexes"] == [0]
    assert body["unhit_rule_codes"] == []

    # 全程不落库
    assert client.get(f"/api/rulesets/{rid}/evaluations").json() == []


def test_coverage_does_not_write_evaluations(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()[
        "ruleset_id"
    ]
    before = client.get(f"/api/rulesets/{rid}/evaluations").json()
    resp = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 1, "documents": [{"x": 42}, {"x": 1}]},
    )
    assert resp.status_code == 200
    after = client.get(f"/api/rulesets/{rid}/evaluations").json()
    assert before == after == []


def test_coverage_ruleset_not_found(client):
    resp = client.post(
        "/api/rulesets/nope/coverage",
        json={"version": 1, "documents": [{}]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_coverage_version_not_found(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()[
        "ruleset_id"
    ]
    resp = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 9, "documents": [{}]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VERSION_NOT_FOUND"


@pytest.mark.parametrize(
    "payload",
    [
        {"documents": [{}]},  # 缺 version
        {"version": 0, "documents": [{}]},  # version < 1
        {"version": 1, "documents": []},  # 空文档数组
    ],
)
def test_coverage_validates_request_body(client, simple_ruleset_payload, payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()[
        "ruleset_id"
    ]
    assert client.post(
        f"/api/rulesets/{rid}/coverage", json=payload
    ).status_code == 422


def test_coverage_rejects_more_than_100_documents(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()[
        "ruleset_id"
    ]
    resp = client.post(
        f"/api/rulesets/{rid}/coverage",
        json={"version": 1, "documents": [{}] * 101},
    )
    assert resp.status_code == 422


def test_coverage_demo_ruleset(client, demo_id):
    body = client.post(
        f"/api/rulesets/{demo_id}/coverage",
        json={
            "version": 1,
            "documents": [
                # 大额 VIP 北方：discount 组 vip_discount 胜出，且包邮
                {"amount": 1500, "customer": {"vip": True}, "region": "north"},
                # 两张券：coupon 组 first_hit，SAVE10 胜出
                {"coupons": ["SAVE10", "SAVE20"]},
                # 无任何命中
                {"amount": 50},
            ],
        },
    ).json()

    assert body["total"] == 3 and body["success_count"] == 3
    # 贪心：0 号新增最多（含 discount 组单元），再补 1 号（coupon 组单元），
    # 2 号零新增不入选
    assert body["representative_indexes"] == [0, 1]

    observed = {(c["kind"], c.get("group"), c["winner"],
                 tuple(c["suppressed_rule_codes"])) for c in body["observed_conflicts"]}
    assert observed == {
        ("group", "coupon", "coupon_save10", ("coupon_save20",)),
        ("group", "discount", "vip_discount", ("large_order",)),
    }
    # 被压制的命中也计入 fired 次数
    counts = {r["code"]: r["hit_count"] for r in body["rule_coverage"]}
    assert counts["large_order"] == 1
    assert counts["coupon_save20"] == 1
    assert counts["manual_review"] == 0
    assert body["unhit_rule_codes"] == ["manual_review"]
