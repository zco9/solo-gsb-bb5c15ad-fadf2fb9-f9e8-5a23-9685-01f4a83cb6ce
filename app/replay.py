"""样本回放诊断引擎。

对一批分析样本之外的真实文档，分别用分析记录保存的**旧版 / 新版
schema** 逐个做 ``jsonschema`` 校验，并按兼容方向分类：

* 旧版合法、新版拒绝 -> ``backward``（向后破坏的实锤样本）；
* 新版合法、旧版拒绝 -> ``forward``（向前破坏的实锤样本）；
* 两版都合法 -> ``valid``；
* 两版都拒绝 -> ``invalid_both``（无法归因于本次 schema 演进，
  可能是脏数据或两版共有的约束）。

对于前两类单向失败，把每条校验错误**归因到分析报告中的某条 issue**
（direction + path + code 匹配）：

* 命中 -> 该 issue 的命中计数 +1（同一文档对同一条 issue 最多计一次）；
* 未命中 -> 记入 ``unattributed``，按（方向、原因、位置、规则、规则
  主体）归并，给出命中文档数与示例下标——说明该失败无法用报告中已有
  的 issue 解释（启发式降级 warning、分析器未覆盖的关键字或路径无法
  对齐）。

两版都拒绝的文档进入 ``unattributed``（``reason="rejected_by_both"``），
不参与方向归因；其余未命中错误的原因为 ``no_matching_issue``。

路径归因
========
报告 issue 的 ``path`` 是规则所在 **schema 节点**的 JSON Pointer，
``jsonschema`` 错误的 ``absolute_schema_path`` 指向触发关键字：

* 值约束类（type / minimum / enum / minItems / required /
  additionalProperties ...）：去掉 schema_path 末段关键字即节点
  （根级 required 的 schema_path 为 ``['required']``，节点为 ``#``）；
* 子 schema 为 ``false``：jsonschema 产出的错误不带实例 / schema 路径
  （descend 不补 path），引擎在 ``$ref`` 预解析后的 schema 与文档上
  并行下钻，按实例身份定位真正声明 false 的节点指针；
* oneOf / anyOf：递归展开错误树到叶子后按叶子规则归因；叶子路径含
  分支下标（``oneOf/0/...``），报告 issue 挂在组合节点上，因此深层
  叶子在精确路径无匹配时，回退到组合节点处的组合类 issue，其他规则
  不做跨节点回退，避免把深层失败错误归因给无关的祖先 issue。

引擎为纯函数，不读取数据库、不修改入参、不做任何持久化。
"""
from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any

from app.compat import escape_pointer, make_validator, resolve_refs
from app.profile import (
    MAX_EXAMPLE_INDICES,
    _rule_subject,
    render_instance_pointer,
    render_schema_pointer,
    rule_message,
)

# 单次回放允许的最大文档数（与 schemas.ReplayRequest 保持一致）。
MAX_REPLAY_DOCUMENTS = 5000

# 会产生"任一分支均失败"嵌套错误树的 jsonschema 关键字。
_COMPOSITION_VALIDATORS = ("oneOf", "anyOf")

# ---------------------------------------------------------------------------
# jsonschema 规则 -> 报告 issue code 候选
# ---------------------------------------------------------------------------
#
# 同一 jsonschema validator 可能对应多个分析器 code（如 enum 失败可能是
# 枚举收窄 enum_value_removed，也可能是新增枚举 enum_added），归因时按
# 报告中实际存在的 issue 依次尝试。
_VALIDATOR_CODES: dict[str, tuple[str, ...]] = {
    "required": ("required_added",),
    "additionalProperties": (
        "additional_properties_forbidden",
        "property_no_longer_allowed",
    ),
    "type": ("type_removed", "type_restricted", "integer_required"),
    "enum": ("enum_value_removed", "enum_added"),
    "const": ("const_added", "const_changed"),
    "minimum": ("minimum_increased",),
    "maximum": ("maximum_decreased",),
    "exclusiveMinimum": ("minimum_increased",),
    "exclusiveMaximum": ("maximum_decreased",),
    "multipleOf": ("multiple_of_added",),
    "minLength": ("min_length_increased",),
    "maxLength": ("max_length_decreased",),
    "pattern": ("pattern_changed",),
    "minItems": ("min_items_increased",),
    "maxItems": ("max_items_decreased",),
    "uniqueItems": ("unique_items_added",),
    "minProperties": ("min_properties_increased",),
    "maxProperties": ("max_properties_decreased",),
    "oneOf": ("oneOf_branch_missing", "oneOf_branch_rejected", "oneOf_added"),
    "anyOf": ("anyOf_branch_missing", "anyOf_branch_rejected", "anyOf_added"),
    "not": ("not_added",),
    "false": ("schema_forbids_all",),
}

