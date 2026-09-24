"""基线对比引擎（app.compare）单元测试。"""
from app import compare


def make_issue(direction="backward", path="#", code="required_added",
               severity="error", message="m", reason="r"):
    return {
        "direction": direction, "path": path, "code": code,
        "severity": severity, "message": message, "reason": reason,
        "counterexample": None, "counterexample_path": None,
    }


def make_report(issues, backward_compatible=True, forward_compatible=True):
    def summarize(direction):
        subset = [i for i in issues if i["direction"] == direction]
        return {
            "errors": sum(1 for i in subset if i["severity"] == "error"),
            "warnings": sum(1 for i in subset if i["severity"] == "warning"),
            "total": len(subset),
        }

    return {
        "backward_compatible": backward_compatible,
        "forward_compatible": forward_compatible,
        "summary": {
            "backward": summarize("backward"),
            "forward": summarize("forward"),
            "total": len(issues),
            "errors": sum(1 for i in issues if i["severity"] == "error"),
            "warnings": sum(1 for i in issues if i["severity"] == "warning"),
        },
        "issues": issues,
    }


class TestNormalizeReason:
    def test_collapses_whitespace(self):
        assert compare.normalize_reason("  枚举值 \n 已移除\t") == "枚举值 已移除"

    def test_none_becomes_empty(self):
        assert compare.normalize_reason(None) == ""

    def test_strips_heuristic_suffix(self):
        from app.compat import HEURISTIC_SUFFIX
        base = "旧版未限制为整数，小数值在新版会被拒绝"
        assert compare.normalize_reason(base + HEURISTIC_SUFFIX) == compare.normalize_reason(base)


class TestIssueFingerprint:
    def test_stable_for_same_issue(self):
        issue = make_issue()
        assert compare.issue_fingerprint(issue) == compare.issue_fingerprint(dict(issue))

    def test_ignores_whitespace_jitter_in_reason(self):
        a = make_issue(reason="旧版允许类型 integer")
        b = make_issue(reason="  旧版允许类型\ninteger  ")
        assert compare.issue_fingerprint(a) == compare.issue_fingerprint(b)

    def test_ignores_heuristic_suffix(self):
        from app.compat import HEURISTIC_SUFFIX
        a = make_issue(reason="旧版未限制为整数，小数值在新版会被拒绝")
        b = make_issue(reason="旧版未限制为整数，小数值在新版会被拒绝" + HEURISTIC_SUFFIX)
        assert compare.issue_fingerprint(a) == compare.issue_fingerprint(b)

    def test_ignores_severity(self):
        a = make_issue(severity="error")
        b = make_issue(severity="warning")
        assert compare.issue_fingerprint(a) == compare.issue_fingerprint(b)

    def test_sensitive_to_direction_path_code_reason(self):
        base = make_issue()
        base_fp = compare.issue_fingerprint(base)
        assert compare.issue_fingerprint(make_issue(direction="forward")) != base_fp
        assert compare.issue_fingerprint(make_issue(path="#/properties/a")) != base_fp
        assert compare.issue_fingerprint(make_issue(code="type_removed")) != base_fp
        assert compare.issue_fingerprint(make_issue(reason="其他原因")) != base_fp

    def test_hex_prefix_format(self):
        fp = compare.issue_fingerprint(make_issue())
        assert len(fp) == 16
        int(fp, 16)  # 合法十六进制


