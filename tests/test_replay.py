"""样本回放诊断引擎单元测试。

覆盖：四类文档分类、结构 issue 归因（required / enum / 边界 / false 子
schema / oneOf / $ref / 元组）、每文档去重与示例下标、无法归因错误
（no_matching_issue / rejected_by_both）、以及报告 issue 全量顺序输出。
"""
import pytest

from app import compat, replay


# ---------------------------------------------------------------------------
# 分类计数
# ---------------------------------------------------------------------------


def test_classifies_documents_into_four_buckets():
    old = {"type": "object",
           "properties": {"s": {"type": "string", "enum": ["a", "b"]}}}
    new = {"type": "object",
           "properties": {"s": {"type": "string", "enum": ["a"]}},
           "required": ["s"]}
    report = compat.analyze(old, new)

    result = replay.replay_documents(old, new, report, [
        {"s": "b"},   # 0: 旧合法新拒绝 -> backward（enum）
        {},           # 1: 旧合法新拒绝 -> backward（required）
        {"s": "a"},   # 2: 两版都合法 -> valid
        42,           # 3: 两版都拒绝 -> invalid_both
    ])

    assert result["total_documents"] == 4
    assert result["counts"] == {
        "valid": 1, "backward": 2, "forward": 0, "invalid_both": 1,
    }


def test_forward_direction_classification():
    # 旧版必填 email，新版改为可选：新版合法的 {"s": 1} 被旧版拒绝
    old = {"type": "object", "required": ["email"],
           "properties": {"email": {"type": "string"}}}
    new = {"type": "object", "properties": {"email": {"type": "string"}}}
    report = compat.analyze(old, new)

    result = replay.replay_documents(old, new, report, [{"email": "a@b.c"}, {}])
    assert result["counts"] == {
        "valid": 1, "backward": 0, "forward": 1, "invalid_both": 0,
    }
    hit = [h for h in result["issue_hits"] if h["count"]]
    assert len(hit) == 1
    assert hit[0]["direction"] == "forward"
    assert hit[0]["path"] == "#"
    assert hit[0]["code"] == "required_added"
    assert hit[0]["example_indices"] == [1]


def test_all_valid_documents():
    schema = {"type": "object", "required": ["a"]}
    report = compat.analyze(schema, schema)
    result = replay.replay_documents(schema, schema, report, [{"a": 1}, {"a": 2}])
    assert result["counts"] == {
        "valid": 2, "backward": 0, "forward": 0, "invalid_both": 0,
    }
    assert all(h["count"] == 0 for h in result["issue_hits"])
    assert result["unattributed"] == []


# ---------------------------------------------------------------------------
# 结构 issue 归因
# ---------------------------------------------------------------------------


def test_attributes_required_and_enum_hits():
    old = {"type": "object",
           "properties": {"s": {"type": "string", "enum": ["a", "b", "c"]}}}
    new = {"type": "object",
           "properties": {"s": {"type": "string", "enum": ["a", "b"]}},
           "required": ["s"]}
    report = compat.analyze(old, new)

    result = replay.replay_documents(old, new, report, [
        {"s": "c"},   # enum
        {"s": "b"},   # valid
        {},           # required
        {"s": "c"},   # enum 再次命中
    ])

    by_key = {(h["path"], h["code"]): h for h in result["issue_hits"]}
    enum_hit = by_key[("#/properties/s", "enum_value_removed")]
    assert enum_hit["count"] == 2
    assert enum_hit["example_indices"] == [0, 3]
    required_hit = by_key[("#", "required_added")]
    assert required_hit["count"] == 1
    assert required_hit["example_indices"] == [2]


def test_attributes_numeric_and_string_bound_hits():
    old = {"type": "object", "properties": {
        "n": {"type": "integer"},
        "s": {"type": "string"},
    }}
    new = {"type": "object", "properties": {
        "n": {"type": "integer", "minimum": 10},
        "s": {"type": "string", "maxLength": 2},
    }}
    report = compat.analyze(old, new)

    result = replay.replay_documents(old, new, report, [
        {"n": 1, "s": "abc"},  # minimum + maxLength 同时命中
        {"n": 11, "s": "ok"},  # valid
    ])
    by_key = {(h["path"], h["code"]): h for h in result["issue_hits"]}
    assert by_key[("#/properties/n", "minimum_increased")]["count"] == 1
    assert by_key[("#/properties/n", "minimum_increased")]["example_indices"] == [0]
    assert by_key[("#/properties/s", "max_length_decreased")]["count"] == 1