# 创建分析时携带的真实样例产生的 issue code：仅在实例失败位置与该 issue
# 路径**精确一致**时作为最后兜底，不做祖先回退以免跨位置误归因。
_SAMPLE_FALLBACK_CODES = {
    "backward": "sample_rejected_by_new",
    "forward": "sample_rejected_by_old",
}


def _issue_subject(issue: dict) -> str:
    """提取报告 issue 原因中引用的规则主体（必填字段名 / 额外键名）。

    报告允许同一 (direction, path, code) 因**规则主体**不同而存在多条
    issue（如根级 required 同时新增 status 与 owner），归因时需要用主体
    进一步区分。消息形如 ``字段 'owner' 在新版中变为必填``。
    """
    quoted = re.findall(r"'([^']*)'", _normalize(issue.get("reason", "")))
    if issue.get("code") == "required_added" and quoted:
        return quoted[0]
    if issue.get("code") in (
        "additional_properties_forbidden",
        "property_no_longer_allowed",
    ):
        return ",".join(sorted(quoted))
    return ""


def _normalize(text: Any) -> str:
    if text is None:
        return ""
    return " ".join(str(text).split())


# ---------------------------------------------------------------------------
# false 子 schema 定位（绕过 jsonschema 不补 path 的缺陷）
# ---------------------------------------------------------------------------


def _find_false_pointers(schema: Any, document: Any) -> dict[int, list[str]]:
    """在 ``$ref`` 预解析后的 schema 与文档上并行下钻。

    返回 ``id(实例值) -> [声明 false 并拒绝该值的 schema 节点指针]``。
    同一实例身份被多处 false 节点命中时保留全部指针，归因时任选一个能
    匹配报告 issue 的。
    """
    resolved = resolve_refs(schema)
    found: dict[int, list[str]] = {}

    def record(pointer: str, value: Any) -> None:
        bucket = found.setdefault(id(value), [])
        if pointer not in bucket:
            bucket.append(pointer)

    def walk(node: Any, value: Any, pointer: str, seen: frozenset[int]) -> None:
        if node is True:
            return
        if node is False:
            record(pointer, value)
            return
        if not isinstance(node, dict) or id(node) in seen:
            return
        seen = seen | {id(node)}

        if isinstance(value, dict):
            properties = node.get("properties", {})
            for name, sub in properties.items():
                if name in value:
                    walk(sub, value[name],
                         pointer + "/properties/" + escape_pointer(str(name)), seen)
            pattern_props = node.get("patternProperties", {})
            matched_pattern_keys: set[str] = set()
            for pattern, sub in pattern_props.items():
                try:
                    regex = re.compile(pattern)
                except re.error:
                    continue
                for name in value:
                    if regex.search(str(name)):
                        matched_pattern_keys.add(name)
                        walk(sub, value[name],
                             pointer + "/patternProperties/"
                             + escape_pointer(str(pattern)), seen)
            ap = node.get("additionalProperties", True)
            if ap is not True:
                for name, item in value.items():
                    if name in properties or name in matched_pattern_keys:
                        continue
                    walk(ap, item, pointer + "/additionalProperties", seen)

        elif isinstance(value, list):
            prefix = node.get("prefixItems")
            legacy_tuple = False
            if not isinstance(prefix, list) and isinstance(node.get("items"), list):
                prefix = node["items"]  # draft-04/06/07 元组写法
                legacy_tuple = True
            if isinstance(prefix, list):
                for i, item in enumerate(value[:len(prefix)]):
                    walk(prefix[i], item, pointer + "/" + str(i), seen)
                if legacy_tuple:
                    rest = node.get("additionalItems", True)
                    for i in range(len(prefix), len(value)):
                        walk(rest, value[i], pointer + "/" + str(i), seen)
                else:
                    rest = node.get("items", True)
                    for i in range(len(prefix), len(value)):
                        walk(rest, value[i], pointer + "/" + str(i), seen)
            elif isinstance(node.get("items"), dict):
                for i, item in enumerate(value):
                    walk(node["items"], item, pointer + "/items", seen)

        sub = node.get("not")
        if sub is not None:
            walk(sub, value, pointer + "/not", seen)
        for keyword in ("anyOf", "oneOf", "allOf"):
            branches = node.get(keyword)
            if isinstance(branches, list):
                for i, branch in enumerate(branches):
                    walk(branch, value,
                         pointer + "/" + keyword + "/" + str(i), seen)
        if node.get("if") is not None:
            for keyword in ("then", "else"):
                branch = node.get(keyword)
                if branch is not None:
                    walk(branch, value, pointer + "/" + keyword, seen)

    walk(resolved, document, "#", frozenset())
    return found


