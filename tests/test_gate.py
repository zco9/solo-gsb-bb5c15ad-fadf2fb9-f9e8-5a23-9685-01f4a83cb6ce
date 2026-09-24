"""发布门禁引擎（app.gate）单元测试。"""
import pytest

from app import gate


def make_issue(direction="backward", path="#", code="required_added",
               severity="error", message="m", reason="r"):
    return {
        "direction": direction, "path": path, "code": code,
        "severity": severity, "message": message, "reason": reason,
        "counterexample": None, "counterexample_path": None,
    }


def make_report(issues):
    def summarize(direction):
        subset = [i for i in issues if i["direction"] == direction]
        return {
            "errors": sum(1 for i in subset if i["severity"] == "error"),
            "warnings": sum(1 for i in subset if i["severity"] == "warning"),
            "total": len(subset),
        }

    return {
        "backward_compatible": False,
        "forward_compatible": False,
        "summary": {
            "backward": summarize("backward"),
            "forward": summarize("forward"),
            "total": len(issues),
            "errors": sum(1 for i in issues if i["severity"] == "error"),
            "warnings": sum(1 for i in issues if i["severity"] == "warning"),
        },
        "issues": issues,
    }


class TestPointerSegments:
    def test_root(self):
        assert gate.pointer_segments("#") == []

    def test_simple_segments(self):
        assert gate.pointer_segments("#/properties/a") == ["properties", "a"]

    def test_unescape(self):
        # 与 compat.escape_pointer 对应
        assert gate.pointer_segments("#/properties/a~1b~0c") == ["properties", "a/b~c"]

    def test_wildcard_is_kept_literal_token(self):
        assert gate.pointer_segments("#/properties/*") == ["properties", "*"]


class TestPathMatches:
    @pytest.mark.parametrize("rule_path,issue_path,expected", [
        ("#", "#", True),
        ("#/properties/a", "#/properties/a", True),
        ("#/properties/a", "#/properties/b", False),
        ("#/properties/*", "#/properties/a", True),
        ("#/properties/*", "#/properties/a/b", False),       # * 只匹配单段
        ("#/*/a", "#/properties/a", True),
        ("#/*/*", "#/properties/a", True),
        ("#/*", "#", False),                                  # 通配段必须匹配一段
        ("#/properties/*", "#/items", False),                 # 字面段必须一致
        ("#/properties/a*", "#/properties/ab", False),        # 段内 * 不是通配
        ("#/properties/*/items", "#/properties/x/items", True),
        ("#/properties/*/items", "#/properties/x/y/items", False),
    ])
    def test_cases(self, rule_path, issue_path, expected):
        assert gate.path_matches(rule_path, issue_path) is expected


class TestSelectRule:
    def test_no_match_returns_none(self):
        issue = make_issue(code="required_added", path="#")
        assert gate.select_rule(issue, [
            {"direction": "forward", "code": "required_added", "path": "#"},
            {"direction": "backward", "code": "other", "path": "#"},
            {"direction": "backward", "code": "required_added", "path": "#/properties/a"},
        ]) is None

    def test_direction_and_code_must_be_exact(self):
        issue = make_issue(direction="backward", code="required_added", path="#/properties/a")
        # 即使 path 通配命中，direction / code 不一致也不豁免
        assert gate.select_rule(issue, [
            {"direction": "forward", "code": "required_added", "path": "#/properties/*"},
            {"direction": "backward", "code": "type_removed", "path": "#/properties/*"},
        ]) is None

    def test_exact_path_beats_wildcard(self):
        # 精确字段数：字面 path（3）优先于通配 path（2），即使通配规则声明更晚
        issue = make_issue(path="#/properties/a", code="required_added")
        rules = [
            {"direction": "backward", "code": "required_added", "path": "#/properties/*"},
            {"direction": "backward", "code": "required_added", "path": "#/properties/a"},
            {"direction": "backward", "code": "required_added", "path": "#/**/a"},
        ]
        # "#/**/a" 中没有单段 "*"，三段全字面，但根本不命中（* 在段内不是通配）
        assert gate.select_rule(issue, rules) == 1

    def test_more_literal_segments_wins(self):
        # 都含通配（精确字段数并列 2），字面段多者优先
        issue = make_issue(path="#/properties/a/items", code="type_removed")
        rules = [
            {"direction": "backward", "code": "type_removed", "path": "#/properties/*/items"},
            {"direction": "backward", "code": "type_removed", "path": "#/*/*/items"},
        ]
        assert gate.select_rule(issue, rules) == 0

    def test_declaration_order_breaks_tie(self):
        # 完全等价的两条规则，声明顺序靠后者胜出
        issue = make_issue(path="#/properties/a", code="required_added")
        rules = [
            {"direction": "backward", "code": "required_added", "path": "#/properties/*"},
            {"direction": "backward", "code": "required_added", "path": "#/properties/*"},
        ]
        assert gate.select_rule(issue, rules) == 1

    def test_root_vs_wildcard_same_depth_literal_count(self):
        # "#" 是 3 精确字段（0 字面段）；"#/*" 是 2 精确字段，精确字段数优先
        issue = make_issue(path="#", code="required_added")
        rules = [
            {"direction": "backward", "code": "required_added", "path": "#/*"},
            {"direction": "backward", "code": "required_added", "path": "#"},
        ]
        assert gate.select_rule(issue, rules) == 1