class TestCompareReports:
    def test_classifies_all_four_categories(self):
        shared = make_issue(path="#/properties/a", code="min_length_increased",
                            reason="minLength 从 0 提高到 2")
        fixed_issue = make_issue(path="#", code="required_added",
                                 reason="旧版中 'b' 可以缺省")
        added_issue = make_issue(path="#", code="required_added",
                                 reason="旧版中 'c' 可以缺省")
        sev_base = make_issue(path="#", code="integer_required", severity="error",
                              reason="旧版未限制为整数")
        sev_cur = make_issue(path="#", code="integer_required", severity="warning",
                             reason="旧版未限制为整数")

        baseline = make_report([shared, fixed_issue, sev_base],
                               backward_compatible=False)
        current = make_report([shared, added_issue, sev_cur],
                              backward_compatible=False)

        result = compare.compare_reports(baseline, current)

        assert result["counts"] == {"added": 1, "fixed": 1, "persisting": 1,
                                    "severity_changed": 1}
        assert result["issues"]["added"][0]["reason"] == "旧版中 'c' 可以缺省"
        assert result["issues"]["fixed"][0]["reason"] == "旧版中 'b' 可以缺省"
        assert result["issues"]["persisting"][0]["code"] == "min_length_increased"
        changed = result["issues"]["severity_changed"][0]
        assert changed["baseline_severity"] == "error"
        assert changed["current_severity"] == "warning"
        assert changed["fingerprint"] == compare.issue_fingerprint(sev_base)

    def test_multiset_matching_by_occurrence(self):
        issue = make_issue()
        baseline = make_report([dict(issue), dict(issue)])  # 同指纹出现两次
        current = make_report([dict(issue)])
        result = compare.compare_reports(baseline, current)
        assert result["counts"]["persisting"] == 1
        assert result["counts"]["fixed"] == 1

    def test_identical_reports_all_persisting(self):
        report = make_report([make_issue(), make_issue(code="enum_value_removed")])
        result = compare.compare_reports(report, report)
        assert result["counts"]["persisting"] == 2
        assert result["counts"]["added"] == 0
        assert result["counts"]["fixed"] == 0
        assert result["counts"]["severity_changed"] == 0

    def test_compatibility_and_summary_diff(self):
        baseline = make_report(
            [make_issue(severity="error"), make_issue(code="x", severity="warning")],
            backward_compatible=False, forward_compatible=True)
        current = make_report([], backward_compatible=True, forward_compatible=True)
        result = compare.compare_reports(baseline, current)

        compat = result["compatibility"]
        assert compat["backward"] == {"baseline": False, "current": True, "changed": True}
        assert compat["forward"] == {"baseline": True, "current": True, "changed": False}

        diff = result["summary_diff"]
        assert diff["errors"] == {"baseline": 1, "current": 0, "delta": -1}
        assert diff["warnings"] == {"baseline": 1, "current": 0, "delta": -1}
        assert diff["total"] == {"baseline": 2, "current": 0, "delta": -2}
        assert diff["backward"]["errors"]["delta"] == -1
        assert diff["forward"]["errors"] == {"baseline": 0, "current": 0, "delta": 0}

    def test_does_not_mutate_reports(self):
        baseline = make_report([make_issue()])
        current = make_report([make_issue(code="other")])
        baseline_snapshot = repr(baseline)
        current_snapshot = repr(current)
        compare.compare_reports(baseline, current)
        assert repr(baseline) == baseline_snapshot
        assert repr(current) == current_snapshot

    def test_empty_reports(self):
        result = compare.compare_reports(make_report([]), make_report([]))
        assert result["counts"] == {"added": 0, "fixed": 0, "persisting": 0,
                                    "severity_changed": 0}
        assert result["summary_diff"]["total"]["delta"] == 0


class TestConditionalIssues:
    """条件分支 issue 的指纹与分类。"""

    def test_conditional_path_participates_in_fingerprint(self):
        base = make_issue(path="#/then", code="required_added")
        assert compare.issue_fingerprint(base) != compare.issue_fingerprint(
            make_issue(path="#/else", code="required_added"))
        assert compare.issue_fingerprint(base) != compare.issue_fingerprint(
            make_issue(path="#", code="required_added"))

    def test_conditional_severity_change_classified(self):
        from app.compat import HEURISTIC_SUFFIX
        reason = "旧版中 'y' 可以缺省，新版 required 包含该字段"
        baseline = make_report([make_issue(
            path="#/then", code="required_added", severity="warning",
            reason=reason + HEURISTIC_SUFFIX)])
        current = make_report([make_issue(
            path="#/then", code="required_added", severity="error", reason=reason)])
        result = compare.compare_reports(baseline, current)
        assert result["counts"]["severity_changed"] == 1
        changed = result["issues"]["severity_changed"][0]
        assert changed["path"] == "#/then"
        assert changed["baseline_severity"] == "warning"
        assert changed["current_severity"] == "error"
