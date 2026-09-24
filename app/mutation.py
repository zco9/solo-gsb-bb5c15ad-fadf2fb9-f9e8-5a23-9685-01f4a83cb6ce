"""样本变异覆盖（mutation coverage）评估引擎。

针对一次分析已保存的**新 schema**（``new_schema``）确定性地生成一批
**单点变异体（mutants）**——每个变异体只改动 schema 上的一处约束，
且改动方向要么收紧（拒绝更多实例）、要么放松（接受更多实例）：

收紧类（tightening，正向样本可发现）
------------------------------------
* ``enum_narrowed``          枚举删除一个取值；
* ``minimum_increased``      数值下界提高（含排他边界 / draft-04 布尔写法）；
* ``maximum_decreased``      数值上界降低；
* ``type_narrowed``          多类型声明删除一个类型；
* ``min_length_increased`` / ``max_length_decreased`` 字符串长度边界收紧；
* ``min_items_increased`` / ``max_items_decreased``     数组长度边界收紧；
* ``min_properties_increased`` / ``max_properties_decreased``
                              对象属性数量边界收紧。

放松类（loosening，负向样本可发现）
-----------------------------------
* ``required_removed``       必填字段列表删除一个字段。

杀死判定
========
逐变异、逐文档用 ``jsonschema`` 校验：**同一文档在原 schema 与变异体上
的校验结果不同**，即该变异体被这篇文档"杀死（killed）"。

* 收紧类变异由"原 schema 合法、变异体拒绝"的文档发现；
* 放松类变异（删除必填项）由"原 schema 拒绝、变异体接受"的文档发现
  ——因此提交的文档既可以是正向样本，也可以是缺少必填字段等负向样本。

等价变异剔除
============
单点改动不一定产生语义差异（例如 ``type: ["integer", "number"]`` 删除
``integer`` 后仍接受全部整数；或枚举唯一取值同时被其他关键字禁止）。
引擎在变异所在的** schema 节点局部**构造一个区分见证（discriminating
witness）并双向校验：收紧类要求"原节点合法、变异节点拒绝"，放松类
要求"原节点拒绝、变异节点合法"；无法构造出见证的变异视为等价变异，
不计入分母，避免拉低本不应存在的覆盖率。节点局部见证是尽力而为的
快速过滤（跨节点的 allOf / if 交互无法在局部体现），最终是否可被样本
发现仍以完整 schema 上的逐文档校验为准。

确定性与上限
============
* 变异只依赖 schema 本身：同一份 schema 生成的变异体集合、ID、描述与
  顺序完全确定（按规则、节点指针、变异主体稳定排序）；
* 每类规则独立截断到 ``max_per_rule``（缺省 50，0 表示关闭该类变异），
  防止大 schema 产生无界变异；
* 变异体 ID 为 ``rule + 节点指针 + 变异主体`` 的 SHA-256 前 12 位。

引擎为纯函数，不读取数据库、不修改入参、不做任何持久化。
"""
from __future__ import annotations

import copy
import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from app.compat import (
    _lower_bound,
    _min_number,
    _upper_bound,
    escape_pointer,
    jsonish,
    make_validator,
    minimal_instance,
    minimal_object,
    resolve_refs,
    typed_fragment,
    validator_class_for,
)

_MUT_ID_LEN = 12
_FIELD_SEP = "\x1f"

# 单次评估允许的最大文档数（与 schemas.MutationCoverageRequest 一致）。
MAX_MUTATION_DOCUMENTS = 5000

# 每类变异的缺省生成上限。
DEFAULT_MAX_PER_RULE = 50

# 单类变异允许声明的上限（防止恶意 / 误操作提交产生无界校验开销）。
MAX_RULE_LIMIT = 10000

# 杀死变异的示例下标最大保留数量（仅控制响应体积，不影响计数）。
MAX_KILL_EXAMPLE_INDICES = 20

# 支持的变异规则（顺序即稳定输出顺序）。
RULE_NAMES: tuple[str, ...] = (
    "required_removed",
    "enum_narrowed",
    "minimum_increased",
    "maximum_decreased",
    "type_narrowed",
    "min_length_increased",
    "max_length_decreased",
    "min_items_increased",
    "max_items_decreased",
    "min_properties_increased",
    "max_properties_decreased",
)


