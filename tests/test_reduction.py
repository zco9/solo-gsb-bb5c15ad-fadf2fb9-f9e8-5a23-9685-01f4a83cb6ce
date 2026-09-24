"""兼容性反例缩减引擎单元测试。

覆盖：三类操作固定顺序（对象属性 -> 数组元素 -> 字符串缩短）、方向
保持（backward / forward）、非指定单向破坏类别的明确报错、重验上限
提前结束只返回已验证结果、确定性与入参不被修改。
"""
import copy

import pytest

from app import compat, reduction


def _validators(old, new):
    return compat.make_validator(old), compat.make_validator(new)


# ---------------------------------------------------------------------------
# 方向保持与基本缩减
# ---------------------------------------------------------------------------


def test_backward_reduces_irrelevant_properties_and_string():
    # 两版都必填 s；新版枚举收窄到 ["a"]、旧版要求 minLength 1。
    # 反例 {"s": "bbb", ...}：缩短到 "b" 后旧版仍合法、新版仍拒绝，
    # 再短成 "" 会被旧版 minLength 拒绝（方向丢失），故最终 {"s": "b"}。
    old = {"type": "object", "required": ["s"],
           "properties": {"s": {"type": "string", "minLength": 1}}}
    new = {"type": "object", "required": ["s"],
           "properties": {"s": {"type": "string", "enum": ["a"]}}}
    compat.analyze(old, new)

    document = {"s": "bbb", "noise": {"a": 1, "b": 2}, "zzz": [1, 2, 3]}
    result = reduction.reduce_counterexample(old, new, document, "backward")

    old_v, new_v = _validators(old, new)
    assert old_v.is_valid(result["reduced"])
    assert not new_v.is_valid(result["reduced"])
    assert result["reduced"] == {"s": "b"}
    assert result["direction"] == "backward"
    assert result["limit_reached"] is False

    # 每步长度严格递减；操作顺序：先删属性，字符串缩短排在最后
    ops = [s["op"] for s in result["steps"]]
    assert "shorten_string" in ops
    assert ops.index("shorten_string") > max(
        i for i, op in enumerate(ops) if op == "delete_property"
    )
    for step in result["steps"]:
        assert step["serialized_length_after"] < step["serialized_length_before"]
    assert (result["serialized_length_original"]
            > result["serialized_length_reduced"])


def test_forward_direction_reduction():
    # 旧版必填 email：新版合法、旧版拒绝 -> forward；多余属性应被删掉
    old = {"type": "object", "required": ["email"],
           "properties": {"email": {"type": "string"}}}
    new = {"type": "object", "properties": {"email": {"type": "string"}}}
    compat.analyze(old, new)

    document = {"noise": 1, "deep": {"x": "y"}}
    result = reduction.reduce_counterexample(old, new, document, "forward")

    old_v, new_v = _validators(old, new)
    assert new_v.is_valid(result["reduced"])
    assert not old_v.is_valid(result["reduced"])
    assert result["reduced"] == {}
    assert all(s["op"] == "delete_property" for s in result["steps"])


def test_wrong_direction_raises():
    # backward 文档 + forward 请求 -> wrong_direction
    old = {"type": "object", "required": ["s"],
           "properties": {"s": {"type": "string"}}}
    new = {"type": "object", "required": ["s"],
           "properties": {"s": {"type": "string", "enum": ["a"]}}}

    with pytest.raises(reduction.NonBreakingDocument) as exc:
        reduction.reduce_counterexample(old, new, {"s": "b"}, "forward")
    assert exc.value.reason == "wrong_direction"
    assert exc.value.actual_direction == "backward"
    assert exc.value.old_valid is True
    assert exc.value.new_valid is False


def test_valid_both_and_invalid_both_raise():
    schema = {"type": "object", "required": ["a"]}

    with pytest.raises(reduction.NonBreakingDocument) as exc:
        reduction.reduce_counterexample(schema, schema, {"a": 1}, "backward")
    assert exc.value.reason == "valid_both"

    # 根类型为 object：整数被旧、新两版同时拒绝
    with pytest.raises(reduction.NonBreakingDocument) as exc:
        reduction.reduce_counterexample(schema, schema, 42, "backward")
    assert exc.value.reason == "invalid_both"
    assert exc.value.old_valid is False
    assert exc.value.new_valid is False


# ---------------------------------------------------------------------------
# 数组元素删除
# ---------------------------------------------------------------------------


def test_array_element_deletion_in_fixed_phase():
    # 两版都必填 arr（数组不能整列删除，否则旧版 required 失败）；
    # 新版要求数组最少 2 个元素 -> 1 元素文档已破坏，删掉唯一元素得到
    # 空数组后旧版仍合法、新版仍拒绝。属性阶段无法破坏 minItems，元素
    # 删除发生在数组阶段（晚于属性阶段）。
    old = {"type": "object", "required": ["arr"],
           "properties": {"arr": {"type": "array"}}}
    new = {"type": "object", "required": ["arr"],
           "properties": {"arr": {"type": "array", "minItems": 2}}}
    compat.analyze(old, new)

    document = {"extra": {"nested": 1}, "arr": [{"x": 1}]}
    result = reduction.reduce_counterexample(old, new, document, "backward")

    old_v, new_v = _validators(old, new)
    assert old_v.is_valid(result["reduced"])
    assert not new_v.is_valid(result["reduced"])
    assert result["reduced"] == {"arr": []}

    ops = [s["op"] for s in result["steps"]]
    property_steps = [s for s in result["steps"] if s["op"] == "delete_property"]
    assert any(s["pointer"] == "#/extra" for s in property_steps)
    element_steps = [s for s in result["steps"] if s["op"] == "delete_element"]
    assert element_steps
    # 属性阶段整体早于数组阶段
    assert ops.index("delete_element") > ops.index("delete_property")
    # delete_element 携带 element_index、不携带 key，指针定位到数组元素
    assert all(s["element_index"] is not None and s["key"] is None
               for s in element_steps)
    assert {s["pointer"] for s in element_steps} <= {"#/arr/0", "#/arr/1"}