# ---------------------------------------------------------------------------
# 错误展平与节点定位
# ---------------------------------------------------------------------------


def _is_composition(error: Any) -> bool:
    return error.validator in _COMPOSITION_VALIDATORS and bool(error.context)


def _collect_leaves(error: Any) -> list[Any]:
    """展开 oneOf/anyOf 错误树收集叶子；非组合错误（allOf / if 等）原样保留。"""
    if _is_composition(error):
        leaves: list[Any] = []
        for child in error.context:
            leaves.extend(_collect_leaves(child))
        return leaves
    return [error]


def _is_false_leaf(leaf: Any) -> bool:
    return leaf.validator is None or (
        leaf.schema is False and str(leaf.message).startswith("False schema")
    )


def _node_pointer(leaf: Any, false_pointers: list[str] | None = None) -> str:
    """把一条叶子校验错误映射到"规则所在 schema 节点"的 JSON Pointer。

    即分析器报告 issue.path 使用的位置（关键字段之前的节点）。
    """
    schema_path = list(leaf.absolute_schema_path)

    if _is_false_leaf(leaf):
        # jsonschema 对 false 子 schema 不补路径，优先使用预扫描得到的
        # false 节点指针；扫描未果时退回容器 + 实例末段推断。
        if false_pointers:
            return false_pointers[0]
        instance_path = list(leaf.absolute_path)
        if instance_path:
            return render_schema_pointer(schema_path + [instance_path[-1]])
        return render_schema_pointer(schema_path)

    # 其余规则：schema_path 末段是关键字名（required / type / minimum ...），
    # 去掉后即为该关键字所属的 schema 节点。
    if schema_path and not isinstance(schema_path[-1], int):
        return render_schema_pointer(schema_path[:-1])
    return render_schema_pointer(schema_path)


def _composition_base(pointer: str) -> tuple[str, str] | None:
    """若路径穿过 oneOf/anyOf 分支，返回 (组合节点指针, 组合关键字)。

    例如 ``#/properties/k/oneOf/0/properties/v`` ->
    ``('#/properties/k', 'oneOf')``。
    """
    if pointer == "#":
        return None
    segments = pointer[2:].split("/")
    for i, segment in enumerate(segments):
        if segment in _COMPOSITION_VALIDATORS:
            base = segments[:i]
            return ("#/" + "/".join(base) if base else "#", segment)
    return None


def _build_record(leaf: Any, false_pointers: list[str] | None = None) -> dict:
    """把一条 jsonschema 叶子错误转成归因 / 归并用记录。"""
    is_false = _is_false_leaf(leaf)
    validator = "false" if is_false else str(leaf.validator)
    message = (
        "子 schema 为 false，拒绝任何实例"
        if is_false
        else rule_message(leaf)
    )
    # 规则主体（required 缺失字段名 / 额外键名）按原始 validator 提取；
    # false 叶子没有关键字，主体为空。additionalProperties 一条错误可能
    # 携带多个未允许键名（"'x', 'y' were unexpected"），全部纳入主体，
    # 保证只带部分键的文档不会与带全部键的文档错并到同一组。
    if is_false:
        subject = ""
    elif str(leaf.validator) == "additionalProperties":
        quoted = re.findall(r"'([^']*)'", str(leaf.message))
        subject = ",".join(sorted(quoted))
    else:
        subject = _rule_subject(str(leaf.validator), leaf)
    return {
        "validator": validator,
        "node": _node_pointer(leaf, false_pointers),
        "alt_nodes": list(false_pointers[1:]) if false_pointers else [],
        "schema_pointer": render_schema_pointer(list(leaf.absolute_schema_path)),
        "instance_pointer": render_instance_pointer(list(leaf.absolute_path)),
        "subject": subject,
        "message": message,
    }


# ---------------------------------------------------------------------------
# 归因
# ---------------------------------------------------------------------------


