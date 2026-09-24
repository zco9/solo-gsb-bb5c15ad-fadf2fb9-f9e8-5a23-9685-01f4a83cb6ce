"""基于 ``x-sensitive`` 的实例脱敏引擎单元测试。

覆盖：对象 / 数组遍历（properties / patternProperties /
additionalProperties / prefixItems / 元组 items / additionalItems）、
本地 ``$ref`` 解析、oneOf/anyOf 仅遍历校验通过分支、allOf 全分支、
递归引用终止、组合分支与 properties 的重复命中去重、指针转义、
原始入参与 schema 不被修改、以及无法解析引用的异常与 schema 指针。
"""
import pytest

from app import redact


def redacted(schema, doc):
    return redact.redact_documents(schema, [doc])["results"][0]


# ---------------------------------------------------------------------------
# 基础对象 / 数组遍历
# ---------------------------------------------------------------------------


def test_replaces_only_sensitive_properties():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "ssn": {"type": "string", "x-sensitive": True},
        },
    }
    doc = {"name": "alice", "ssn": "111"}
    result = redacted(schema, doc)
    assert result["redacted"] == {"name": "alice", "ssn": "***"}
    assert [(r["instance_pointer"], r["schema_pointer"])
            for r in result["replacements"]] == [
        ("#/ssn", "#/properties/ssn")]
    # 原文不被修改
    assert doc["ssn"] == "111"


def test_non_true_x_sensitive_is_ignored():
    schema = {"properties": {"a": {"x-sensitive": False},
                             "b": {"x-sensitive": "yes"},
                             "c": {}}}
    result = redacted(schema, {"a": 1, "b": 2, "c": 3})
    assert result["redacted"] == {"a": 1, "b": 2, "c": 3}
    assert result["replacements"] == []


def test_array_items_each_redacted():
    schema = {"type": "array", "items": {"x-sensitive": True}}
    result = redacted(schema, ["a", "b", "c"])
    assert result["redacted"] == ["***", "***", "***"]
    assert [r["instance_pointer"] for r in result["replacements"]] == [
        "#/0", "#/1", "#/2"]


def test_nested_objects_and_arrays():
    schema = {
        "properties": {
            "users": {
                "type": "array",
                "items": {
                    "properties": {
                        "email": {"x-sensitive": True},
                        "tags": {"type": "array",
                                 "items": {"x-sensitive": True}},
                    },
                },
            },
        },
    }
    doc = {"users": [{"email": "a@b.c", "tags": ["x"]},
                     {"email": "d@e.f", "tags": []}]}
    result = redacted(schema, doc)
    assert result["redacted"] == {
        "users": [{"email": "***", "tags": ["***"]},
                  {"email": "***", "tags": []}]}
    assert doc["users"][0]["email"] == "a@b.c"


def test_pattern_and_additional_properties():
    schema = {
        "patternProperties": {"^secret_": {"x-sensitive": True}},
        "additionalProperties": {"properties": {"v": {"x-sensitive": True}}},
    }
    doc = {"secret_a": 1, "other": {"v": 2, "keep": 3}}
    result = redacted(schema, doc)
    assert result["redacted"] == {
        "secret_a": "***", "other": {"v": "***", "keep": 3}}


def test_additional_properties_false_does_not_redact():
    schema = {
        "properties": {"a": {"x-sensitive": True}},
        "additionalProperties": False,
    }
    result = redacted(schema, {"a": 1, "b": 2})
    assert result["redacted"] == {"a": "***", "b": 2}


def test_prefix_items_and_items_2020():
    schema = {"prefixItems": [{"x-sensitive": True}, {"type": "string"}],
              "items": {"x-sensitive": True}}
    result = redacted(schema, [1, "a", 3, 4])
    assert result["redacted"] == ["***", "a", "***", "***"]
    ptrs = [(r["instance_pointer"], r["schema_pointer"])
            for r in result["replacements"]]
    assert ("#/0", "#/prefixItems/0") in ptrs
    assert ("#/2", "#/items") in ptrs


def test_legacy_tuple_items_with_additional_items():
    schema = {"items": [{"x-sensitive": True}, {}],
              "additionalItems": {"x-sensitive": True}}
    result = redacted(schema, [1, 2, 3])
    assert result["redacted"] == ["***", 2, "***"]


def test_scalar_and_null_documents_pass_through():
    schema = {"type": "string"}
    out = redact.redact_documents(schema, [None, True, 42, "s", [1]])
    assert [r["redacted"] for r in out["results"]] == [
        None, True, 42, "s", [1]]