# ---------------------------------------------------------------------------
# 变异描述
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MutationSpec:
    """一条待生成的单点变异（在变异生成阶段完成等价性预检）。"""

    rule: str
    pointer: str
    subject: str
    description: str
    # 在 schema 深拷贝上施加这一处单点编辑
    apply: Callable[[dict], None]
    # 节点局部区分见证：(原节点是否应合法, 见证实例)
    witness: Any
    witness_originally_valid: bool


def _mutation_id(rule: str, pointer: str, subject: str) -> str:
    key = _FIELD_SEP.join([rule, pointer, subject])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:_MUT_ID_LEN]


def _render_pointer(tokens: tuple[str, ...]) -> str:
    if not tokens:
        return "#"
    return "#/" + "/".join(escape_pointer(t) for t in tokens)


def _node_types_number(node: dict) -> set[str]:
    raw = node.get("type")
    if isinstance(raw, list):
        return set(raw)
    if isinstance(raw, str):
        return {raw}
    return set()


def _fractional_fragment(node: dict) -> Any:
    """构造一个满足节点其他数值约束的小数值（尽力而为，失败由等价预检过滤）。"""
    lo, lo_exclusive = _lower_bound(node)
    hi, _ = _upper_bound(node)
    candidates = [0.5, 1.5, 0.1]
    base = float(_min_number(node, integer=False))
    for value in (base, base + 0.5):
        candidates.append(value + (0.5 if lo_exclusive else 0))
    if lo is not None:
        candidates.append(lo + (0.5 if lo_exclusive else 0.5))
    multiple = node.get("multipleOf")
    for value in candidates:
        if lo is not None and (value < lo or (lo_exclusive and value <= lo)):
            continue
        if hi is not None and value >= hi:
            continue
        if isinstance(multiple, (int, float)) and multiple > 0:
            if abs(value / multiple - round(value / multiple)) > 1e-9:
                continue
        return value
    return 0.5


def _integer_step(node: dict) -> int | float:
    """数值边界收紧步长：仅允许整数时取 1，否则取 1.0。"""
    types = _node_types_number(node)
    if types and types <= {"integer"}:
        return 1
    return 1.0


# ---------------------------------------------------------------------------
# 节点局部变异生成
# ---------------------------------------------------------------------------


