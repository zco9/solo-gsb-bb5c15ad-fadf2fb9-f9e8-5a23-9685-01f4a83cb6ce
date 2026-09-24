"""分析报告基线对比。

核心思路
========
两次分析（基线 baseline 与当前 current）的报告各自包含一组 issue。
为了判断"同一个问题"在两份报告中的状态，为每条 issue 生成**稳定指纹**：

* 指纹输入：方向（direction）、schema 路径（path）、问题代码（code）
  以及**规范化后的原因**（normalized reason）；
* 规范化：剥离启发式降级后缀（error 降级为 warning 时引擎追加的
  说明文字），并压缩全部连续空白为单个空格、去除首尾空白，使指纹对
  严重级别抖动与格式差异免疫，但对实质内容（路径、取值描述）保持敏感；
* 指纹算法：SHA-256（字段间以 ``\\x1f`` 分隔避免拼接歧义），取前 16 个
  十六进制字符。指纹不包含 severity，因此同一问题仅严重级别变化时
  指纹保持不变，可被识别为"仅严重级别变化"。

分类规则（同一指纹按出现次数做多重集配对）：

* ``added``：仅出现在当前报告 —— 新增问题；
* ``fixed``：仅出现在基线报告 —— 已修复问题；
* ``persisting``：两份报告都存在且严重级别相同 —— 仍存在；
* ``severity_changed``：两份报告都存在但严重级别不同 —— 仅严重级别变化。

对比过程只读取两份报告，绝不修改原始分析记录。
"""
from __future__ import annotations

import hashlib
from collections import deque
from typing import Any

from app.compat import HEURISTIC_SUFFIX

_FINGERPRINT_LEN = 16
_FIELD_SEP = "\x1f"


def normalize_reason(reason: Any) -> str:
    """规范化 issue 原因文本。

    * 剥离启发式降级后缀（``HEURISTIC_SUFFIX``）：同一问题在 error 与
      warning 之间变化时引擎会追加 / 移除该后缀，保留它会让
      "仅严重级别变化"的问题被误判为两个不同问题；
    * 压缩全部连续空白为单个空格并去除首尾空白，使指纹对换行 / 缩进
      等格式抖动免疫，但对取值、路径等实质内容保持敏感。
    """
    if reason is None:
        return ""
    text = " ".join(str(reason).split())
    suffix = " ".join(HEURISTIC_SUFFIX.split())
    if text.endswith(suffix):
        text = text[: -len(suffix)]
    return text


def issue_fingerprint(issue: dict) -> str:
    """生成稳定 issue 指纹（direction + path + code + 规范化 reason）。"""
    key = _FIELD_SEP.join([
        str(issue.get("direction", "")),
        str(issue.get("path", "")),
        str(issue.get("code", "")),
        normalize_reason(issue.get("reason", "")),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:_FINGERPRINT_LEN]


def _issue_entry(issue: dict) -> dict:
    """从报告 issue 中提取对比条目（附指纹，不含反例等大字段）。"""
    return {
        "fingerprint": issue_fingerprint(issue),
        "direction": issue.get("direction"),
        "path": issue.get("path"),
        "code": issue.get("code"),
        "severity": issue.get("severity"),
        "message": issue.get("message"),
        "reason": issue.get("reason"),
    }


def _sort_key(entry: dict) -> tuple:
    return (
        str(entry.get("direction") or ""),
        str(entry.get("path") or ""),
        str(entry.get("code") or ""),
        str(entry.get("fingerprint") or ""),
    )


def _metric_diff(baseline: int, current: int) -> dict:
    return {"baseline": baseline, "current": current, "delta": current - baseline}


def _summary_diff(baseline_summary: dict, current_summary: dict) -> dict:
    def direction_diff(name: str) -> dict:
        base_dir = baseline_summary.get(name, {}) or {}
        cur_dir = current_summary.get(name, {}) or {}
        return {
            "errors": _metric_diff(base_dir.get("errors", 0), cur_dir.get("errors", 0)),
            "warnings": _metric_diff(base_dir.get("warnings", 0), cur_dir.get("warnings", 0)),
            "total": _metric_diff(base_dir.get("total", 0), cur_dir.get("total", 0)),
        }

    return {
        "errors": _metric_diff(baseline_summary.get("errors", 0), current_summary.get("errors", 0)),
        "warnings": _metric_diff(baseline_summary.get("warnings", 0), current_summary.get("warnings", 0)),
        "total": _metric_diff(baseline_summary.get("total", 0), current_summary.get("total", 0)),
        "backward": direction_diff("backward"),
        "forward": direction_diff("forward"),
    }


def _compatibility_diff(baseline_report: dict, current_report: dict) -> dict:
    def direction(name: str) -> dict:
        base = bool(baseline_report.get(f"{name}_compatible"))
        cur = bool(current_report.get(f"{name}_compatible"))
        return {"baseline": base, "current": cur, "changed": base != cur}

    return {"backward": direction("backward"), "forward": direction("forward")}


def compare_reports(baseline_report: dict, current_report: dict) -> dict:
    """对比两份分析报告，返回问题分类与汇总差异（纯函数，不修改入参）。"""
    # 基线指纹 -> 待配对 issue 队列（同一指纹可能出现多次，按多重集配对）
    unmatched: dict[str, deque] = {}
    for issue in baseline_report.get("issues", []):
        unmatched.setdefault(issue_fingerprint(issue), deque()).append(issue)

    added: list[dict] = []
    fixed: list[dict] = []
    persisting: list[dict] = []
    severity_changed: list[dict] = []

    for issue in current_report.get("issues", []):
        fingerprint = issue_fingerprint(issue)
        queue = unmatched.get(fingerprint)
        if not queue:
            added.append(_issue_entry(issue))
            continue
        baseline_issue = queue.popleft()
        if baseline_issue.get("severity") == issue.get("severity"):
            persisting.append(_issue_entry(issue))
        else:
            entry = _issue_entry(issue)
            entry["baseline_severity"] = baseline_issue.get("severity")
            entry["current_severity"] = issue.get("severity")
            severity_changed.append(entry)

    for queue in unmatched.values():
        for issue in queue:
            fixed.append(_issue_entry(issue))

    added.sort(key=_sort_key)
    fixed.sort(key=_sort_key)
    persisting.sort(key=_sort_key)
    severity_changed.sort(key=_sort_key)

    return {
        "compatibility": _compatibility_diff(baseline_report, current_report),
        "summary_diff": _summary_diff(
            baseline_report.get("summary", {}) or {},
            current_report.get("summary", {}) or {},
        ),
        "issues": {
            "added": added,
            "fixed": fixed,
            "persisting": persisting,
            "severity_changed": severity_changed,
        },
        "counts": {
            "added": len(added),
            "fixed": len(fixed),
            "persisting": len(persisting),
            "severity_changed": len(severity_changed),
        },
    }
