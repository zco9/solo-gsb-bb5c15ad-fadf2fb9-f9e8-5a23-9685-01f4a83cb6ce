"""合规画像引擎单元测试（最深公共失败路径 + 稳定指纹）。"""
import json

from app.profile import (
    MAX_PROFILE_DOCUMENTS,
    failure_fingerprint,
    profile_documents,
    resolve_error,
)
from app.compat import make_validator


def _top_errors(schema, doc):
    return list(make_validator(schema).iter_errors(doc))


def test_all_valid_documents_full_compliance():
    schema = {"type": "object", "required": ["a"],
              "properties": {"a": {"type": "string"}}}
    result = profile_documents(schema, [{"a": "x"}, {"a": "y"}])
    assert result["total_documents"] == 2
    assert result["valid_documents"] == 2
    assert result["invalid_documents"] == 0
    assert result["compliance_rate"] == 1.0
    assert result["failures"] == []


def test_compliance_rate_and_counts():
    schema = {"type": "object", "required": ["a"],
              "properties": {"a": {"type": "integer"}}}
    result = profile_documents(schema, [{"a": 1}, {"a": "bad"}, {}, {"a": 2}])
    assert result["valid_documents"] == 2
    assert result["invalid_documents"] == 2
    assert result["compliance_rate"] == 0.5


def test_failures_merged_by_pointer_and_rule():
    # 同一位置同一规则（minimum）在多个文档上只产生一个分组
    schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 10}},
              "required": ["n"]}
    result = profile_documents(schema, [{"n": 1}, {"n": 2}, {"n": 3}])
    assert len(result["failures"]) == 1
    group = result["failures"][0]
    assert group["pointer"] == "#/n"
    assert group["validator"] == "minimum"
    assert group["count"] == 3
    assert group["example_indices"] == [0, 1, 2]
    # 消息不含实例取值，只描述规则
    assert "10" in group["message"]
    assert "1" not in group["message"] or "不少于" in group["message"] or "大于" in group["message"]
    assert len(group["fingerprint"]) == 16


def test_same_pointer_different_rules_are_separate_groups():
    schema = {"type": "object", "required": ["a"],
              "properties": {"a": {"type": "string", "minLength": 3}}}
    # doc1: a 为整数 -> type 失败；doc2: a 太短 -> minLength 失败
    result = profile_documents(schema, [{"a": 1}, {"a": "x"}])
    validators = sorted(g["validator"] for g in result["failures"])
    assert validators == ["minLength", "type"]
    assert all(g["pointer"] == "#/a" for g in result["failures"])