def _mutations_for_node(node: dict, pointer: str) -> list[MutationSpec]:
    specs: list[MutationSpec] = []

    # ---- required_removed（放松）-----------------------------------------
    required = node.get("required")
    if isinstance(required, list) and required:
        for name in sorted(set(required)):
            def apply_required(n: dict, name=name) -> None:
                remaining = [x for x in n["required"] if x != name]
                if remaining:
                    n["required"] = remaining
                else:
                    n.pop("required", None)

            witness = minimal_object(node)
            witness.pop(name, None)
            specs.append(MutationSpec(
                rule="required_removed",
                pointer=pointer,
                subject=str(name),
                description=f"删除必填字段 {jsonish(name)}（required 移除该项）",
                apply=apply_required,
                witness=witness,
                witness_originally_valid=False,
            ))

    # ---- enum_narrowed（收紧）--------------------------------------------
    enum = node.get("enum")
    if isinstance(enum, list) and len(enum) >= 2:
        for index, value in enumerate(enum):
            subject = jsonish(value)

            def apply_enum(n: dict, index=index) -> None:
                n["enum"] = [v for i, v in enumerate(n["enum"]) if i != index]

            specs.append(MutationSpec(
                rule="enum_narrowed",
                pointer=pointer,
                subject=subject,
                description=f"枚举收窄：删除取值 {subject}",
                apply=apply_enum,
                witness=copy.deepcopy(value),
                witness_originally_valid=True,
            ))

    # ---- minimum_increased（收紧）----------------------------------------
    lo, lo_exclusive = _lower_bound(node)
    if lo is not None:
        step = _integer_step(node)
        if lo_exclusive:
            new_bound = lo + step
            boolean_exclusive = isinstance(node.get("exclusiveMinimum"), bool)

            def apply_minimum(n: dict, new_bound=new_bound) -> None:
                if isinstance(n.get("exclusiveMinimum"), bool):
                    # draft-04：minimum + 布尔排他标记，收紧 minimum 本身
                    n["minimum"] = new_bound
                else:
                    n["exclusiveMinimum"] = new_bound

            if boolean_exclusive:
                old_text = f"minimum {fmt_number(lo)}（exclusiveMinimum=true）"
            else:
                old_text = f"exclusiveMinimum {fmt_number(lo)}"
        else:
            new_bound = lo + step

            def apply_minimum(n: dict, new_bound=new_bound) -> None:  # noqa: F811
                n["minimum"] = new_bound

            old_text = f"minimum {fmt_number(lo)}"

        # 原边界上合法的最小值（兼顾 multipleOf / 排他边界），
        # 下界提高后必然被拒绝
        witness = _min_number(node, integer=step == 1)
        specs.append(MutationSpec(
            rule="minimum_increased",
            pointer=pointer,
            subject="minimum",
            description=f"数值下界收紧：{old_text} 提高为 {fmt_number(new_bound)}",
            apply=apply_minimum,
            witness=witness,
            witness_originally_valid=True,
        ))

    # ---- maximum_decreased（收紧）----------------------------------------
    hi, hi_exclusive = _upper_bound(node)
    if hi is not None:
        step = _integer_step(node)
        if hi_exclusive:
            new_bound = hi - step
            boolean_exclusive = isinstance(node.get("exclusiveMaximum"), bool)

            def apply_maximum(n: dict, new_bound=new_bound) -> None:
                if isinstance(n.get("exclusiveMaximum"), bool):
                    n["maximum"] = new_bound
                else:
                    n["exclusiveMaximum"] = new_bound

            if boolean_exclusive:
                old_text = f"maximum {fmt_number(hi)}（exclusiveMaximum=true）"
            else:
                old_text = f"exclusiveMaximum {fmt_number(hi)}"
        else:
            new_bound = hi - step

            def apply_maximum(n: dict, new_bound=new_bound) -> None:  # noqa: F811
                n["maximum"] = new_bound

            old_text = f"maximum {fmt_number(hi)}"

        witness = _max_number(node)
        specs.append(MutationSpec(
            rule="maximum_decreased",
            pointer=pointer,
            subject="maximum",
            description=f"数值上界收紧：{old_text} 降低为 {fmt_number(new_bound)}",
            apply=apply_maximum,
            witness=witness,
            # 当声明上界低于下界等自相矛盾节点上，最大合法值可能不存在，
            # 交由等价性预检过滤。
            witness_originally_valid=True,
        ))

    # ---- type_narrowed（收紧）--------------------------------------------
    raw_type = node.get("type")
    types = list(raw_type) if isinstance(raw_type, list) else []
    if len(types) >= 2:
        for removed in types:
            def apply_type(n: dict, removed=removed) -> None:
                remaining = [t for t in n["type"] if t != removed]
                n["type"] = remaining[0] if len(remaining) == 1 else remaining

            # integer 是 number 的特化：删除 number 后仅剩 integer，
            # 必须用小数值见证才能与原 ["integer","number"] 区分；
            # 其余类型用 typed_fragment 取该类型的最小片段。
            witness = (
                _fractional_fragment(node)
                if removed == "number" and "integer" in types
                else typed_fragment(node, removed)
            )
            specs.append(MutationSpec(
                rule="type_narrowed",
                pointer=pointer,
                subject=str(removed),
                description=f"类型收窄：删除允许类型 {removed}",
                apply=apply_type,
                witness=witness,
                witness_originally_valid=True,
            ))

    # ---- 长度 / 数量边界收紧 ----------------------------------------------
    specs.extend(_length_mutations(node, pointer))
    return specs