def test_attributes_false_subschema_via_prewalk():
    # jsonschema 对布尔 false 子 schema 的错误不带路径，引擎预扫描定位
    old = {"type": "object", "properties": {"f": {"type": "number"}}}
    new = {"type": "object", "properties": {"f": False}}
    report = compat.analyze(old, new)

    result = replay.replay_documents(old, new, report, [{"f": 1}, {}, {"f": 2}])
    hit = [h for h in result["issue_hits"] if h["code"] == "schema_forbids_all"]
    assert len(hit) == 1
    assert hit[0]["path"] == "#/properties/f"
    assert hit[0]["count"] == 2
    assert hit[0]["example_indices"] == [0, 2]


def test_attributes_false_subschema_through_ref():
    old = {"$defs": {"p": {"type": "number"}}, "type": "object",
           "properties": {"a": {"$ref": "#/$defs/p"}}}
    new = {"$defs": {"p": False}, "type": "object",
           "properties": {"a": {"$ref": "#/$defs/p"}}}
    report = compat.analyze(old, new)

    result = replay.replay_documents(old, new, report, [{"a": 1}])
    hit = [h for h in result["issue_hits"] if h["count"]]
    assert len(hit) == 1
    assert hit[0]["path"] == "#/properties/a"
    assert hit[0]["code"] == "schema_forbids_all"


def test_attributes_oneof_deep_leaf_to_composition_issue():
    # 分析器只在组合节点上报 oneOf 不兼容，分支内深层叶子失败也应归因到它
    old = {"type": "object", "properties": {"kind": True}}
    new = {"type": "object", "properties": {
        "kind": {"oneOf": [{"type": "string"}, {"type": "null"}]}}}
    report = compat.analyze(old, new)

    result = replay.replay_documents(old, new, report, [{"kind": 5}, {"kind": "x"}])
    hit = [h for h in result["issue_hits"] if h["count"]]
    assert len(hit) == 1
    assert hit[0]["path"] == "#/properties/kind"
    assert hit[0]["code"] == "oneOf_branch_missing"
    assert hit[0]["count"] == 1
    assert hit[0]["example_indices"] == [0]
    assert result["unattributed"] == []


def test_attributes_legacy_tuple_and_2020_prefix_items():
    old7 = {"items": [{"type": "integer"}]}
    new7 = {"items": [{"type": "string"}]}
    report7 = compat.analyze(old7, new7)
    result7 = replay.replay_documents(old7, new7, report7, [[1]])
    hit7 = [h for h in result7["issue_hits"] if h["direction"] == "backward" and h["count"]]
    assert len(hit7) == 1
    assert hit7[0]["path"] == "#/items/0"
    assert hit7[0]["code"] == "type_removed"

    old2020 = {"$schema": "https://json-schema.org/draft/2020-12/schema",
               "prefixItems": [{"type": "integer"}]}
    new2020 = {"$schema": "https://json-schema.org/draft/2020-12/schema",
               "prefixItems": [{"type": "string"}]}
    report20 = compat.analyze(old2020, new2020)
    result20 = replay.replay_documents(old2020, new2020, report20, [[1]])
    hit20 = [h for h in result20["issue_hits"] if h["direction"] == "backward" and h["count"]]
    assert len(hit20) == 1
    assert hit20[0]["path"] == "#/prefixItems/0"


def test_attributes_additional_properties_forbidden():
    old = {"type": "object", "properties": {"a": {}}}
    new = {"type": "object", "properties": {"a": {}},
           "additionalProperties": False}
    report = compat.analyze(old, new)

    result = replay.replay_documents(old, new, report, [{"a": 1, "extra": 2}])
    hit = [h for h in result["issue_hits"] if h["count"]]
    assert len(hit) == 1
    assert hit[0]["path"] == "#"
    assert hit[0]["code"] == "additional_properties_forbidden"


def test_warning_issue_is_attributed_too():
    # pattern 变化只能降级 warning，但回放命中时仍应归因
    old = {"type": "object", "properties": {"s": {"type": "string"}}}
    new = {"type": "object",
           "properties": {"s": {"type": "string", "pattern": "^x"}}}
    report = compat.analyze(old, new)
    assert any(i["severity"] == "warning" for i in report["issues"])

    result = replay.replay_documents(old, new, report, [{"s": "no"}])
    hit = [h for h in result["issue_hits"] if h["count"]]
    assert len(hit) == 1
    assert hit[0]["code"] == "pattern_changed"
    assert hit[0]["severity"] == "warning"


