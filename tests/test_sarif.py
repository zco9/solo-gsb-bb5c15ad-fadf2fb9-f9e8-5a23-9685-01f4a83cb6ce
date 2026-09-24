"""SARIF 2.1.0 导出测试：纯函数转换 + 端到端接口。"""
import json
import os
import tempfile

# 在导入应用前指定临时数据库
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test_sarif.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.sarif import build_sarif  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


OLD_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled", "pending"]},
        "a": {"type": "string", "pattern": "^x"},
    },
}
NEW_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled"]},
        "a": {"type": "string", "pattern": "^xx"},
        "owner": {"type": "string"},
    },
    "required": ["status", "owner"],
}


def _create(client):
    resp = client.post("/api/analyses", json={
        "name": "sarif-case",
        "old_schema": OLD_SCHEMA,
        "new_schema": NEW_SCHEMA,
        "samples": [{"status": "pending"}],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 纯函数 build_sarif
# ---------------------------------------------------------------------------

def _issue(direction="backward", path="#/properties/a", code="pattern_changed",
           severity="warning", message="新版正则约束为 '^xx'",
           reason="旧版 pattern 为 '^x'"):
    return {
        "direction": direction,
        "path": path,
        "code": code,
        "severity": severity,
        "message": message,
        "reason": reason,
        "counterexample": None,
        "counterexample_path": None,
    }


def _analysis(issues):
    return {
        "id": 1,
        "name": "t",
        "report": {"issues": issues},
    }


def test_sarif_envelope():
    doc = build_sarif(_analysis([]))
    assert doc["version"] == "2.1.0"
    assert doc["$schema"].startswith("https://")
    run = doc["runs"][0]
    assert run["tool"]["driver"]["name"]
    assert run["results"] == []
    assert run["logicalLocations"] == []
    assert run["tool"]["driver"]["rules"] == []


def test_issue_to_result_mapping():
    doc = build_sarif(_analysis([_issue()]))
    result = doc["runs"][0]["results"][0]
    assert result["ruleId"] == "pattern_changed"
    assert result["level"] == "warning"
    assert result["message"]["text"] == "新版正则约束为 '^xx'"
    # direction 不属于 SARIF 标准字段，放入 properties
    assert result["properties"]["direction"] == "backward"

    # schema path 作为 logicalLocation，通过 index 引用 run 级去重表
    loc = result["locations"][0]["logicalLocations"][0]
    run = doc["runs"][0]
    assert run["logicalLocations"][loc["index"]]["fullyQualifiedName"] == "#/properties/a"
    assert run["logicalLocations"][loc["index"]]["name"] == "a"
    assert loc["fullyQualifiedName"] == "#/properties/a"


def test_severity_maps_to_level():
    doc = build_sarif(_analysis([_issue(severity="error", code="required_added", path="#")]))
    assert doc["runs"][0]["results"][0]["level"] == "error"


def test_rules_dedup_by_code_and_sorted():
    doc = build_sarif(_analysis([
        _issue(code="required_added", severity="error", path="#"),
        _issue(code="pattern_changed", severity="warning", path="#/properties/a"),
        _issue(code="pattern_changed", severity="error", path="#/properties/b"),
    ]))
    rules = doc["runs"][0]["tool"]["driver"]["rules"]
    assert [r["id"] for r in rules] == ["pattern_changed", "required_added"]
    by_id = {r["id"]: r for r in rules}
    # 同 code 同时出现 error / warning 时，rule 默认级别取最高（error）
    assert by_id["pattern_changed"]["defaultConfiguration"]["level"] == "error"
    assert by_id["required_added"]["defaultConfiguration"]["level"] == "error"
    # 已知 code 带人类可读短描述
    assert by_id["required_added"]["shortDescription"]["text"]


def test_logical_locations_dedup_and_sorted():
    doc = build_sarif(_analysis([
        _issue(path="#/properties/b"),
        _issue(path="#/properties/a"),
        _issue(path="#/properties/b"),  # 重复 path
    ]))
    locations = doc["runs"][0]["logicalLocations"]
    assert [loc["fullyQualifiedName"] for loc in locations] == [
        "#/properties/a", "#/properties/b",
    ]
    for result in doc["runs"][0]["results"]:
        ref = result["locations"][0]["logicalLocations"][0]
        assert locations[ref["index"]]["fullyQualifiedName"] == ref["fullyQualifiedName"]


def test_results_sorted_by_stable_key():
    doc = build_sarif(_analysis([
        _issue(direction="forward", path="#/properties/z", code="required_added",
               severity="error", reason="r-f"),
        _issue(direction="backward", path="#/properties/m", code="enum_value_removed",
               severity="error", reason="r-b1"),
        _issue(direction="backward", path="#/properties/a", code="enum_value_removed",
               severity="error", reason="r-b2"),
    ]))
    results = doc["runs"][0]["results"]
    keys = [
        (r["properties"]["direction"], r["ruleId"],
         r["locations"][0]["logicalLocations"][0]["fullyQualifiedName"])
        for r in results
    ]
    assert keys == sorted(keys)
    assert keys[0][0] == "backward"
    assert keys[1][2] == "#/properties/m"
    assert keys[-1][0] == "forward"


def test_duplicate_issues_kept_as_separate_results():
    issue = _issue()
    doc = build_sarif(_analysis([issue, dict(issue)]))
    results = doc["runs"][0]["results"]
    assert len(results) == 2
    fp0 = results[0]["partialFingerprints"]["businessIdentity/v1"]
    fp1 = results[1]["partialFingerprints"]["businessIdentity/v1"]
    assert fp0 == fp1
    assert len(fp0) == 16


def test_partial_fingerprint_stable_when_message_changes():
    doc1 = build_sarif(_analysis([_issue(message="措辞 A")]))
    doc2 = build_sarif(_analysis([_issue(message="完全不同的措辞 B")]))
    fp1 = doc1["runs"][0]["results"][0]["partialFingerprints"]["businessIdentity/v1"]
    fp2 = doc2["runs"][0]["results"][0]["partialFingerprints"]["businessIdentity/v1"]
    assert fp1 == fp2


def test_partial_fingerprint_changes_when_business_key_changes():
    base = build_sarif(_analysis([_issue(path="#/properties/a")]))
    other_path = build_sarif(_analysis([_issue(path="#/properties/b")]))
    other_code = build_sarif(_analysis([_issue(code="required_added", path="#")]))
    fp = base["runs"][0]["results"][0]["partialFingerprints"]["businessIdentity/v1"]
    assert other_path["runs"][0]["results"][0]["partialFingerprints"][
        "businessIdentity/v1"] != fp
    assert other_code["runs"][0]["results"][0]["partialFingerprints"][
        "businessIdentity/v1"] != fp


def test_root_path_logical_location_name():
    doc = build_sarif(_analysis([_issue(path="#", code="required_added")]))
    assert doc["runs"][0]["logicalLocations"][0]["name"] == "#"


# ---------------------------------------------------------------------------
# GET /api/analyses/{id}/sarif
# ---------------------------------------------------------------------------

def test_sarif_endpoint(client):
    analysis = _create(client)
    resp = client.get(f"/api/analyses/{analysis['id']}/sarif")
    assert resp.status_code == 200
    assert resp.headers["content-type"].split(";")[0] == "application/sarif+json"

    doc = resp.json()
    run = doc["runs"][0]
    expected_issues = analysis["report"]["issues"]
    assert len(run["results"]) == len(expected_issues)

    rules = run["tool"]["driver"]["rules"]
    expected_codes = sorted({i["code"] for i in expected_issues})
    assert [r["id"] for r in rules] == expected_codes

    # 每条 result 都能按 ruleId 找到对应 rule，并带稳定业务指纹
    rule_ids = {r["id"] for r in rules}
    for result in run["results"]:
        assert result["ruleId"] in rule_ids
        assert result["level"] in ("error", "warning")
        assert result["properties"]["direction"] in ("backward", "forward")
        assert result["partialFingerprints"]["businessIdentity/v1"]
        ref = result["locations"][0]["logicalLocations"][0]
        assert run["logicalLocations"][ref["index"]]["fullyQualifiedName"] == \
            ref["fullyQualifiedName"]

    # results 按稳定键排序
    keys = [
        (r["properties"]["direction"], r["ruleId"],
         r["locations"][0]["logicalLocations"][0]["fullyQualifiedName"],
         r["level"], r["partialFingerprints"]["businessIdentity/v1"])
        for r in run["results"]
    ]
    assert keys == sorted(keys)

    # JSON 可被标准解析器解析
    json.loads(resp.content)


def test_sarif_endpoint_missing_returns_404(client):
    resp = client.get("/api/analyses/999999/sarif")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "分析记录不存在"


def test_sarif_does_not_modify_report(client):
    analysis = _create(client)
    resp = client.get(f"/api/analyses/{analysis['id']}/sarif")
    assert resp.status_code == 200
    after = client.get(f"/api/analyses/{analysis['id']}").json()
    assert after["report"] == analysis["report"]


def test_if_condition_changed_rule_description():
    doc = build_sarif(_analysis([
        _issue(code="if_condition_changed", path="#/if", severity="warning",
               message="新版 if 条件发生变化"),
    ]))
    rules = doc["runs"][0]["tool"]["driver"]["rules"]
    assert [r["id"] for r in rules] == ["if_condition_changed"]
    assert rules[0]["shortDescription"]["text"]
    # 条件路径作为逻辑位置导出
    locations = doc["runs"][0]["logicalLocations"]
    assert [loc["fullyQualifiedName"] for loc in locations] == ["#/if"]
    assert locations[0]["name"] == "if"
