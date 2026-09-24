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

分类规则（同一指纹内按严重级别做多重集配对）：

* ``added``：仅出现在当前报告 —— 新增问题；
* ``fixed``：仅出现在基线报告 —— 已修复问题；
* ``persisting``：两份报告都存在且严重级别相同 —— 仍存在；
* ``severity_changed``：两份报告都存在但严重级别不同 —— 仅严重级别变化。

同一指纹的配对分两阶段进行：**先**把严重级别相同的记录两两配为
``persisting``（如基线 ``error, warning`` 与当前 ``warning, error`` 应得到
两条 ``persisting``，而非按出现顺序误配成两条 ``severity_changed``）；
**再**把双方剩余记录跨严重级别配为 ``severity_changed``；最后基线多出的
归入 ``fixed``、当前多出的归入 ``added``。

对比过程只读取两份报告，绝不修改原始分析记录。
"""
from __future__ import annotations

import hashlib
from collections import defaultdict, deque
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
    # 每个指纹按严重级别分桶（保留报告中的出现顺序，桶内按多重集配对）
    baseline_groups: dict[str, dict[Any, deque]] = defaultdict(
        lambda: defaultdict(deque))
    current_groups: dict[str, dict[Any, deque]] = defaultdict(
        lambda: defaultdict(deque))
    for issue in baseline_report.get("issues", []):
        baseline_groups[issue_fingerprint(issue)][issue.get("severity")].append(issue)
    for issue in current_report.get("issues", []):
        current_groups[issue_fingerprint(issue)][issue.get("severity")].append(issue)

    added: list[dict] = []
    fixed: list[dict] = []
    persisting: list[dict] = []
    severity_changed: list[dict] = []

    # 指纹与严重级别均排序遍历，保证配对选择与输出顺序稳定
    for fingerprint in sorted(set(baseline_groups) | set(current_groups)):
        baseline_by_sev = baseline_groups.get(fingerprint, {})
        current_by_sev = current_groups.get(fingerprint, {})
        severities = sorted(set(baseline_by_sev) | set(current_by_sev),
                            key=lambda sev: str(sev))

        # 阶段一：相同严重级别优先配为 persisting
        for severity in severities:
            same_base = baseline_by_sev.get(severity)
            same_current = current_by_sev.get(severity)
            if not same_base or not same_current:
                continue
            while same_base and same_current:
                same_base.popleft()
                persisting.append(_issue_entry(same_current.popleft()))

        # 阶段二：双方剩余记录严重级别集合必然不相交（相同级别的桶至少
        # 有一方已在阶段一清空），跨严重级别逐条配为 severity_changed
        leftover_base = [baseline_by_sev[sev] for sev in severities
                         if baseline_by_sev.get(sev)]
        leftover_current = [current_by_sev[sev] for sev in severities
                            if current_by_sev.get(sev)]
        bi = ci = 0
        while bi < len(leftover_base) and ci < len(leftover_current):
            base_bucket = leftover_base[bi]
            cur_bucket = leftover_current[ci]
            baseline_issue = base_bucket.popleft()
            current_issue = cur_bucket.popleft()
            entry = _issue_entry(current_issue)
            entry["baseline_severity"] = baseline_issue.get("severity")
            entry["current_severity"] = current_issue.get("severity")
            severity_changed.append(entry)
            if not base_bucket:
                bi += 1
            if not cur_bucket:
                ci += 1

        # 基线多出的为 fixed，当前多出的为 added
        fixed.extend(_issue_entry(issue)
                     for bucket in leftover_base for issue in bucket)
        added.extend(_issue_entry(issue)
                     for bucket in leftover_current for issue in bucket)

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