def test_sample_fallback_uses_instance_pointer():
    # 分析器无结构 issue（contains 未覆盖），报告仅含样本 issue（实例路径）
    old = {"type": "object",
           "properties": {"arr": {"type": "array", "items": {}}}}
    new = {"type": "object", "properties": {
        "arr": {"type": "array", "items": {}, "contains": {"const": "x"}}}}
    report = compat.analyze(old, new, samples=[{"arr": []}])
    assert any(i["code"] == "sample_rejected_by_new" for i in report["issues"])

    result = replay.replay_documents(old, new, report, [{"arr": ["y"]}])
    hit = [h for h in result["issue_hits"] if h["count"]]
    assert len(hit) == 1
    assert hit[0]["path"] == "#/arr"
    assert hit[0]["code"] == "sample_rejected_by_new"
    assert result["unattributed"] == []


# ---------------------------------------------------------------------------
# 去重、示例下标与顺序无关性
# ---------------------------------------------------------------------------


def test_same_issue_counts_once_per_document():
    # 一个文档同时缺多个必填字段，required_added 也只计一次
    old = {"type": "object"}
    new = {"type": "object", "required": ["a", "b", "c"]}
    report = compat.analyze(old, new)

    result = replay.replay_documents(old, new, report, [{}])
    hit = [h for h in result["issue_hits"] if h["code"] == "required_added"][0]
    assert hit["count"] == 1
    assert hit["example_indices"] == [0]


def test_distinct_required_issues_distinguished_by_subject():
    # 新版同时新增两个根级必填字段：报告含两条同 (path, code) 的 issue，
    # 回放必须按缺失字段名分别归因，而不是都归到第一条
    old = {"type": "object",
           "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    new = {"type": "object",
           "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
           "required": ["a", "b"]}
    report = compat.analyze(old, new)
    required_issues = [i for i in report["issues"] if i["code"] == "required_added"]
    assert len(required_issues) == 2

    result = replay.replay_documents(old, new, report, [
        {"a": "x"},   # 缺 b
        {"b": "y"},   # 缺 a
        {},           # 两个都缺：两条 issue 同时命中，各自只计一次
    ])
    hits = [h for h in result["issue_hits"] if h["code"] == "required_added"]
    assert len(hits) == 2
    for hit in hits:
        assert hit["count"] == 2
    # 每篇文档在每条 issue 上只计一次（count=2 而非 3）
    assert sorted(hits[0]["example_indices"]) == [0, 2] or \
        sorted(hits[0]["example_indices"]) == [1, 2]
    assert result["unattributed"] == []


def test_example_indices_capped_and_order_preserved():
    from app.profile import MAX_EXAMPLE_INDICES

    old = {"type": "object"}
    new = {"type": "object", "required": ["a"]}
    report = compat.analyze(old, new)

    documents = [{}] * (MAX_EXAMPLE_INDICES + 5) + [{"a": 1}]
    result = replay.replay_documents(old, new, report, documents)
    hit = [h for h in result["issue_hits"] if h["code"] == "required_added"][0]
    assert hit["count"] == MAX_EXAMPLE_INDICES + 5
    assert len(hit["example_indices"]) == MAX_EXAMPLE_INDICES
    assert hit["example_indices"] == list(range(MAX_EXAMPLE_INDICES))


def test_issue_hits_follow_report_order_including_zero_hits():
    old = {"type": "object", "properties": {
        "s": {"type": "string", "enum": ["a", "b"]}}}
    new = {"type": "object", "required": ["s"], "properties": {
        "s": {"type": "string", "enum": ["a"]}}}
    report = compat.analyze(old, new)

    result = replay.replay_documents(old, new, report, [{"s": "a"}])
    assert [(h["direction"], h["path"], h["code"]) for h in result["issue_hits"]] == \
        [(i["direction"], i["path"], i["code"]) for i in report["issues"]]
    assert all(h["count"] == 0 for h in result["issue_hits"])


def test_input_order_does_not_change_groups():
    old = {"type": "object", "properties": {
        "n": {"type": "integer"}, "s": {"type": "string"}}}
    new = {"type": "object", "properties": {
        "n": {"type": "integer", "minimum": 10},
        "s": {"type": "string", "maxLength": 2}}}
    report = compat.analyze(old, new)

    docs = [{"n": 1}, {"n": 2}, {"s": "toolong"}]
    a = replay.replay_documents(old, new, report, docs)
    b = replay.replay_documents(old, new, report, list(reversed(docs)))

    def signature(result):
        return [
            (h["direction"], h["path"], h["code"], h["count"])
            for h in result["issue_hits"] if h["count"]
        ] + [
            (u["direction"], u["reason"], u["pointer"], u["validator"], u["count"])
            for u in result["unattributed"]
        ]

    assert signature(a) == signature(b)


