"""Schema 合规画像引擎。

对一批待检 JSON 文档用某个分析记录保存的**新 schema**（``new_schema``）
逐个做 ``jsonschema`` 校验，并把失败按位置 / 规则归并：

* 返回合规率（通过文档数 / 文档总数）；
* 每个失败组给出 JSON Pointer 风格的失败位置、规则代码、规则指纹、
  失败计数与示例文档下标；
* 对 ``oneOf`` / ``anyOf`` 产生的嵌套校验错误树，递归收集全部叶子
  失败，取叶子失败路径的**最深公共前缀**（longest common prefix）作为
  失败位置——所有分支都在同一深层位置失败时定位到该深层路径，分支
  在不同位置失败时回退到公共祖先（可能是根 ``#``）。

分组规则（稳定指纹）
====================
同类问题以 ``SHA-256`` 指纹合并，指纹输入为：

1. 实例失败位置（JSON Pointer，如 ``#/a/b``）；
2. jsonschema 校验规则（validator，如 ``required`` / ``type``）；
3. 规则主体（rule subject）：``required`` 取缺失字段名、
   ``additionalProperties`` / ``propertyNames`` 取未允许的键名，
   其余规则为空 —— 保证只剔除**值相关**噪声（如枚举实际取值、
   数组实际长度、oneOf 分支下标），不因文档数据不同或命中分支
   不同而把同一条规则拆成多组。

``schema_pointer``（schema 中触发规则的位置）仅作为排查用信息返回，
不参与指纹：oneOf/anyOf 不同分支下标（``oneOf/0`` 与 ``oneOf/1``）
是分支结构产物，不应拆散"同一位置同一规则"的问题。

字段间以 ``\\x1f`` 分隔避免拼接歧义，取摘要前 16 个十六进制字符。
输入顺序不影响分组结果：组按 (pointer, validator, schema_pointer,
指纹) 稳定排序，同组的 schema_pointer / 消息取字典序最小者，
文档下标升序排列。

引擎为纯函数，不读取数据库、不修改入参。
"""
from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from typing import Any

from app.compat import escape_pointer, make_validator

_FINGERPRINT_LEN = 16
_FIELD_SEP = "\x1f"

# 示例下标的最大保留数量（仅用于控制响应体积，不影响计数）。
MAX_EXAMPLE_INDICES = 20

# 单次画像允许的最大文档数（与 schemas.ProfileCreate 保持一致）。
MAX_PROFILE_DOCUMENTS = 5000

# 会产生"任一分支均失败"嵌套错误树的 jsonschema 关键字。
# 仅 oneOf / anyOf：allOf 的 context 是"未满足的分支"而非互斥候选，
# if/then/else 同理，二者不应套用最深公共路径归并。
_COMPOSITION_VALIDATORS = ("oneOf", "anyOf")


# ---------------------------------------------------------------------------
# JSON Pointer 渲染
# ---------------------------------------------------------------------------


def render_instance_pointer(path: tuple | list) -> str:
    """把实例路径（jsonschema absolute_path）渲染为 ``#/a/b`` 风格指针。"""
    parts = [escape_pointer(str(part)) for part in path]
    return "#/" + "/".join(parts) if parts else "#"


def render_schema_pointer(path: tuple | list) -> str:
    """把 schema 路径（jsonschema absolute_schema_path）渲染为 JSON Pointer。"""
    return render_instance_pointer(path)


def _normalize_text(text: Any) -> str:
    """压缩全部连续空白并去除首尾空白，使规则主体对格式抖动免疫。"""
    if text is None:
        return ""
    return " ".join(str(text).split())


def _render_json(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)