def test_nested_string_shortening_is_last_resort():
    # 无属性 / 数组可删时缩短字符串。旧版允许任意非空字符串，新版只接受
    # 枚举 "a"；从尾位逐字符删除，"b" 是仍满足旧版、被新版拒绝的最短串。
    old = {"type": "string", "minLength": 1}
    new = {"type": "string", "enum": ["a"]}

    result = reduction.reduce_counterexample(old, new, "abc", "backward")
    reduced = result["reduced"]
    old_v, new_v = _validators(old, new)
    assert old_v.is_valid(reduced)
    assert not new_v.is_valid(reduced)
    assert reduced == "b"
    assert all(s["op"] == "shorten_string" for s in result["steps"])
    last = result["steps"][-1]
    assert last["pointer"] == "#"
    assert last["from_length"] == last["to_length"] + 1
    # 接受轨迹 "abc" -> "ab"；删 "b" 得 "a"（新版合法，方向丢失，拒绝）；
    # 再删 "a" 得 "b"（仍破坏 backward，接受），故最后删除的字符是 "a"
    assert last["char"] == "a"
    assert last["position"] == 0


def test_scalar_non_string_has_no_candidates():
    # 数值 backward 反例：没有任何可删目标，原样返回、零重验
    old = {"type": "number"}
    new = {"type": "integer", "minimum": 10}
    result = reduction.reduce_counterexample(old, new, 3.5, "backward")
    assert result["reduced"] == 3.5
    assert result["steps"] == []
    assert result["validation_count"] == 0
    assert result["limit_reached"] is False


# ---------------------------------------------------------------------------
# 重验上限
# ---------------------------------------------------------------------------


def test_max_validations_caps_and_returns_verified_only():
    # 旧版允许任意字符串，新版只接受 "a"：每次删尾字符都仍破坏方向，
    # 直到 "a" 合法。上限设为 1 时只允许一次重验 -> 提前结束，返回的
    # 文档是该次已验证通过的结果（"abcdef" 去掉尾字符）。
    old = {"type": "string"}
    new = {"type": "string", "enum": ["a"]}

    result = reduction.reduce_counterexample(
        old, new, "abcdef", "backward", max_validations=1,
    )
    assert result["limit_reached"] is True
    assert result["validation_count"] == 1
    old_v, new_v = _validators(old, new)
    assert old_v.is_valid(result["reduced"])
    assert not new_v.is_valid(result["reduced"])
    assert result["reduced"] == "abcde"
    assert len(result["steps"]) == 1


def test_max_validations_allows_full_reduction_when_high():
    old = {"type": "object", "required": ["s"],
           "properties": {"s": {"type": "string", "minLength": 1}}}
    new = {"type": "object", "required": ["s"],
           "properties": {"s": {"type": "string", "enum": ["a"]}}}
    result = reduction.reduce_counterexample(
        old, new, {"s": "bbb", "junk": [1, 2, 3]}, "backward",
        max_validations=reduction.MAX_VALIDATIONS_LIMIT,
    )
    assert result["limit_reached"] is False
    assert result["reduced"] == {"s": "b"}


# ---------------------------------------------------------------------------
# 确定性 / 不修改入参
# ---------------------------------------------------------------------------


def test_deterministic_and_input_unchanged():
    old = {"type": "object", "required": ["s"],
           "properties": {"s": {"type": "string", "minLength": 1}}}
    new = {"type": "object", "required": ["s"],
           "properties": {"s": {"type": "string", "enum": ["a"]}}}
    document = {"z": 1, "s": "bb", "a": {"k": 1}}
    doc_copy = copy.deepcopy(document)
    old_copy = copy.deepcopy(old)
    new_copy = copy.deepcopy(new)

    first = reduction.reduce_counterexample(old, new, document, "backward")
    second = reduction.reduce_counterexample(old, new, document, "backward")
    assert first["reduced"] == second["reduced"]
    assert [(s["op"], s["pointer"]) for s in first["steps"]] == \
        [(s["op"], s["pointer"]) for s in second["steps"]]
    assert document == doc_copy
    assert old == old_copy and new == new_copy
    # original 是原文的独立副本
    assert first["original"] == doc_copy
    assert first["original"] is not document


def test_steps_pointers_locate_positions():
    old = {"type": "object", "required": ["s", "arr"],
           "properties": {"s": {"type": "string", "minLength": 1},
                          "arr": {"type": "array"}}}
    new = {"type": "object", "required": ["s", "arr"],
           "properties": {"s": {"type": "string", "enum": ["a"]},
                          "arr": {"type": "array", "minItems": 1}}}
    document = {"s": "b", "arr": [{"deep": 1}, 9], "extra": "x"}
    result = reduction.reduce_counterexample(old, new, document, "backward")

    pointers = {s["pointer"] for s in result["steps"]}
    # 根级多余属性、数组元素位置都以 JSON Pointer 出现
    assert "#/extra" in pointers
    assert {"#/arr/0", "#/arr/1"} & pointers
    # 步骤序号连续
    assert [s["index"] for s in result["steps"]] == list(range(len(result["steps"])))
    # 最终结果同时保留 s（枚举破坏）与空数组（minItems 破坏）
    assert result["reduced"] == {"s": "b", "arr": []}