def _index_issues(report: dict) -> dict[tuple[str, str, str, str], dict]:
    """把报告 issues 建成 (direction, path, code, subject) -> issue 的索引。"""
    index: dict[tuple[str, str, str, str], dict] = {}
    for issue in report.get("issues", []):
        index.setdefault(
            (issue["direction"], issue["path"], issue["code"],
             _issue_subject(issue)),
            issue,
        )
    return index


def _attribute(record: dict, direction: str,
               issue_index: dict[tuple[str, str, str, str], dict]) -> dict | None:
    """把一条叶子失败归因到报告 issue；无法归因返回 None。

    匹配顺序：

    1. 节点精确路径（false 子 schema 含多个候选指针时逐个尝试）×
       候选 code × 规则主体（缺失字段名 / 额外键名；主体为空的 issue
       作为通配兜底）；
    2. 失败路径穿过 oneOf/anyOf 分支时，在组合节点截断路径上尝试该
       组合关键字的全部候选 code——分析器只在组合节点上报分支不兼容，
       不递归分支内部，因此分支内的任何叶子失败都归因到组合 issue；
    3. 创建分析时样例 issue 的兜底 code（仅实例位置精确匹配）。
    """
    def lookup(pointer: str, code: str, subject: str) -> dict | None:
        # 主体精确匹配优先；报告 issue 主体为空时作为同 path/code 的通配
        hit = issue_index.get((direction, pointer, code, subject))
        if hit is not None:
            return hit
        if subject:
            return issue_index.get((direction, pointer, code, ""))
        return None

    codes = _VALIDATOR_CODES.get(record["validator"], ())
    subject = record["subject"]
    for pointer in [record["node"], *record["alt_nodes"]]:
        for code in codes:
            hit = lookup(pointer, code, subject)
            if hit is not None:
                return hit

    composition = _composition_base(record["node"])
    if composition is not None:
        base, keyword = composition
        for code in _VALIDATOR_CODES.get(keyword, ()):
            hit = lookup(base, code, subject)
            if hit is not None:
                return hit

    # 创建分析时携带样本产生的 issue 使用**实例位置**（如 #/arr），
    # 与结构 issue 的 schema 节点路径不同；按实例指针精确兜底。
    fallback = _SAMPLE_FALLBACK_CODES[direction]
    return lookup(record["instance_pointer"], fallback, "")


# ---------------------------------------------------------------------------
# 回放
# ---------------------------------------------------------------------------


def _evaluate_document(document: Any, old_validator: Any, new_validator: Any,
                       old_schema: Any, new_schema: Any,
                       issue_index: dict[tuple[str, str, str, str], dict]) -> dict:
    """用旧 / 新两版 schema 校验单篇文档并把失败叶子归因到报告 issue。

    供 ``replay_documents`` 与最小覆盖模块（``app.coverage``）共用，保证两
    条链路的四向分类与 issue 归因完全一致。返回：

    ``classification``
        ``valid`` / ``backward`` / ``forward`` / ``invalid_both`` 四者之一；
    ``hit_keys``
        该文档命中的报告 issue 身份集合
        ``(direction, path, code, subject)``，同一文档对同一 issue 最多一次；
    ``unattributed``
        无法归因的叶子列表 ``(归并键, 分组模板)``，已按文档内同键去重；
        计数与示例下标由调用方跨文档聚合。
    """
    old_errors = list(old_validator.iter_errors(document))
    new_errors = list(new_validator.iter_errors(document))
    old_ok, new_ok = not old_errors, not new_errors

    if old_ok and new_ok:
        return {"classification": "valid", "hit_keys": set(), "unattributed": []}

    if old_ok and not new_ok:
        classification, direction, failing_errors, failing_schema = (
            "backward", "backward", new_errors, new_schema)
    elif new_ok and not old_ok:
        classification, direction, failing_errors, failing_schema = (
            "forward", "forward", old_errors, old_schema)
    else:
        # 两版都拒绝：两侧叶子都作为无法归因错误收集
        classification, direction, failing_errors, failing_schema = (
            "invalid_both", None, old_errors + new_errors, None)

    # 先展平错误树；只有确实出现 false 子 schema 叶子（jsonschema 对其
    # 不补路径）时，才按文档做 schema 预扫描定位，避免正常文档的额外开销
    leaves: list[Any] = []
    for top_error in failing_errors:
        leaves.extend(_collect_leaves(top_error))

    false_map: dict[int, list[str]] = {}
    if any(_is_false_leaf(leaf) for leaf in leaves):
        if direction is None:
            for scanned in (_find_false_pointers(old_schema, document),
                            _find_false_pointers(new_schema, document)):
                for key, pointers in scanned.items():
                    bucket = false_map.setdefault(key, [])
                    bucket.extend(p for p in pointers if p not in bucket)
        else:
            false_map = _find_false_pointers(failing_schema, document)

    hit_keys: set[tuple[str, str, str, str]] = set()
    unattributed: list[tuple[tuple, dict]] = []
    unattr_keys_this_doc: set[tuple] = set()

    for leaf in leaves:
        is_false = _is_false_leaf(leaf)
        false_pointers = list(false_map.get(id(leaf.instance), [])) if is_false else None
        record = _build_record(leaf, false_pointers)
        leaf_dir = direction or "both"

        if direction is not None:
            issue = _attribute(record, direction, issue_index)
            if issue is not None:
                hit_keys.add((direction, issue["path"], issue["code"],
                              _issue_subject(issue)))
                continue
            reason = "no_matching_issue"
        else:
            reason = "rejected_by_both"

        un_key = (
            leaf_dir, reason, record["node"],
            record["validator"], record["subject"],
        )
        if un_key in unattr_keys_this_doc:
            continue
        unattr_keys_this_doc.add(un_key)
        unattributed.append((un_key, {
            "direction": leaf_dir,
            "reason": reason,
            "pointer": record["node"],
            "validator": record["validator"],
            "schema_pointer": record["schema_pointer"],
            "message": record["message"],
        }))

    return {"classification": classification,
            "hit_keys": hit_keys, "unattributed": unattributed}