def test_required_grouped_by_missing_subject():
    schema = {"type": "object", "required": ["x", "y"],
              "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}}
    # {} 缺 x 和 y；{"y": 3} 只缺 x -> 缺 x: 2 次，缺 y: 1 次
    result = profile_documents(schema, [{}, {"y": 3}, {"x": 1, "y": 2}])
    groups = result["failures"]
    assert all(g["validator"] == "required" for g in groups)
    counts = sorted(g["count"] for g in groups)
    assert counts == [1, 2]
    missing_x = next(g for g in groups if g["count"] == 2)
    assert missing_x["pointer"] == "#"
    assert "x" in missing_x["message"]


def test_one_of_deep_common_path_leaf_rule():
    """所有 oneOf 分支在同一深层路径因同一规则失败 -> 定位到深层路径。"""
    schema = {
        "oneOf": [
            {"properties": {"a": {"type": "string"}}, "required": ["a"]},
            {"properties": {"a": {"type": "boolean"}}, "required": ["a"]},
        ]
    }
    result = profile_documents(schema, [{"a": 1}, {"a": 2}])
    assert len(result["failures"]) == 1
    group = result["failures"][0]
    assert group["pointer"] == "#/a"
    assert group["validator"] == "type"
    assert group["count"] == 2
    assert group["example_indices"] == [0, 1]


def test_one_of_branches_fail_at_different_paths_falls_back_to_lcp():
    """分支在不同路径失败 -> 最深公共路径（可能为根 #）。"""
    schema = {
        "anyOf": [
            {"properties": {"a": {"type": "string"}}, "required": ["a"]},
            {"properties": {"b": {"type": "integer"}}, "required": ["b"]},
        ]
    }
    errors = _top_errors(schema, {})
    assert len(errors) == 1 and errors[0].validator == "anyOf"
    record = resolve_error(errors[0])
    assert record["pointer"] == "#"
    assert record["validator"] == "anyOf"
    assert "anyOf" in record["message"]


def test_one_of_same_path_different_rules_uses_composition():
    """同一深层路径但规则不同（type vs const）-> 以 oneOf 为规则、路径为 LCP。"""
    schema = {
        "oneOf": [
            {"properties": {"a": {"type": "string"}}, "required": ["a"]},
            {"properties": {"a": {"const": "b"}}, "required": ["a"]},
        ]
    }
    result = profile_documents(schema, [{"a": 10}])
    group = result["failures"][0]
    assert group["pointer"] == "#/a"
    assert group["validator"] == "oneOf"
    assert "type" in group["message"] and "const" in group["message"]


def test_nested_one_of_collects_all_leaves():
    """嵌套 oneOf：递归收集所有叶子失败路径再取 LCP。"""
    schema = {
        "oneOf": [
            {"oneOf": [{"properties": {"a": {"type": "string"}}, "required": ["a"]},
                       {"properties": {"a": {"type": "boolean"}}, "required": ["a"]}]},
            {"properties": {"a": {"minimum": 100}}, "required": ["a"]},
        ]
    }
    errors = _top_errors(schema, {"a": 1})
    assert errors[0].validator == "oneOf"
    record = resolve_error(errors[0])
    # 叶子路径全是 /a，规则为 type/type/minimum -> 身份不一致 -> oneOf
    assert record["pointer"] == "#/a"
    assert record["validator"] == "oneOf"


def test_nested_composition_in_property_uses_document_path():
    """属性内部的 anyOf 失败：LCP 必须包含文档路径 #/x。"""
    schema = {
        "type": "object",
        "properties": {
            "x": {
                "anyOf": [
                    {"type": "object", "required": ["p"]},
                    {"type": "object", "required": ["q"]},
                ]
            }
        },
        "required": ["x"],
    }
    result = profile_documents(schema, [{"x": {}}, {"x": {"r": 1}}])
    group = result["failures"][0]
    assert group["pointer"] == "#/x"
    assert group["validator"] == "anyOf"
    assert group["count"] == 2


def test_fingerprint_stable_across_documents_and_order():
    schema = {
        "type": "object",
        "properties": {
            "kind": {
                "oneOf": [
                    {"type": "object", "properties": {"t": {"const": "a"},
                                                     "v": {"type": "string"}},
                     "required": ["t", "v"]},
                    {"type": "object", "properties": {"t": {"const": "b"},
                                                     "v": {"type": "integer"}},
                     "required": ["t", "v"]},
                ]
            }
        },
    }
    docs = [
        {"kind": {"t": "a", "v": 1}},
        {"kind": {"t": "a", "v": 2}},
        {"kind": {"t": "a", "v": True}},
    ]
    forward = profile_documents(schema, docs)
    backward = profile_documents(schema, list(reversed(docs)))

    def keys(result):
        return [(g["pointer"], g["validator"], g["fingerprint"], g["count"],
                 g["message"]) for g in result["failures"]]

    assert keys(forward) == keys(backward)
    group = forward["failures"][0]
    assert group["count"] == 3
    # 示例下标始终按提交顺序升序展示
    assert forward["failures"][0]["example_indices"] == [0, 1, 2]
    assert backward["failures"][0]["example_indices"] == [0, 1, 2]


def test_each_document_contributes_at_most_once_per_fingerprint():
    # 数组里多个坏元素会产生多条 minItems? 这里用 additionalProperties：
    # 同一文档多个未声明键，jsonschema 只报一条；再用 required 多字段验证
    schema = {"type": "object", "required": ["a", "b"],
              "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}}
    result = profile_documents(schema, [{}])
    required_groups = [g for g in result["failures"] if g["validator"] == "required"]
    for group in required_groups:
        assert group["count"] == 1
        assert group["example_indices"] == [0]


def test_non_composition_error_records_schema_pointer():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    errors = _top_errors(schema, {"a": 1})
    record = resolve_error(errors[0])
    assert record["pointer"] == "#/a"
    assert record["validator"] == "type"
    assert record["schema_pointer"] == "#/properties/a/type"


def test_root_pointer_rendering():
    schema = {"type": "string"}
    result = profile_documents(schema, ["valid"])
    assert result["failures"] == []
    result = profile_documents(schema, [{"nested": "object"}])
    group = result["failures"][0]
    assert group["pointer"] == "#"
    assert group["validator"] == "type"


def test_enum_value_noise_does_not_split_groups():
    schema = {"enum": ["a", "b", "c"]}
    result = profile_documents(schema, ["x", "y", "z", "a"])
    assert result["valid_documents"] == 1
    assert len(result["failures"]) == 1
    assert result["failures"][0]["count"] == 3
    # 指纹不受实际取值影响
    assert failure_fingerprint({"pointer": "#", "validator": "enum", "subject": ""}) == \
        result["failures"][0]["fingerprint"]


def test_additional_properties_grouped_by_key():
    schema = {"type": "object", "additionalProperties": False}
    result = profile_documents(schema, [{"x": 1}, {"x": 2}, {"y": 3}])
    groups = sorted(result["failures"], key=lambda g: g["message"])
    assert len(groups) == 2
    by_key = {g["count"]: g for g in groups}
    assert by_key[2]["count"] == 2
    assert "x" in by_key[2]["message"]


def test_groups_sorted_deterministically():
    schema = {
        "type": "object",
        "required": ["z", "a"],
        "properties": {
            "z": {"type": "integer"},
            "a": {"type": "integer", "minimum": 5},
        },
    }
    result = profile_documents(schema, [{"z": "bad", "a": 1}])
    pointers = [g["pointer"] for g in result["failures"]]
    assert pointers == sorted(pointers)


def test_max_documents_constant():
    assert MAX_PROFILE_DOCUMENTS == 5000


def test_result_is_json_serializable():
    schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
    result = profile_documents(schema, [[], {}])
    json.dumps(result, ensure_ascii=False)
