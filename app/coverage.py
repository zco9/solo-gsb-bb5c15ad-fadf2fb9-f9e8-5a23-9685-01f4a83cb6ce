"""回放样本最小覆盖（minimum replay coverage）选择引擎。

在 :mod:`app.replay` 的双版校验与 issue 归因之上，解决"从一大批真实回放
文档中挑出**尽可能少的代表文档**，使报告里的每条可被样本触发的 issue
至少被一篇代表文档命中"的问题——即经典的**集合覆盖**：

* 宇宙（universe）：分析报告中、方向在筛选范围内的全部 issue（按报告
  顺序去重后的业务身份 ``(direction, path, code, subject)``）；
* 每篇文档经旧 / 新两版 schema 校验与叶子归因（复用
  ``replay._evaluate_document``），得到它能覆盖的 issue 身份集合；
* 用**确定性贪心集合覆盖**逐轮选出代表文档，直到没有文档能再带来新增
  覆盖。

贪心每轮的排序键为：

1. **新增覆盖数**（相对已覆盖集合的边际收益），大者优先——
   不考虑文档的历史总覆盖数，避免重复计算已覆盖 issue；
2. **文档索引**（提交顺序下标），小者优先——并列时选择最早提交的
   文档，保证结果对同一输入完全确定。

只覆盖"零边际收益 issue"的文档（valid / 两版都拒绝 / 仅命中已覆盖
issue）不会被选中。提交的文档无法命中的 issue 出现在
``uncovered_issues`` 中。

引擎为纯函数，不读取数据库、不修改入参、不做任何持久化。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.compat import make_validator
from app.replay import (
    MAX_REPLAY_DOCUMENTS,
    _evaluate_document,
    _index_issues,
    _issue_subject,
)

# 单次最小覆盖选择允许的最大文档数（与 schemas.ReplayCoverageRequest 一致）。
MAX_COVERAGE_DOCUMENTS = MAX_REPLAY_DOCUMENTS

# 合法的方向筛选值（与报告 issue.direction 一致）。
DIRECTIONS = ("backward", "forward")


def _normalize_directions(directions: Iterable[str]) -> list[str]:
    """按固定先后顺序去重方向筛选；出现非法方向时抛 ValueError。"""
    chosen = [name for name in DIRECTIONS if name in set(directions)]
    unknown = [name for name in directions if name not in DIRECTIONS]
    if unknown:
        raise ValueError(f"未知兼容方向: {sorted(set(unknown))}")
    return chosen


def _issue_identity(issue: dict) -> tuple[str, str, str, str]:
    return (issue["direction"], issue["path"], issue["code"],
            _issue_subject(issue))


def select_minimum_coverage(old_schema: Any, new_schema: Any, report: dict,
                            documents: list[Any],
                            directions: Iterable[str] = DIRECTIONS) -> dict:
    """用确定性贪心集合覆盖选择覆盖报告 issue 的最少代表文档（纯函数）。

    ``documents`` 由调用方保证为 1..MAX_COVERAGE_DOCUMENTS 个任意 JSON 值；
    ``directions`` 需为 ``backward`` / ``forward`` 的非空去重序列。
    """
    selected_directions = _normalize_directions(directions)
    direction_set = set(selected_directions)

    old_validator = make_validator(old_schema)
    new_validator = make_validator(new_schema)
    issue_index = _index_issues(report)

    # 宇宙：筛选方向内、按报告顺序去重后的 issue 身份（保留首次出现的 issue）
    universe: list[tuple[str, str, str, str]] = []
    universe_set: set[tuple[str, str, str, str]] = set()
    for issue in report.get("issues", []):
        if issue["direction"] not in direction_set:
            continue
        identity = _issue_identity(issue)
        if identity in universe_set:
            continue
        universe_set.add(identity)
        universe.append(identity)

    # 逐篇文档复用回放的分类与归因，只保留落在方向宇宙内的命中身份
    classifications: list[str] = []
    cover_sets: list[set[tuple[str, str, str, str]]] = []
    counts = {"valid": 0, "backward": 0, "forward": 0, "invalid_both": 0}
    for document in documents:
        evaluated = _evaluate_document(
            document, old_validator, new_validator,
            old_schema, new_schema, issue_index)
        classifications.append(evaluated["classification"])
        counts[evaluated["classification"]] += 1
        cover_sets.append(evaluated["hit_keys"] & universe_set)

    # 确定性贪心：每轮在全部文档中取（新增覆盖数最大、索引最小）者；
    # 新增覆盖为 0 的文档永不入选，循环必然终止。
    covered: set[tuple[str, str, str, str]] = set()
    selected: list[dict] = []
    while True:
        best_index: int | None = None
        best_gain = 0
        best_cover: set[tuple[str, str, str, str]] = set()
        for index, cover in enumerate(cover_sets):
            gain = len(cover - covered)
            if gain > best_gain:
                best_gain, best_index, best_cover = gain, index, cover
        if best_index is None:
            break
        covered |= best_cover
        selected.append({
            "index": best_index,
            "classification": classifications[best_index],
            "newly_covered_issues": best_gain,
            "covered_issues": len(best_cover),
        })

    selected_indices = [item["index"] for item in selected]

    # covered_by：每条被覆盖 issue 的代表文档下标（升序）
    covered_by: dict[tuple[str, str, str, str], list[int]] = {}
    for identity in universe:
        covered_by[identity] = []
    for index in selected_indices:
        for identity in cover_sets[index]:
            # identity ∈ covered 时一定已登记
            covered_by[identity].append(index)

    covered_issues: list[dict] = []
    uncovered_issues: list[dict] = []
    for identity in universe:
        issue = issue_index[identity]
        entry = {
            "direction": issue["direction"],
            "path": issue["path"],
            "code": issue["code"],
            "severity": issue["severity"],
            "reason": issue["reason"],
        }
        if identity in covered:
            covered_issues.append({**entry, "covered_by": covered_by[identity]})
        else:
            uncovered_issues.append(entry)

    return {
        "total_documents": len(documents),
        "directions": selected_directions,
        "counts": counts,
        "total_issues": len(universe),
        "covered_issue_count": len(covered_issues),
        "uncovered_issue_count": len(uncovered_issues),
        "selected_indices": selected_indices,
        "selected_documents": selected,
        "covered_issues": covered_issues,
        "uncovered_issues": uncovered_issues,
    }