def test_root_sensitive_masks_whole_document():
    result = redacted({"x-sensitive": True, "type": "object"}, {"a": {"b": 1}})
    assert result["redacted"] == "***"
    assert [r["instance_pointer"] for r in result["replacements"]] == ["#"]


def test_sensitive_ancestor_short_circuits_descendants():
    schema = {"properties": {"wrap": {
        "x-sensitive": True,
        "properties": {"inner": {"x-sensitive": True}}}}}
    result = redacted(schema, {"wrap": {"inner": 1}})
    assert result["redacted"] == {"wrap": "***"}
    # 祖先命中后不再下钻：同一位置只一条记录
    assert [r["instance_pointer"] for r in result["replacements"]] == ["#/wrap"]


def test_pointer_escaping_for_special_keys():
    schema = {"properties": {"a/b": {"x-sensitive": True},
                             "m~n": {"x-sensitive": True}}}
    result = redacted(schema, {"a/b": 1, "m~n": 2})
    ptrs = {r["instance_pointer"] for r in result["replacements"]}
    assert ptrs == {"#/a~1b", "#/m~0n"}
    assert result["redacted"] == {"a/b": "***", "m~n": "***"}


def test_results_preserve_input_order_and_indices():
    schema = {"properties": {"s": {"x-sensitive": True}}}
    out = redact.redact_documents(schema, [{"s": 1}, {"s": 2}, {"x": 3}])
    assert [r["index"] for r in out["results"]] == [0, 1, 2]
    assert out["results"][2]["redacted"] == {"x": 3}


# ---------------------------------------------------------------------------
# $ref
# ---------------------------------------------------------------------------


def test_local_ref_resolved():
    schema = {
        "properties": {"phone": {"$ref": "#/$defs/Phone"}},
        "$defs": {"Phone": {"type": "string", "x-sensitive": True}},
    }
    result = redacted(schema, {"phone": "123"})
    assert result["redacted"] == {"phone": "***"}
    assert result["replacements"][0]["schema_pointer"] == "#/$defs/Phone"


def test_ref_root_pointer():
    schema = {
        "type": "object",
        "x-sensitive": False,
        "properties": {"self": {"$ref": "#"}},
    }
    # 根节点不敏感，自指展开到同一实例位置时不应无限递归
    result = redacted(schema, {"self": {"self": {}}})
    assert result["redacted"] == {"self": {"self": {}}}


def test_recursive_linked_list_terminates_and_redacts_each_level():
    schema = {
        "type": "object",
        "properties": {
            "value": {"x-sensitive": True},
            "next": {"$ref": "#/$defs/Node"},
        },
        "$defs": {"Node": {
            "type": "object",
            "properties": {
                "value": {"x-sensitive": True},
                "next": {"$ref": "#/$defs/Node"},
            },
        }},
    }
    doc = {"value": 1, "next": {"value": 2, "next": {"value": 3}}}
    result = redacted(schema, doc)
    assert result["redacted"] == {
        "value": "***",
        "next": {"value": "***", "next": {"value": "***"}},
    }
    assert doc["next"]["value"] == 2


def test_unresolvable_ref_raises_with_schema_pointer():
    schema = {"properties": {"x": {"$ref": "#/$defs/Missing"}}}
    with pytest.raises(redact.UnresolvableReference) as exc:
        redact.redact_documents(schema, [{"x": 1}])
    assert exc.value.schema_pointer == "#/properties/x"
    assert exc.value.reason == "target_not_found"
    assert exc.value.instance_pointer == "#/x"


def test_external_ref_rejected():
    schema = {"properties": {"x": {"$ref": "https://e.com/s.json#/a"}}}
    with pytest.raises(redact.UnresolvableReference) as exc:
        redact.redact_documents(schema, [{"x": 1}])
    assert exc.value.reason == "external_reference"
    assert exc.value.schema_pointer == "#/properties/x"


def test_name_fragment_ref_rejected():
    schema = {"properties": {"x": {"$ref": "#anchor"}}}
    with pytest.raises(redact.UnresolvableReference) as exc:
        redact.redact_documents(schema, [{"x": 1}])
    assert exc.value.reason == "name_fragment_unsupported"


# ---------------------------------------------------------------------------
# 组合关键字
# ---------------------------------------------------------------------------