def _length_mutations(node: dict, pointer: str) -> list[MutationSpec]:
    specs: list[MutationSpec] = []

    def add(rule: str, keyword: str, old: int, new: int, subject: str,
            text: str, witness: Any) -> None:
        def apply_keyword(n: dict, keyword=keyword, new=new) -> None:
            n[keyword] = new

        specs.append(MutationSpec(
            rule=rule,
            pointer=pointer,
            subject=subject,
            description=text,
            apply=apply_keyword,
            witness=witness,
            witness_originally_valid=True,
        ))

    old = node.get("minLength")
    if isinstance(old, (int, float)) and not isinstance(old, bool):
        add("min_length_increased", "minLength", old, old + 1, "minLength",
            f"字符串最短长度由 {fmt_number(old)} 收紧为 {fmt_number(old + 1)}",
            "a" * int(old))

    old = node.get("maxLength")
    if isinstance(old, (int, float)) and not isinstance(old, bool) and old >= 1:
        add("max_length_decreased", "maxLength", old, old - 1, "maxLength",
            f"字符串最长长度由 {fmt_number(old)} 收紧为 {fmt_number(old - 1)}",
            "a" * int(old))

    old = node.get("minItems")
    if isinstance(old, (int, float)) and not isinstance(old, bool):
        add("min_items_increased", "minItems", old, old + 1, "minItems",
            f"数组最少元素数由 {fmt_number(old)} 收紧为 {fmt_number(old + 1)}",
            _array_at_size(node, int(old)))

    old = node.get("maxItems")
    if isinstance(old, (int, float)) and not isinstance(old, bool) and old >= 1:
        add("max_items_decreased", "maxItems", old, old - 1, "maxItems",
            f"数组最多元素数由 {fmt_number(old)} 收紧为 {fmt_number(old - 1)}",
            _array_at_size(node, int(old)))

    old = node.get("minProperties")
    if isinstance(old, (int, float)) and not isinstance(old, bool):
        add("min_properties_increased", "minProperties", old, old + 1,
            "minProperties",
            f"对象最少属性数由 {fmt_number(old)} 收紧为 {fmt_number(old + 1)}",
            _object_at_size(node, int(old)))

    old = node.get("maxProperties")
    if isinstance(old, (int, float)) and not isinstance(old, bool) and old >= 1:
        add("max_properties_decreased", "maxProperties", old, old - 1,
            "maxProperties",
            f"对象最多属性数由 {fmt_number(old)} 收紧为 {fmt_number(old - 1)}",
            _object_at_size(node, int(old)))

    return specs


