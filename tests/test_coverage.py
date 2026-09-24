"""回放样本最小覆盖引擎单元测试。

覆盖：贪心集合覆盖选择（边际收益优先、多轮选择）、并列按新增覆盖数与
文档索引排序、方向筛选、无法覆盖 issue、原始四向分类计数、确定性与
纯函数（不修改入参）。
"""
import copy

import pytest

from app import compat, coverage

# ---------------------------------------------------------------------------
# 共用夹具：两个根级必填字段 a / b，文档可分别覆盖其中之一或同时覆盖
# ---------------------------------------------------------------------------

OLD = {"type": "object",
       "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
NEW = {"type": "object",
       "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
       "required": ["a", "b"]}


def _issue_codes_by_subject(report, direction="backward"):
    return {
        (i["path"], i["code"], i["reason"]): i
        for i in report["issues"] if i["direction"] == direction
    }


def test_greedy_prefers_document_covering_more_issues():
    report = compat.analyze(OLD, NEW)
    # doc0 同时覆盖缺 a / 缺 b 两条 issue；doc1、doc2 各覆盖一条
    docs = [{"a": "x"}, {"b": "y"}, {}]
    result = coverage.select_minimum_coverage(OLD, NEW, report, docs)

    assert result["selected_indices"] == [2]
    assert result["total_issues"] == 2
    assert result["covered_issue_count"] == 2
    assert result["uncovered_issue_count"] == 0
    selected = result["selected_documents"][0]
    assert selected["index"] == 2
    assert selected["classification"] == "backward"
    assert selected["newly_covered_issues"] == 2
    assert selected["covered_issues"] == 2


def test_greedy_runs_multiple_rounds_and_counts_marginal_gain():
    report = compat.analyze(OLD, NEW)
    # 没有任何一篇文档同时缺 a 和 b：需要两篇代表才能覆盖两条 issue
    docs = [
        {"a": "x"},                    # 0 只覆盖缺 b
        {"a": "x", "b": "y"},          # 1 valid，零覆盖，不应入选
        {"b": "y"},                    # 2 只覆盖缺 a
        42,                            # 3 两版都拒绝，零覆盖，不应入选
    ]
    result = coverage.select_minimum_coverage(OLD, NEW, report, docs)

    assert result["selected_indices"] == [0, 2]
    first, second = result["selected_documents"]
    assert first["newly_covered_issues"] == 1
    assert first["covered_issues"] == 1
    # 第二轮选中 doc2 时仅剩一条未覆盖 issue
    assert second["newly_covered_issues"] == 1
    assert result["covered_issue_count"] == 2
    assert result["uncovered_issues"] == []
    # 原始分类统计不随选择改变
    assert result["counts"] == {
        "valid": 1, "backward": 2, "forward": 0, "invalid_both": 1,
    }
    assert result["total_documents"] == 4


def test_tie_broken_by_document_index():
    report = compat.analyze(OLD, NEW)
    # doc0/doc1/doc3 都只缺 b（覆盖能力相同，并列），doc2 只缺 a
    docs = [{"a": "1"}, {"a": "2"}, {"b": "z"}, {"a": "4"}]
    result = coverage.select_minimum_coverage(OLD, NEW, report, docs)

    # 第一轮四篇文档新增覆盖并列为 1，取最小索引 doc0；
    # 第二轮只有 doc2 覆盖另一条 issue（缺 a），入选
    assert result["selected_indices"] == [0, 2]

    # 所有文档覆盖能力完全相同：每轮并列都取剩余中索引最小者
    docs_same = [{"a": str(i)} for i in range(5)]
    result_same = coverage.select_minimum_coverage(OLD, NEW, report, docs_same)
    assert result_same["selected_indices"] == [0]
    assert result_same["selected_documents"][0]["newly_covered_issues"] == 1


def test_uncovered_issues_when_no_document_hits_them():
    report = compat.analyze(OLD, NEW)
    # 全部候选文档只缺 b（缺 a 的 issue 无法被覆盖）
    docs = [{"a": "x"}, {"a": "y"}]
    result = coverage.select_minimum_coverage(OLD, NEW, report, docs)

    assert result["selected_indices"] == [0]
    assert result["covered_issue_count"] == 1
    assert result["uncovered_issue_count"] == 1
    uncovered = result["uncovered_issues"]
    assert len(uncovered) == 1
    assert uncovered[0]["code"] == "required_added"
    assert "'a'" in uncovered[0]["reason"]
    assert uncovered[0]["direction"] == "backward"
    # 被覆盖的 issue 带 covered_by
    covered = result["covered_issues"]
    assert len(covered) == 1
    assert "'b'" in covered[0]["reason"]
    assert covered[0]["covered_by"] == [0]


def test_covered_by_lists_all_selected_documents_ascending():
    report = compat.analyze(OLD, NEW)
    # 三篇文档都缺 a 与 b（覆盖同一对 issue），贪心只应选最早的一篇；
    # covered_by 只包含被选中的代表文档，而不是全部命中文档
    docs = [{}, {}, {}]
    result = coverage.select_minimum_coverage(OLD, NEW, report, docs)
    assert result["selected_indices"] == [0]
    for issue in result["covered_issues"]:
        assert issue["covered_by"] == [0]

    # 多篇代表共同覆盖同一 issue 时 covered_by 升序完整列出
    docs2 = [{}, {"a": "x"}]
    result2 = coverage.select_minimum_coverage(OLD, NEW, report, docs2)
    # doc0 一轮覆盖两条；doc1 没有新增覆盖 -> 不入选
    assert result2["selected_indices"] == [0]


def test_direction_filter_restricts_universe():
    # 旧版必填 email、新版删除 -> forward issue；新版同时新增必填 name
    old = {"type": "object", "required": ["email"], "properties": {
        "email": {"type": "string"}, "name": {"type": "string"}}}
    new = {"type": "object", "required": ["name"], "properties": {
        "email": {"type": "string"}, "name": {"type": "string"}}}
    report = compat.analyze(old, new)
    assert {i["direction"] for i in report["issues"]} == {"backward", "forward"}

    # {"name": "n"} 缺 email：新版合法、旧版拒绝 -> forward
    # {"email": "e"} 缺 name：旧版合法、新版拒绝 -> backward
    docs = [{"name": "n"}, {"email": "e"}, {"email": "e", "name": "n"}]

    forward_only = coverage.select_minimum_coverage(
        old, new, report, docs, directions=["forward"])
    assert forward_only["directions"] == ["forward"]
    assert forward_only["total_issues"] == 1
    assert forward_only["selected_indices"] == [0]
    assert forward_only["counts"]["forward"] == 1
    # 选中文档的分类仍是原始分类（forward），不被筛选方向篡改
    assert forward_only["selected_documents"][0]["classification"] == "forward"

    backward_only = coverage.select_minimum_coverage(
        old, new, report, docs, directions=["backward"])
    assert backward_only["directions"] == ["backward"]
    assert backward_only["total_issues"] == 1
    assert backward_only["selected_indices"] == [1]


def test_direction_filter_skips_documents_with_no_relevant_hits():
    # 只有 backward issue；筛选 forward 时宇宙为空，任何文档都不入选
    report = compat.analyze(OLD, NEW)
    result = coverage.select_minimum_coverage(
        OLD, NEW, report, [{}, {"a": "x"}], directions=["forward"])
    assert result["total_issues"] == 0
    assert result["selected_indices"] == []
    assert result["selected_documents"] == []
    assert result["covered_issues"] == []
    assert result["uncovered_issues"] == []
    # 原始分类统计照常输出
    assert result["counts"]["backward"] == 2


def test_directions_deduped_and_normalized():
    report = compat.analyze(OLD, NEW)
    result = coverage.select_minimum_coverage(
        OLD, NEW, report, [{}], directions=["forward", "backward", "forward"])
    assert result["directions"] == ["backward", "forward"]

    with pytest.raises(ValueError):
        coverage.select_minimum_coverage(
            OLD, NEW, report, [{}], directions=["sideways"])


def test_no_representative_when_all_documents_valid_or_invalid_both():
    report = compat.analyze(OLD, NEW)
    docs = [{"a": "x", "b": "y"}, 42, "str"]
    result = coverage.select_minimum_coverage(OLD, NEW, report, docs)
    assert result["selected_indices"] == []
    assert result["covered_issue_count"] == 0
    assert result["uncovered_issue_count"] == 2
    assert result["counts"] == {
        "valid": 1, "backward": 0, "forward": 0, "invalid_both": 2,
    }


def test_result_is_deterministic_and_order_dependent_only_via_indices():
    report = compat.analyze(OLD, NEW)
    docs = [{"a": "0"}, {"a": "1"}, {"b": "2"}]
    first = coverage.select_minimum_coverage(OLD, NEW, report, docs)
    second = coverage.select_minimum_coverage(OLD, NEW, report, list(docs))
    assert first == second

    # 打乱输入顺序：选中的仍是等价位置上的文档，但下标随提交顺序变化；
    # 覆盖到的 issue 集合保持一致
    reordered = [docs[2], docs[0], docs[1]]
    result = coverage.select_minimum_coverage(OLD, NEW, report, reordered)
    assert result["selected_indices"] == [0, 1]
    assert result["covered_issue_count"] == 2


def test_does_not_mutate_inputs():
    report = compat.analyze(OLD, NEW)
    docs = [{"a": "x"}, {"b": "y"}, {}]
    old_copy, new_copy, report_copy, docs_copy = (
        copy.deepcopy(OLD), copy.deepcopy(NEW),
        copy.deepcopy(report), copy.deepcopy(docs),
    )
    coverage.select_minimum_coverage(OLD, NEW, report, docs)
    assert OLD == old_copy
    assert NEW == new_copy
    assert report == report_copy
    assert docs == docs_copy