def test_allof_every_branch_traversed():
    schema = {
        "type": "object",
        "allOf": [
            {"properties": {"a": {"x-sensitive": True}}},
            {"properties": {"b": {"x-sensitive": True}}},
        ],
    }
    result = redacted(schema, {"a": 1, "b": 2, "c": 3})
    assert result["redacted"] == {"a": "***", "b": "***", "c": 3}


def test_oneof_only_passing_branch_traversed():
    schema = {
        "oneOf": [
            {"type": "object",
             "properties": {"t": {"const": "a"}, "secret": {"x-sensitive": True}},
             "required": ["t"]},
            {"type": "object",
             "properties": {"t": {"const": "b"}, "secret": {"x-sensitive": True}},
             "required": ["t"]},
        ],
    }
    out = redact.redact_documents(schema, [
        {"t": "a", "secret": "shh"},
        {"t": "b", "secret": "boo"},
    ])
    assert out["results"][0]["redacted"] == {"t": "a", "secret": "***"}
    assert (out["results"][0]["replacements"][0]["schema_pointer"]
            == "#/oneOf/0/properties/secret")
    assert out["results"][1]["redacted"] == {"t": "b", "secret": "***"}
    assert (out["results"][1]["replacements"][0]["schema_pointer"]
            == "#/oneOf/1/properties/secret")


def test_anyof_only_passing_branch_traversed():
    schema = {"anyOf": [
        {"type": "string", "x-sensitive": True},
        {"type": "integer"},
    ]}
    out = redact.redact_documents(schema, [7, "hi"])
    assert out["results"][0]["redacted"] == 7
    assert out["results"][0]["replacements"] == []
    assert out["results"][1]["redacted"] == "***"


def test_anyof_multiple_passing_branches_all_traversed():
    schema = {
        "type": "object",
        "anyOf": [
            {"properties": {"a": {"x-sensitive": True}}},
            {"properties": {"b": {"x-sensitive": True}}},
        ],
    }
    # 空约束分支都校验通过：a 与 b 都应脱敏
    result = redacted(schema, {"a": 1, "b": 2})
    assert result["redacted"] == {"a": "***", "b": "***"}


def test_branch_validation_uses_original_value_not_mask():
    """分支判定必须基于原值：先在分支内把判别字段标记敏感也不能误路由。"""
    schema = {
        "type": "object",
        "oneOf": [
            {"properties": {"kind": {"const": "a", "x-sensitive": True},
                            "a_secret": {"x-sensitive": True}}},
            {"properties": {"kind": {"const": "b"},
                            "b_secret": {"x-sensitive": True}}},
        ],
    }
    result = redacted(schema, {"kind": "a", "a_secret": "shh", "b_secret": "x"})
    # 即使 kind 自身会被掩码，分支判定仍按原值 "a" 路由
    assert result["redacted"] == {"kind": "***", "a_secret": "***", "b_secret": "x"}


def test_allof_and_properties_duplicate_hit_deduped():
    schema = {
        "type": "object",
        "properties": {"a": {"x-sensitive": True}},
        "allOf": [{"properties": {"a": {"x-sensitive": True}}}],
    }
    result = redacted(schema, {"a": 1})
    assert result["redacted"] == {"a": "***"}
    assert len(result["replacements"]) == 1


def test_recursive_ref_inside_allof_terminates():
    schema = {
        "type": "object",
        "properties": {
            "value": {"x-sensitive": True},
            "child": {"allOf": [{"$ref": "#"}]},
        },
    }
    doc = {"value": 1, "child": {"value": 2, "child": {"value": 3}}}
    result = redacted(schema, doc)
    assert result["redacted"] == {
        "value": "***",
        "child": {"value": "***", "child": {"value": "***"}},
    }


# ---------------------------------------------------------------------------
# 顺序与不修改入参
# ---------------------------------------------------------------------------


def test_replacements_sorted_by_instance_pointer():
    schema = {
        "properties": {
            "z": {"x-sensitive": True},
            "arr": {"type": "array", "items": {"x-sensitive": True}},
            "a": {"x-sensitive": True},
        },
    }
    result = redacted(schema, {"z": 1, "arr": [9, 8], "a": 1})
    assert [r["instance_pointer"] for r in result["replacements"]] == [
        "#/a", "#/arr/0", "#/arr/1", "#/z"]


def test_schema_not_mutated():
    schema = {"properties": {"a": {"$ref": "#/$defs/S"}},
              "$defs": {"S": {"x-sensitive": True}}}
    import copy
    snapshot = copy.deepcopy(schema)
    redact.redact_documents(schema, [{"a": 1}, {"a": 2}])
    assert schema == snapshot