# ---------------------------------------------------------------------------
# 无法归因
# ---------------------------------------------------------------------------


def test_rejected_by_both_collected_as_unattributed():
    old = {"type": "object", "properties": {"s": {"type": "string"}}}
    new = {"type": "object", "properties": {"s": {"type": "string"}}}
    report = compat.analyze(old, new)  # 完全兼容，报告无 issue

    result = replay.replay_documents(old, new, report, [
        {"s": 1},     # 两版都拒绝：type
        {"s": "x"},   # valid
        42,           # 两版都拒绝：根 type
    ])
    assert result["counts"]["invalid_both"] == 2
    assert result["counts"]["valid"] == 1
    reasons = {(u["pointer"], u["validator"]): u for u in result["unattributed"]}
    assert ("#/properties/s", "type") in reasons
    assert ("#", "type") in reasons
    for group in result["unattributed"]:
        assert group["direction"] == "both"
        assert group["reason"] == "rejected_by_both"
        assert group["count"] == 1


def test_no_matching_issue_when_report_silent():
    # 新增 additionalProperties:{"type": "integer"} 收紧，额外键类型错误；
    # 该结构分析器不报 issue 时，单向失败应落入 no_matching_issue
    old = {"type": "object", "properties": {"a": {}},
           "additionalProperties": {"type": "integer"}}
    new = {"type": "object", "properties": {"a": {}},
           "additionalProperties": {"type": "integer", "minimum": 100}}
    report = compat.analyze(old, new)

    result = replay.replay_documents(old, new, report, [{"a": 1, "b": 5}])
    # 若分析器恰好报告了 minimum_increased，则应归因而不是未归因
    if any(i["path"] == "#/additionalProperties" and i["code"] == "minimum_increased"
           for i in report["issues"]):
        hit = [h for h in result["issue_hits"] if h["count"]]
        assert hit and hit[0]["code"] == "minimum_increased"
    else:
        assert result["counts"]["backward"] == 1
        un = result["unattributed"]
        assert len(un) == 1
        assert un[0]["reason"] == "no_matching_issue"
        assert un[0]["direction"] == "backward"


def test_unattributed_groups_merge_with_count_and_examples():
    # 相同未允许键集合在多个文档中违规 -> 合并为一组并累计计数；
    # 与画像指纹一致，额外键名作为规则主体（键集合不同不合并）。
    shared = {"type": "object", "required": ["only"],
              "properties": {"only": {"type": "string"}},
              "additionalProperties": False}
    report = compat.analyze(shared, shared)
    result = replay.replay_documents(shared, shared, report, [
        {"only": "a", "dup": 1},          # 仅 dup
        {"only": "b", "dup": 2},          # 仅 dup -> 与上同组
        {"only": "c", "dup": 3, "x": 4}, # dup + x -> 键集合不同，独立成组
    ])
    assert result["counts"]["invalid_both"] == 3
    ap = [u for u in result["unattributed"] if u["validator"] == "additionalProperties"]
    assert len(ap) == 2
    ap.sort(key=lambda u: u["count"])
    assert ap[1]["count"] == 2
    assert ap[1]["example_indices"] == [0, 1]
    assert ap[0]["count"] == 1
    assert ap[0]["example_indices"] == [2]


def test_unattributed_required_distinguishes_missing_field():
    # 不同缺失字段（规则主体不同）不应合并为同一组
    schema = {"type": "object", "required": ["a", "b"],
              "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    report = compat.analyze(schema, schema)
    result = replay.replay_documents(schema, schema, report, [{"a": "x"}, {"b": "y"}])
    required_groups = [u for u in result["unattributed"] if u["validator"] == "required"]
    assert len(required_groups) == 2


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_replay_does_not_mutate_inputs():
    old = {"type": "object"}
    new = {"type": "object", "required": ["a"]}
    report = compat.analyze(old, new)
    import copy

    old_copy, new_copy, report_copy, docs_copy = (
        copy.deepcopy(old), copy.deepcopy(new),
        copy.deepcopy(report), copy.deepcopy([{}]),
    )
    replay.replay_documents(old, new, report, [{}])
    assert old == old_copy
    assert new == new_copy
    assert report == report_copy
    assert [{}] == docs_copy