class TestEvaluateGate:
    def test_clean_report_passes(self):
        result = gate.evaluate_gate(make_report([]), {"directions": ["backward"]})
        assert result["pass"] is True
        assert result["blocking"] == []
        assert result["exempted"] == []
        assert result["counts"]["backward"] == {
            "errors": 0, "warnings": 0, "exempted": 0, "total": 0,
            "max_warnings": 0,
        }

    def test_unexempted_error_blocks(self):
        report = make_report([make_issue(severity="error")])
        result = gate.evaluate_gate(report, {"directions": ["backward"]})
        assert result["pass"] is False
        assert len(result["blocking"]) == 1
        assert result["blocking"][0]["blocked_reason"] == "error"
        assert result["counts"]["backward"]["errors"] == 1

    def test_exempted_error_does_not_count_or_block(self):
        report = make_report([make_issue(code="required_added", path="#")])
        result = gate.evaluate_gate(report, {
            "directions": ["backward"],
            "exemptions": [{"direction": "backward", "code": "required_added", "path": "#"}],
        })
        assert result["pass"] is True
        assert result["blocking"] == []
        assert result["counts"]["backward"]["errors"] == 0
        assert result["counts"]["backward"]["exempted"] == 1
        assert result["exempted"][0]["rule_index"] == 0

    def test_warning_within_cap_passes(self):
        report = make_report([
            make_issue(path="#/properties/a", code="pattern_changed", severity="warning"),
            make_issue(path="#/properties/b", code="pattern_changed", severity="warning"),
        ])
        result = gate.evaluate_gate(report, {
            "directions": ["backward"], "max_warnings": {"backward": 2},
        })
        assert result["pass"] is True
        assert result["counts"]["backward"]["warnings"] == 2
        assert result["blocking"] == []

    def test_warning_over_cap_blocks_in_report_order(self):
        report = make_report([
            make_issue(path="#/properties/a", code="pattern_changed", severity="warning"),
            make_issue(path="#/properties/b", code="pattern_changed", severity="warning"),
        ])
        result = gate.evaluate_gate(report, {
            "directions": ["backward"], "max_warnings": {"backward": 1},
        })
        assert result["pass"] is False
        blocked = result["blocking"]
        assert [b["path"] for b in blocked] == ["#/properties/b"]
        assert blocked[0]["blocked_reason"] == "warning_limit_exceeded"
        assert result["counts"]["backward"]["warnings"] == 2

    def test_default_cap_is_zero(self):
        report = make_report([make_issue(severity="warning")])
        result = gate.evaluate_gate(report, {"directions": ["backward"]})
        assert result["pass"] is False
        assert result["blocking"][0]["blocked_reason"] == "warning_limit_exceeded"

    def test_unchecked_direction_ignored(self):
        report = make_report([
            make_issue(direction="backward", severity="error"),
            make_issue(direction="forward", severity="error"),
        ])
        # 只检查 forward：backward 的 error 不影响结论
        result = gate.evaluate_gate(report, {"directions": ["forward"]})
        assert result["pass"] is False
        assert [b["direction"] for b in result["blocking"]] == ["forward"]
        assert set(result["counts"]) == {"forward"}

    def test_exemption_wildcard_path(self):
        report = make_report([
            make_issue(path="#/properties/a", code="pattern_changed", severity="warning"),
            make_issue(path="#/properties/b", code="pattern_changed", severity="warning"),
            make_issue(path="#/properties/a", code="required_added", severity="error"),
        ])
        result = gate.evaluate_gate(report, {
            "directions": ["backward"],
            "exemptions": [
                {"direction": "backward", "code": "pattern_changed",
                 "path": "#/properties/*"},
            ],
        })
        # 两条 warning 被通配豁免；required_added error 未命中仍阻断
        assert result["pass"] is False
        assert result["counts"]["backward"]["exempted"] == 2
        assert [e["path"] for e in result["exempted"]] == [
            "#/properties/a", "#/properties/b",
        ]
        assert all(e["rule_index"] == 0 for e in result["exempted"])
        assert result["blocking"][0]["code"] == "required_added"

    def test_exemption_rule_priority(self):
        # issue 同时命中通配规则（下标 0）与精确规则（下标 1），必须记到精确规则
        report = make_report([
            make_issue(path="#/properties/a", code="required_added", severity="error"),
        ])
        result = gate.evaluate_gate(report, {
            "directions": ["backward"],
            "exemptions": [
                {"direction": "backward", "code": "required_added",
                 "path": "#/properties/*"},
                {"direction": "backward", "code": "required_added",
                 "path": "#/properties/a"},
            ],
        })
        assert result["exempted"][0]["rule_index"] == 1

    def test_error_and_overflow_warning_both_block(self):
        report = make_report([
            make_issue(path="#", code="required_added", severity="error"),
            make_issue(path="#/properties/a", code="pattern_changed", severity="warning"),
        ])
        result = gate.evaluate_gate(report, {"directions": ["backward"]})
        assert result["pass"] is False
        reasons = {b["blocked_reason"] for b in result["blocking"]}
        assert reasons == {"error", "warning_limit_exceeded"}

    def test_exempted_warning_does_not_consume_cap(self):
        report = make_report([
            make_issue(path="#/properties/a", code="pattern_changed", severity="warning"),
            make_issue(path="#/properties/b", code="pattern_changed", severity="warning"),
        ])
        result = gate.evaluate_gate(report, {
            "directions": ["backward"],
            "max_warnings": {"backward": 1},
            "exemptions": [
                {"direction": "backward", "code": "pattern_changed",
                 "path": "#/properties/a"},
            ],
        })
        # a 被豁免不计数不占额度；b 是唯一计数 warning，cap=1 足够
        assert result["pass"] is True
        assert result["counts"]["backward"] == {
            "errors": 0, "warnings": 1, "exempted": 1, "total": 2,
            "max_warnings": 1,
        }

    def test_direction_duplicates_collapsed(self):
        report = make_report([make_issue(severity="warning")])
        result = gate.evaluate_gate(report, {"directions": ["backward", "backward"]})
        assert set(result["counts"]) == {"backward"}

    def test_does_not_mutate_inputs(self):
        report = make_report([make_issue(severity="error")])
        request = {"directions": ["backward"], "exemptions": []}
        report_repr = repr(report)
        request_repr = repr(request)
        gate.evaluate_gate(report, request)
        assert repr(report) == report_repr
        assert repr(request) == request_repr


class TestConditionalPaths:
    """条件分支（if/then/else）路径的门禁匹配。"""

    def test_then_path_exact_exemption(self):
        report = make_report([
            make_issue(path="#/then", code="required_added", severity="error"),
        ])
        result = gate.evaluate_gate(report, {
            "directions": ["backward"],
            "exemptions": [
                {"direction": "backward", "code": "required_added", "path": "#/then"},
            ],
        })
        assert result["pass"] is True
        assert result["exempted"][0]["rule_index"] == 0
        assert result["counts"]["backward"]["exempted"] == 1

    def test_branch_wildcard_does_not_match_deeper_segment(self):
        # 单段 * 不匹配 #/then/properties/x 这种更深的条件分支路径
        assert not gate.path_matches("#/then/*", "#/then/properties/x")
        assert gate.path_matches("#/then/*/*", "#/then/properties/x")
        assert gate.path_matches("#/if", "#/if")