def fmt_number(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _array_at_size(node: dict, size: int) -> list:
    from app.compat import minimal_array, normalize_items

    arr = minimal_array(node)
    prefix, rest, _ = normalize_items(node)
    prefix_len = len(prefix) if prefix else 0
    while len(arr) < size:
        j = len(arr)
        if prefix and j < prefix_len:
            arr.append(minimal_instance(prefix[j]))
        else:
            arr.append(minimal_instance(rest))
    return arr


def _object_at_size(node: dict, size: int) -> dict:
    from app.compat import extra_key

    obj = minimal_object(node)
    while len(obj) < size:
        obj[extra_key(node, obj)] = 1
    return obj


def _max_number(node: dict) -> int | float:
    """节点声明上界处的最大合法数值（兼顾排他边界与 multipleOf）。"""
    import math

    hi, exclusive = _upper_bound(node)
    if hi is None:
        return 0
    integer = _integer_step(node) == 1
    if integer:
        value = int(math.floor(hi))
        if exclusive and float(value) >= hi:
            value -= 1
    else:
        value = hi - (1 if exclusive else 0)
    multiple = node.get("multipleOf")
    if isinstance(multiple, (int, float)) and multiple > 0:
        steps = math.floor(value / multiple)
        value = steps * multiple
        if integer:
            value = int(value)
    return value


# ---------------------------------------------------------------------------
# schema 遍历（递归收集所有"生效中"的子 schema 节点）
# ---------------------------------------------------------------------------


def _iter_nodes(schema: Any):
    """深度优先、确定性顺序产出 (node, pointer_tokens)。

    不进入 ``not`` / ``if`` 内层（收紧 not 内层反而放松整体语义；
    if 不直接生效）与未内联的 ``$defs`` / ``definitions`` 副本。
    """
    stack: list[tuple[Any, tuple[str, ...], frozenset[int]]] = [
        (schema, (), frozenset())
    ]
    while stack:
        node, tokens, seen = stack.pop()
        if node is True or node is False or not isinstance(node, dict):
            continue
        if id(node) in seen:
            continue
        seen = seen | {id(node)}
        yield node, tokens

        children: list[tuple[Any, tuple[str, ...]]] = []

        props = node.get("properties")
        if isinstance(props, dict):
            for name in sorted(props):
                children.append((props[name], tokens + ("properties", str(name))))
        pp = node.get("patternProperties")
        if isinstance(pp, dict):
            for pattern in sorted(pp):
                children.append((pp[pattern],
                                 tokens + ("patternProperties", str(pattern))))
        ap = node.get("additionalProperties")
        if isinstance(ap, (dict, bool)):
            children.append((ap, tokens + ("additionalProperties",)))

        prefix = node.get("prefixItems")
        if isinstance(prefix, list):
            for i, sub in enumerate(prefix):
                children.append((sub, tokens + ("prefixItems", str(i))))
        items = node.get("items")
        if isinstance(items, list):  # draft-04/06/07 元组写法
            for i, sub in enumerate(items):
                children.append((sub, tokens + ("items", str(i))))
            extra_items = node.get("additionalItems")
            if isinstance(extra_items, (dict, bool)):
                children.append((extra_items, tokens + ("additionalItems",)))
        elif isinstance(items, (dict, bool)):
            children.append((items, tokens + ("items",)))

        for keyword in ("anyOf", "oneOf", "allOf"):
            branches = node.get(keyword)
            if isinstance(branches, list):
                for i, sub in enumerate(branches):
                    children.append((sub, tokens + (keyword, str(i))))
        for keyword in ("then", "else", "contains", "propertyNames"):
            sub = node.get(keyword)
            if isinstance(sub, (dict, bool)):
                children.append((sub, tokens + (keyword,)))

        # 压栈顺序反转，保证按 children 声明顺序展开
        for child, child_tokens in reversed(children):
            stack.append((child, child_tokens, seen))


# ---------------------------------------------------------------------------
# 变异体生成（含节点局部等价性预检与每类上限截断）
# ---------------------------------------------------------------------------


def _node_validators(node: dict, validator_cls, spec: MutationSpec):
    """返回 (原节点校验器, 变异节点校验器)。"""
    mutated = copy.deepcopy(node)
    spec.apply(mutated)
    return validator_cls(node), validator_cls(mutated)


def _is_distinguishable(node: dict, validator_cls, spec: MutationSpec) -> bool:
    """节点局部见证是否确实在原 / 异节点上给出不同校验结果。"""
    try:
        original, mutated = _node_validators(node, validator_cls, spec)
        originally_valid = original.is_valid(spec.witness)
        mutated_valid = mutated.is_valid(spec.witness)
    except Exception:  # noqa: BLE001
        return False
    if spec.witness_originally_valid:
        return originally_valid and not mutated_valid
    return (not originally_valid) and mutated_valid


def generate_mutations(schema: Any, max_per_rule: dict[str, int]) -> list[dict]:
    """基于新 schema 确定性生成通过等价性预检的全部变异体（已按类截断）。

    返回字段：id / rule / pointer / subject / description / apply。
    """
    resolved = resolve_refs(schema)
    validator_cls = validator_class_for(resolved)

    by_rule: "OrderedDict[str, list[dict]]" = OrderedDict(
        (rule, []) for rule in RULE_NAMES
    )

    for node, tokens in _iter_nodes(resolved):
        pointer = _render_pointer(tokens)
        for spec in _mutations_for_node(node, pointer):
            if not _is_distinguishable(node, validator_cls, spec):
                continue
            by_rule[spec.rule].append({
                "id": _mutation_id(spec.rule, spec.pointer, spec.subject),
                "rule": spec.rule,
                "pointer": spec.pointer,
                "subject": spec.subject,
                "description": spec.description,
                "spec": spec,
            })

    mutants: list[dict] = []
    for rule in RULE_NAMES:
        bucket = by_rule[rule]
        bucket.sort(key=lambda m: (m["pointer"], m["subject"]))
        limit = max_per_rule.get(rule, DEFAULT_MAX_PER_RULE)
        mutants.extend(bucket[:limit])
    return mutants


# ---------------------------------------------------------------------------
# 变异体落地与逐文档评估
# ---------------------------------------------------------------------------


def _apply_mutation(resolved_schema: Any, mutant: dict) -> Any:
    """在 schema 深拷贝上沿变异体指针施加单点编辑。"""
    mutated = copy.deepcopy(resolved_schema)
    tokens = mutant["pointer"][2:].split("/") if mutant["pointer"] != "#" else []
    node: Any = mutated
    for token in tokens:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list) and token.isdigit():
            node = node[int(token)]
        elif isinstance(node, dict):
            node = node[token]
        else:
            raise ValueError(f"变异路径无法导航: {mutant['pointer']}")
    if not isinstance(node, dict):
        raise ValueError(f"变异终点不是 schema 对象: {mutant['pointer']}")
    mutant["spec"].apply(node)
    return mutated