def replay_documents(old_schema: Any, new_schema: Any, report: dict,
                     documents: list[Any]) -> dict:
    """用旧 / 新两版 schema 回放文档并归因到报告 issue（纯函数）。

    ``documents`` 由调用方保证为 1..MAX_REPLAY_DOCUMENTS 个任意 JSON 值。
    """
    old_validator = make_validator(old_schema)
    new_validator = make_validator(new_schema)
    issue_index = _index_issues(report)

    # issue 身份 (direction, path, code, subject) -> 命中聚合
    hits: OrderedDict[tuple[str, str, str, str], dict] = OrderedDict()
    # 未归因错误按 (direction, reason, node, validator, subject) 归并
    unattributed: OrderedDict[tuple, dict] = OrderedDict()

    counters = {"valid": 0, "backward": 0, "forward": 0, "invalid_both": 0}

    for index, document in enumerate(documents):
        evaluated = _evaluate_document(
            document, old_validator, new_validator,
            old_schema, new_schema, issue_index)
        counters[evaluated["classification"]] += 1

        for key in evaluated["hit_keys"]:
            bucket = hits.get(key)
            if bucket is None:
                hits[key] = {
                    "issue": issue_index[key],
                    "count": 1,
                    "example_indices": [index],
                }
            else:
                bucket["count"] += 1
                if len(bucket["example_indices"]) < MAX_EXAMPLE_INDICES:
                    bucket["example_indices"].append(index)

        for un_key, template in evaluated["unattributed"]:
            group = unattributed.get(un_key)
            if group is None:
                unattributed[un_key] = {
                    **template,
                    "count": 1,
                    "example_indices": [index],
                }
            else:
                group["count"] += 1
                if len(group["example_indices"]) < MAX_EXAMPLE_INDICES:
                    group["example_indices"].append(index)

    total = len(documents)
    return {
        "total_documents": total,
        "counts": counters,
        "issue_hits": _render_issue_hits(report, hits),
        "unattributed": sorted(
            unattributed.values(),
            key=lambda g: (g["direction"], g["reason"], g["pointer"],
                           g["validator"]),
        ),
    }


def _render_issue_hits(report: dict,
                       hits: dict[tuple[str, str, str, str], dict]) -> list[dict]:
    """按报告 issues 的原始顺序输出命中情况（未命中的 issue count=0）。

    同一业务身份 (direction, path, code, subject) 的重复报告项只输出一条。
    """
    rendered = []
    seen: set[tuple[str, str, str, str]] = set()
    for issue in report.get("issues", []):
        key = (issue["direction"], issue["path"], issue["code"],
               _issue_subject(issue))
        if key in seen:
            continue
        seen.add(key)
        bucket = hits.get(key)
        rendered.append({
            "direction": issue["direction"],
            "path": issue["path"],
            "code": issue["code"],
            "severity": issue["severity"],
            "count": bucket["count"] if bucket else 0,
            "example_indices": list(bucket["example_indices"]) if bucket else [],
        })
    return rendered