def rule_message(error: Any) -> str:
    """根据规则（而非实例取值）生成确定性的人类可读失败说明。

    不直接使用 jsonschema 的 ``error.message``：它通常包含实例的实际
    取值 / 长度 / 数量（如 ``-1 is less than the minimum of 0``），同一
    规则在不同文档上的消息不同，存入合并后的分组会让结果随输入顺序
    漂移。这里仅基于 ``validator`` 与 ``validator_value``（规则声明本身）
    渲染；未知规则退化为通用说明。
    """
    validator = str(error.validator)
    value = error.validator_value

    if validator == "required":
        subject = _rule_subject(validator, error)
        return f"缺少必填字段: {subject}" if subject else "缺少必填字段"
    if validator == "type":
        expected = value if isinstance(value, str) else _render_json(value)
        return f"值的类型必须为 {expected}"
    if validator == "enum":
        return "值不在 schema 允许的枚举集合中"
    if validator == "const":
        return "值必须等于 schema 声明的常量"
    if validator == "additionalProperties":
        subject = _rule_subject(validator, error)
        return (f"出现未声明的额外字段: {subject}" if subject
                else "出现 schema 不允许的额外字段")
    if validator == "propertyNames":
        subject = _rule_subject(validator, error)
        return (f"属性名不满足约束: {subject}" if subject
                else "存在不满足 propertyNames 约束的属性名")
    if validator == "minLength":
        return f"字符串长度必须不少于 {value}"
    if validator == "maxLength":
        return f"字符串长度必须不超过 {value}"
    if validator == "pattern":
        return f"字符串必须匹配正则 {value}"
    if validator == "minimum":
        return f"数值必须大于等于 {value}"
    if validator == "maximum":
        return f"数值必须小于等于 {value}"
    if validator == "exclusiveMinimum":
        return f"数值必须严格大于 {value}"
    if validator == "exclusiveMaximum":
        return f"数值必须严格小于 {value}"
    if validator == "multipleOf":
        return f"数值必须是 {value} 的整数倍"
    if validator == "minItems":
        return f"数组元素数量必须不少于 {value}"
    if validator == "maxItems":
        return f"数组元素数量必须不超过 {value}"
    if validator == "uniqueItems":
        return "数组元素必须唯一"
    if validator == "contains":
        return "数组中不存在满足 contains 约束的元素"
    if validator == "minProperties":
        return f"对象属性数量必须不少于 {value}"
    if validator == "maxProperties":
        return f"对象属性数量必须不超过 {value}"
    if validator == "dependentRequired":
        return f"字段依赖缺失: {_render_json(value)}"
    if validator == "not":
        return "值命中了 not 子 schema（不得满足该子 schema）"
    if validator in ("oneOf", "anyOf"):
        return f"值无法满足 {validator} 的任何分支"
    return f"未通过 {validator} 校验规则"


# ---------------------------------------------------------------------------
# oneOf / anyOf 嵌套错误树：最深公共失败路径提取
# ---------------------------------------------------------------------------


def _is_composition_error(error: Any) -> bool:
    return error.validator in _COMPOSITION_VALIDATORS and bool(error.context)


def _lcp_paths(paths: list[tuple]) -> tuple:
    """若干实例路径的最长公共前缀。"""
    if not paths:
        return ()
    common = list(paths[0])
    for path in paths[1:]:
        bound = min(len(common), len(path))
        i = 0
        while i < bound and common[i] == path[i]:
            i += 1
        common = common[:i]
        if not common:
            break
    return tuple(common)


def _collect_leaf_errors(error: Any) -> list[Any]:
    """沿 oneOf/anyOf（含嵌套组合）错误树收集全部叶子校验错误。

    组合节点自身（"not valid under any of the given schemas"）不产生
    叶子；非组合错误即使带 context 也视为叶子。
    """
    if _is_composition_error(error):
        leaves: list[Any] = []
        for child in error.context:
            leaves.extend(_collect_leaf_errors(child))
        return leaves
    return [error]


def _first_quoted(text: str) -> str:
    """提取错误消息中首个单引号包裹的标识（required 字段名 / 额外键名等）。"""
    match = re.search(r"'([^']*)'", _normalize_text(text))
    return match.group(1) if match else ""


def _rule_subject(validator: str, error: Any) -> str:
    """提取规则主体：只保留"同一条 schema 规则"下稳定不变的信息。

    * required：实际缺失的字段名（来自 jsonschema 消息，因为
      ``validator_value`` 是完整 required 列表而非缺失字段）；
    * dependentRequired：触发依赖的字段名；
    * additionalProperties / propertyNames：未被允许的键名。
    其余规则（type、enum、minimum、minLength……）返回空串，避免把
    实例实际取值 / 长度 / 数量等数据差异误并入指纹。
    """
    if validator == "required":
        return _first_quoted(getattr(error, "message", ""))
    if validator == "dependentRequired":
        # 消息形如 "'x' is a dependency of 'y'"；触发字段为消息末项
        quoted = re.findall(r"'([^']*)'", _normalize_text(getattr(error, "message", "")))
        return quoted[-1] if quoted else ""
    if validator in ("additionalProperties", "propertyNames"):
        return _first_quoted(getattr(error, "message", ""))
    return ""


