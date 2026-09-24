"""发布门禁（release gate）评估。

对一份已生成的兼容性报告按调用方声明的门禁策略求值：

* ``directions``：需要检查的兼容方向（``backward`` / ``forward``），
  未声明的方向即使存在 issue 也不影响结论；
* 每个方向可声明 ``max_warnings``：未豁免 warning 数的允许上限；
* ``exemptions``：issue 豁免规则，规则由 ``direction``、``code``、``path``
  三个字段构成。``direction`` / ``code`` 必须字面相等；``path`` 为 JSON
  Pointer 风格（``#``、``#/properties/a``），**单段** ``*`` 可作为该段
  通配（如 ``#/properties/*``），跨段或仅匹配段的一部分都不算通配。

同一条 issue 命中多条规则时，按以下顺序选出唯一规则（后者覆盖前者）：

1. 精确字段数多者优先 —— path 不含 ``*`` 时 3 个字段全部精确（3），
   含通配时仅 direction / code 精确（2）；
2. path 字面段数多者优先 —— 通配段不计入字面段，``#`` 为 0 段；
3. 声明顺序靠后者优先（以 ``exemptions`` 数组下标为准）。

被豁免的 issue **不计数**，也不会成为阻断项；任一被检查方向存在
未豁免 error，或未豁免 warning 数超过该方向上限，门禁即失败。

整个评估过程只读报告，不修改原报告。
"""
from __future__ import annotations

# JSON Pointer 通配段：仅单个 "*" 才是单段通配。
_WILDCARD_SEGMENT = "*"


def pointer_segments(path: str) -> list[str]:
    """把 JSON Pointer 风格路径（``#/...``）拆成反转义后的字面段列表。

    与引擎 ``compat.render_path`` 对应：``~1`` -> ``/``、``~0`` -> ``~``。
    根路径 ``#`` 返回空列表。
    """
    if path == "#" or path == "":
        return []
    if not path.startswith("#/"):
        # 容错：也接受不带 # 前缀的裸 pointer
        if path.startswith("/"):
            path = "#" + path
        else:
            path = "#/" + path
    return [seg.replace("~1", "/").replace("~0", "~") for seg in path[2:].split("/")]


def path_matches(rule_path: str, issue_path: str) -> bool:
    """判断 issue 路径是否命中规则路径（支持单段 ``*`` 通配）。"""
    rule_segs = pointer_segments(rule_path)
    issue_segs = pointer_segments(issue_path)
    if len(rule_segs) != len(issue_segs):
        return False
    for rule_seg, issue_seg in zip(rule_segs, issue_segs):
        if rule_seg == _WILDCARD_SEGMENT:
            continue  # 单段通配：恰好匹配这一段
        if rule_seg != issue_seg:
            return False
    return True


def _rule_hits(rule: dict, issue: dict) -> bool:
    """direction / code 必须字面相等，path 按通配规则匹配。"""
    return (
        rule.get("direction") == issue.get("direction")
        and rule.get("code") == issue.get("code")
        and path_matches(rule.get("path", ""), issue.get("path", ""))
    )


def _exact_field_count(rule: dict) -> int:
    """精确匹配的字段数：path 含通配段时只有 direction/code 精确（2）。"""
    if any(seg == _WILDCARD_SEGMENT for seg in pointer_segments(rule.get("path", ""))):
        return 2
    return 3


def _literal_segment_count(rule: dict) -> int:
    """path 中字面段（非 ``*``）的数量，``#`` 为 0。"""
    return sum(1 for seg in pointer_segments(rule.get("path", ""))
               if seg != _WILDCARD_SEGMENT)


def select_rule(issue: dict, exemptions: list[dict]) -> int | None:
    """为一条 issue 选出唯一命中规则的声明下标；未命中返回 None。

    依次按精确字段数、path 字面段数、声明顺序（靠后者）排序取最大。
    """
    best_index: int | None = None
    best_key: tuple[int, int, int] | None = None
    for index, rule in enumerate(exemptions):
        if not _rule_hits(rule, issue):
            continue
        key = (_exact_field_count(rule), _literal_segment_count(rule), index)
        if best_key is None or key > best_key:
            best_key = key
            best_index = index
    return best_index


def _issue_sort_key(issue: dict) -> tuple:
    return (
        str(issue.get("direction") or ""),
        str(issue.get("path") or ""),
        str(issue.get("code") or ""),
        str(issue.get("severity") or ""),
    )


def _issue_entry(issue: dict) -> dict:
    """门禁结果中的 issue 条目（保留报告中的完整信息）。"""
    return {
        "direction": issue.get("direction"),
        "path": issue.get("path"),
        "code": issue.get("code"),
        "severity": issue.get("severity"),
        "message": issue.get("message"),
        "reason": issue.get("reason"),
        "counterexample": issue.get("counterexample"),
        "counterexample_path": issue.get("counterexample_path"),
    }


def evaluate_gate(report: dict, request: dict) -> dict:
    """对报告执行门禁评估，返回门禁结论（纯函数，不修改入参）。

    ``request`` 字段：

    * ``directions``: 待检查方向列表（去重，调用方应保证合法）；
    * ``max_warnings``: 可选的 ``{方向: 上限}``，默认上限 0；
    * ``exemptions``: 可选的豁免规则列表，默认无豁免。
    """
    directions: list[str] = list(dict.fromkeys(request.get("directions") or []))
    checked = set(directions)
    caps: dict[str, int] = {
        name: int((request.get("max_warnings") or {}).get(name, 0)) for name in directions
    }
    exemptions = request.get("exemptions") or []

    # 每个被检查方向：保留报告中该方向 issue 的稳定顺序（按 path/code/severity）。
    per_direction: dict[str, list[dict]] = {name: [] for name in directions}
    for issue in sorted(report.get("issues", []), key=_issue_sort_key):
        direction = issue.get("direction")
        if direction not in checked:
            continue
        per_direction[direction].append(issue)

    blocking: list[dict] = []
    exempted: list[dict] = []
    counts: dict[str, dict] = {}

    for name in directions:
        error_count = 0
        warning_count = 0
        exempt_count = 0
        allowed_warnings = 0
        cap = caps[name]

        for issue in per_direction[name]:
            rule_index = select_rule(issue, exemptions)
            if rule_index is not None:
                exempt_count += 1
                entry = _issue_entry(issue)
                entry["rule_index"] = rule_index
                exempted.append(entry)
                continue
            if issue.get("severity") == "error":
                error_count += 1
                entry = _issue_entry(issue)
                entry["blocked_reason"] = "error"
                blocking.append(entry)
            else:
                warning_count += 1
                # 未豁免 warning：按报告顺序计数，前 cap 条允许，其余超限。
                if allowed_warnings < cap:
                    allowed_warnings += 1
                else:
                    overflow = _issue_entry(issue)
                    overflow["blocked_reason"] = "warning_limit_exceeded"
                    blocking.append(overflow)

        counts[name] = {
            "errors": error_count,
            "warnings": warning_count,
            "exempted": exempt_count,
            "total": len(per_direction[name]),
            "max_warnings": cap,
        }

    return {
        "pass": not blocking,
        "counts": counts,
        "blocking": blocking,
        "exempted": exempted,
    }