def evaluate_mutation_coverage(schema: Any, documents: list[Any],
                               max_per_rule: dict[str, int] | None = None) -> dict:
    """对一批文档执行变异覆盖评估（纯函数）。

    ``documents`` 由调用方保证为 1..MAX_MUTATION_DOCUMENTS 个任意 JSON 值；
    ``max_per_rule`` 的键由调用方保证合法、值非负。
    """
    limits = dict(max_per_rule or {})
    resolved = resolve_refs(schema)
    base_validator = make_validator(resolved)
    validator_cls = validator_class_for(resolved)

    mutants = generate_mutations(resolved, limits)

    # 原 schema 上的逐文档校验结果（杀死判定的基线）
    originally_valid = [base_validator.is_valid(doc) for doc in documents]
    valid_documents = sum(1 for ok in originally_valid if ok)

    results: list[dict] = []
    for mutant in mutants:
        mutated_schema = _apply_mutation(resolved, mutant)
        try:
            mutated_validator = validator_cls(mutated_schema)
        except Exception:  # noqa: BLE001 —— 理论上不会发生，防御性降级
            results.append(_mutant_result(mutant, killed=False, examples=[]))
            continue

        examples: list[int] = []
        killed = False
        for index, doc in enumerate(documents):
            try:
                result_diff = originally_valid[index] != mutated_validator.is_valid(doc)
            except Exception:  # noqa: BLE001
                result_diff = False
            if result_diff:
                killed = True
                if len(examples) < MAX_KILL_EXAMPLE_INDICES:
                    examples.append(index)
        results.append(_mutant_result(mutant, killed=killed, examples=examples))

    results.sort(key=lambda m: (m["rule"], m["pointer"], m["subject"], m["id"]))

    total = len(results)
    killed_count = sum(1 for m in results if m["killed"])
    # 按规则分类的 mutation score
    rule_scores: list[dict] = []
    for rule in RULE_NAMES:
        bucket = [m for m in results if m["rule"] == rule]
        rule_total = len(bucket)
        rule_killed = sum(1 for m in bucket if m["killed"])
        rule_scores.append({
            "rule": rule,
            "total": rule_total,
            "killed": rule_killed,
            "surviving": rule_total - rule_killed,
            "mutation_score": (rule_killed / rule_total) if rule_total else None,
        })

    return {
        "total_documents": len(documents),
        "valid_documents": valid_documents,
        "invalid_documents": len(documents) - valid_documents,
        "total_mutations": total,
        "killed_mutations": killed_count,
        "surviving_mutations": total - killed_count,
        "mutation_score": (killed_count / total) if total else 1.0,
        "limits": {rule: limits.get(rule, DEFAULT_MAX_PER_RULE) for rule in RULE_NAMES},
        "rules": rule_scores,
        "mutants": results,
        "uncovered_paths": [m["pointer"] for m in results if not m["killed"]],
    }


def _mutant_result(mutant: dict, killed: bool, examples: list[int]) -> dict:
    return {
        "id": mutant["id"],
        "rule": mutant["rule"],
        "pointer": mutant["pointer"],
        "subject": mutant["subject"],
        "description": mutant["description"],
        "killed": killed,
        "example_indices": examples,
    }