def resolve_error(error: Any) -> dict:
    """把一条 jsonschema 顶层错误归并为一条失败记录。

    返回字段：pointer / validator / schema_pointer / subject / message。
    对 oneOf/anyOf 嵌套错误提取最深公共失败路径，并在全部叶子的
    （规则, 规则主体）身份一致时用叶子规则代表整棵子树（失败位置仍取
    最深公共前缀），否则以组合关键字本身作为规则。
    """
    if not _is_composition_error(error):
        validator = str(error.validator)
        return {
            "pointer": render_instance_pointer(list(error.absolute_path)),
            "validator": validator,
            "schema_pointer": render_schema_pointer(list(error.absolute_schema_path)),
            "subject": _rule_subject(validator, error),
            "message": rule_message(error),
        }

    leaves = _collect_leaf_errors(error)
    if not leaves:
        leaves = [error]

    leaf_paths = [tuple(leaf.absolute_path) for leaf in leaves]
    common_path = _lcp_paths(leaf_paths)

    # 叶子身份只看（规则, 规则主体）：分支下标造成的 schema_path
    # 差异不应拆散"同一位置同一规则"的问题。
    identities = {
        (
            str(leaf.validator),
            _rule_subject(str(leaf.validator), leaf),
        )
        for leaf in leaves
    }
    if len(identities) == 1:
        # 所有分支在同一深层位置因同一条规则失败：用确定性的叶子
        # 规则代表（消息用不含实例取值的规则模板），失败位置取最深
        # 公共前缀，schema 位置回退为组合节点自身位置以避免偏向某个
        # 分支下标。
        leaf = min(
            leaves,
            key=lambda l: tuple(str(p) for p in l.absolute_schema_path),
        )
        validator = str(leaf.validator)
        subject = _rule_subject(validator, leaf)
        message = rule_message(leaf)
    else:
        # 分支失败原因各异：公共路径处的组合约束才是真正的失败点。
        validator = str(error.validator)
        subject = ""
        message = _composition_message(error, leaves, common_path)

    return {
        "pointer": render_instance_pointer(common_path),
        "validator": validator,
        "schema_pointer": render_schema_pointer(list(error.absolute_schema_path)),
        "subject": subject,
        "message": message,
    }


def _composition_message(error: Any, leaves: list[Any], common_path: tuple) -> str:
    """构造组合失败的确定性人类可读消息（不依赖文档顺序）。"""
    child_validators = sorted({str(leaf.validator) for leaf in leaves})
    location = render_instance_pointer(common_path)
    return (
        f"文档在 {location} 处无法满足 {error.validator} 的任一分支"
        f"（叶子规则：{', '.join(child_validators)}）"
    )


def failure_fingerprint(record: dict) -> str:
    """生成稳定失败指纹：位置 + 规则 + 规则主体（不含 schema 位置）。"""
    key = _FIELD_SEP.join([
        str(record.get("pointer", "")),
        str(record.get("validator", "")),
        str(record.get("subject", "")),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:_FINGERPRINT_LEN]


# ---------------------------------------------------------------------------
# 画像
# ---------------------------------------------------------------------------


def profile_documents(schema: Any, documents: list[dict]) -> dict:
    """用 schema 逐个校验文档，返回合规画像（纯函数）。

    ``documents`` 由调用方保证为 1..MAX_PROFILE_DOCUMENTS 个 JSON 对象。
    每个文档内同一失败指纹只计一次（一条规则在一个文档中最多贡献
    一次失败），跨文档累计计数并收集示例下标。
    """
    validator = make_validator(schema)

    groups: "OrderedDict[str, dict]" = OrderedDict()
    valid_count = 0

    for index, document in enumerate(documents):
        errors = list(validator.iter_errors(document))
        if not errors:
            valid_count += 1
            continue

        seen_this_doc: set[str] = set()
        for error in errors:
            record = resolve_error(error)
            fingerprint = failure_fingerprint(record)
            if fingerprint in seen_this_doc:
                continue
            seen_this_doc.add(fingerprint)

            group = groups.get(fingerprint)
            if group is None:
                groups[fingerprint] = {
                    "fingerprint": fingerprint,
                    "pointer": record["pointer"],
                    "validator": record["validator"],
                    "schema_pointer": record["schema_pointer"],
                    "message": record["message"],
                    "count": 1,
                    "example_indices": [index],
                }
            else:
                group["count"] += 1
                if len(group["example_indices"]) < MAX_EXAMPLE_INDICES:
                    group["example_indices"].append(index)
                # 指纹不包含 schema_pointer / message：同组记录的这两个
                # 展示字段取字典序最小者，保证存储快照与输入顺序无关。
                if record["schema_pointer"] < group["schema_pointer"]:
                    group["schema_pointer"] = record["schema_pointer"]
                if record["message"] < group["message"]:
                    group["message"] = record["message"]

    total = len(documents)
    invalid_count = total - valid_count
    failures = sorted(
        groups.values(),
        key=lambda g: (g["pointer"], g["validator"], g["schema_pointer"],
                       g["fingerprint"]),
    )
    return {
        "total_documents": total,
        "valid_documents": valid_count,
        "invalid_documents": invalid_count,
        "compliance_rate": (valid_count / total) if total else 0.0,
        "failures": failures,
    }
