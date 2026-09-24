"""样本变异覆盖引擎单元测试。"""
import pytest

from app import mutation
from app.mutation import DEFAULT_MAX_PER_RULE, RULE_NAMES


# ---------------------------------------------------------------------------
# 变异生成：规则覆盖
# ---------------------------------------------------------------------------


def _by_rule(schema, limits=None):
    mutants = mutation.generate_mutations(schema, limits or {})
    grouped: dict[str, list[dict]] = {}
    for mutant in mutants:
        grouped.setdefault(mutant["rule"], []).append(mutant)
    return grouped


def test_generates_required_enum_number_type_mutations():
    schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["a", "b", "c"]},
            "age": {"type": "integer", "minimum": 0, "maximum": 9},
            "kind": {"type": ["string", "integer"]},
        },
        "required": ["status", "age"],
    }
    grouped = _by_rule(schema)

    required = grouped["required_removed"]
    assert {m["subject"] for m in required} == {"status", "age"}
    assert all(m["pointer"] == "#" for m in required)

    enum_mut = grouped["enum_narrowed"]
    assert {m["subject"] for m in enum_mut} == {'"a"', '"b"', '"c"'}
    assert {m["pointer"] for m in enum_mut} == {"#/properties/status"}

    minimum = grouped["minimum_increased"]
    assert len(minimum) == 1 and minimum[0]["subject"] == "minimum"
    assert "0" in minimum[0]["description"] and "1" in minimum[0]["description"]

    maximum = grouped["maximum_decreased"]
    assert len(maximum) == 1

    types = grouped["type_narrowed"]
    # ["string", "integer"] 互不为超集：删除任一个都可区分
    assert [m["subject"] for m in types] == ["integer", "string"]


def test_length_mutations_on_strings_arrays_objects():
    schema = {
        "type": "object",
        "properties": {
            "s": {"type": "string", "minLength": 2, "maxLength": 8},
            "arr": {"type": "array", "minItems": 1, "maxItems": 3,
                    "items": {"type": "integer"}},
            "obj": {"type": "object", "minProperties": 1, "maxProperties": 2},
        },
    }
    grouped = _by_rule(schema)
    assert {m["subject"] for m in grouped["min_length_increased"]} == {"minLength"}
    assert {m["subject"] for m in grouped["max_length_decreased"]} == {"maxLength"}
    assert {m["subject"] for m in grouped["min_items_increased"]} == {"minItems"}
    assert {m["subject"] for m in grouped["max_items_decreased"]} == {"maxItems"}
    assert {m["subject"] for m in grouped["min_properties_increased"]} == {"minProperties"}
    assert {m["subject"] for m in grouped["max_properties_decreased"]} == {"maxProperties"}

    paths = {m["pointer"] for m in grouped["min_items_increased"]}
    assert paths == {"#/properties/arr"}


def test_mutations_inside_nested_arrays_and_branches():
    schema = {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "v": {"type": "string", "enum": ["x", "y"]},
                    },
                    "required": ["v"],
                },
            },
            "choice": {"anyOf": [
                {"type": "object", "properties": {"t": {"const": "a"}},
                 "required": ["t"]},
                {"type": "object", "properties": {"t": {"const": "b"}},
                 "required": ["t"]},
            ]},
        },
    }
    grouped = _by_rule(schema)
    nested_required = {m["pointer"] for m in grouped["required_removed"]}
    assert "#/properties/rows/items" in nested_required
    assert "#/properties/choice/anyOf/0" in nested_required
    assert "#/properties/choice/anyOf/1" in nested_required
    enum_paths = {m["pointer"] for m in grouped["enum_narrowed"]}
    assert enum_paths == {"#/properties/rows/items/properties/v"}


def test_not_if_and_defs_subtrees_are_not_mutated():
    schema = {
        "type": "object",
        "if": {"required": ["triggered"]},
        "then": {"properties": {"v": {"enum": ["a", "b"]}}},
        "not": {"type": "object", "required": ["evil"]},
        "$defs": {"unused": {"required": ["hidden"],
                             "properties": {"hidden": {"enum": [1, 2]}}}},
    }
    mutants = mutation.generate_mutations(schema, {})
    for mutant in mutants:
        assert not mutant["pointer"].startswith("#/not")
        assert not mutant["pointer"].startswith("#/if")
        assert not mutant["pointer"].startswith("#/$defs")


def test_refs_are_resolved_before_mutating():
    schema = {
        "type": "object",
        "properties": {
            "kind": {"$ref": "#/$defs/kind"},
        },
        "$defs": {
            "kind": {"type": "string", "enum": ["a", "b"]},
        },
    }
    grouped = _by_rule(schema)
    # 内联解析后的 properties/kind 上应能生成枚举收窄变异
    assert {m["pointer"] for m in grouped["enum_narrowed"]} == {"#/properties/kind"}


# ---------------------------------------------------------------------------
# 等价变异预检
# ---------------------------------------------------------------------------


def test_equivalent_enum_and_type_mutants_filtered():
    schema = {
        "type": "object",
        "properties": {
            # 唯一无法通过 type 校验的枚举值 -> 删除它是等价变异
            "e": {"type": "integer", "enum": [1, "x"]},
            # integer ∈ number：删 number 可区分（小数），删 integer 等价
            "n": {"type": ["integer", "number"]},
        },
    }
    grouped = _by_rule(schema)
    assert [m["subject"] for m in grouped["enum_narrowed"]] == ["1"]
    assert [m["subject"] for m in grouped["type_narrowed"]] == ["number"]


def test_empty_and_singleton_enums_not_mutated():
    schema = {"type": "object", "properties": {"a": {"enum": ["only"]}}}
    assert mutation.generate_mutations(schema, {}) == []


def test_no_mutations_on_unconstrained_schema():
    assert mutation.generate_mutations({"type": "object"}, {}) == []


def test_mutant_ids_are_stable_and_unique():
    schema = {
        "type": "object",
        "properties": {"s": {"enum": ["a", "b"]}},
        "required": ["s"],
    }
    first = mutation.generate_mutations(schema, {})
    second = mutation.generate_mutations(schema, {})
    assert [m["id"] for m in first] == [m["id"] for m in second]
    assert all(len(m["id"]) == 12 for m in first)
    assert len({m["id"] for m in first}) == len(first)


# ---------------------------------------------------------------------------
# 每类上限
# ---------------------------------------------------------------------------


def test_per_rule_limit_truncates_deterministically():
    schema = {
        "type": "object",
        "properties": {f"p{i}": {"type": "string", "enum": ["a", "b"]}
                       for i in range(5)},
    }
    full = mutation.generate_mutations(schema, {})
    assert len(full) == 10  # 5 个属性 × 2 个枚举值

    capped = mutation.generate_mutations(schema, {"enum_narrowed": 3})
    enum_mut = [m for m in capped if m["rule"] == "enum_narrowed"]
    assert len(enum_mut) == 3
    # 按 (pointer, subject) 稳定截断：每个属性含 "a"/"b" 两个变异，
    # 前 3 个正好是 p0 的两个 + p1 的第一个
    assert sorted({m["pointer"] for m in enum_mut}) == [
        "#/properties/p0", "#/properties/p1",
    ]

    # 0 表示关闭该类变异
    assert mutation.generate_mutations(schema, {"enum_narrowed": 0}) == []


# ---------------------------------------------------------------------------
# 杀死判定与评分
# ---------------------------------------------------------------------------


FULL_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled", "pending"]},
        "age": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": ["status", "age"],
}


def test_tightening_mutants_killed_by_positive_samples():
    documents = [
        {"status": "pending", "age": 0},
        {"status": "active", "age": 100},
    ]
    result = mutation.evaluate_mutation_coverage(FULL_SCHEMA, documents)

    rules = {r["rule"]: r for r in result["rules"]}
    assert rules["minimum_increased"]["killed"] == 1
    assert rules["maximum_decreased"]["killed"] == 1
    # active / pending 被覆盖，disabled 未覆盖
    enum_rule = rules["enum_narrowed"]
    assert enum_rule["total"] == 3 and enum_rule["killed"] == 2
    assert enum_rule["mutation_score"] == pytest.approx(2 / 3)

    surviving_enum = [m for m in result["mutants"]
                      if m["rule"] == "enum_narrowed" and not m["killed"]]
    assert len(surviving_enum) == 1
    assert surviving_enum[0]["subject"] == '"disabled"'
    assert surviving_enum[0]["example_indices"] == []

    assert "#/properties/status" in result["uncovered_paths"]


def test_required_removal_killed_only_by_negative_samples():
    documents = [
        {"status": "active", "age": 1},   # 正向：杀不死 required_removed
        {"status": "active"},             # 缺 age 的负向样本
        {"age": 1},                       # 缺 status 的负向样本
    ]
    result = mutation.evaluate_mutation_coverage(FULL_SCHEMA, documents)
    assert result["valid_documents"] == 1
    assert result["invalid_documents"] == 2

    required = {m["subject"]: m for m in result["mutants"]
                if m["rule"] == "required_removed"}
    assert required["age"]["killed"] is True
    assert required["age"]["example_indices"] == [1]
    assert required["status"]["killed"] is True
    assert required["status"]["example_indices"] == [2]


def test_example_indices_capped_and_sorted():
    # 两篇文档都落在边界上，按提交顺序记录，超过上限截断
    documents = [{"status": "pending", "age": 0}] * (mutation.MAX_KILL_EXAMPLE_INDICES + 5)
    result = mutation.evaluate_mutation_coverage(FULL_SCHEMA, documents)
    minimum = next(m for m in result["mutants"] if m["rule"] == "minimum_increased")
    assert minimum["killed"] is True
    assert len(minimum["example_indices"]) == mutation.MAX_KILL_EXAMPLE_INDICES
    assert minimum["example_indices"] == list(range(mutation.MAX_KILL_EXAMPLE_INDICES))


def test_overall_score_and_counts_consistent():
    documents = [{"status": "active", "age": 50}]  # 只覆盖 active
    result = mutation.evaluate_mutation_coverage(FULL_SCHEMA, documents)
    total = result["total_mutations"]
    killed = result["killed_mutations"]
    assert result["surviving_mutations"] == total - killed
    assert result["mutation_score"] == pytest.approx(killed / total)
    assert len(result["mutants"]) == total
    assert len(result["uncovered_paths"]) == total - killed
    # 每条规则都出现在 rules 中且顺序固定
    assert [r["rule"] for r in result["rules"]] == list(RULE_NAMES)
    for rule in result["rules"]:
        if rule["total"] == 0:
            assert rule["mutation_score"] is None
        else:
            assert rule["mutation_score"] == pytest.approx(
                rule["killed"] / rule["total"]
            )


def test_empty_mutant_set_scores_one():
    # 无约束 schema：没有可生成的变异体，覆盖率约定为 1.0
    result = mutation.evaluate_mutation_coverage({"type": "object"}, [{}])
    assert result["total_mutations"] == 0
    assert result["killed_mutations"] == 0
    assert result["mutation_score"] == 1.0
    assert result["uncovered_paths"] == []
    assert all(r["mutation_score"] is None for r in result["rules"])


def test_limits_reflected_in_response():
    result = mutation.evaluate_mutation_coverage(
        FULL_SCHEMA, [{"status": "active", "age": 1}],
        {"enum_narrowed": 1, "required_removed": 0},
    )
    assert result["limits"]["enum_narrowed"] == 1
    assert result["limits"]["required_removed"] == 0
    assert result["limits"]["minimum_increased"] == DEFAULT_MAX_PER_RULE
    rules = {r["rule"]: r for r in result["rules"]}
    assert rules["enum_narrowed"]["total"] == 1
    assert rules["required_removed"]["total"] == 0


def test_results_are_deterministic():
    documents = [{"status": "pending", "age": 5},
                 {"status": "active", "age": 99},
                 {"status": "active"}]
    a = mutation.evaluate_mutation_coverage(FULL_SCHEMA, documents)
    b = mutation.evaluate_mutation_coverage(FULL_SCHEMA, documents)
    assert a == b
    keys = [(m["rule"], m["pointer"], m["subject"]) for m in a["mutants"]]
    assert keys == sorted(keys)


def test_input_schema_and_documents_are_not_mutated():
    import copy

    schema = copy.deepcopy(FULL_SCHEMA)
    documents = [{"status": "pending", "age": 0}, {"status": "active"}]
    docs_snapshot = copy.deepcopy(documents)
    mutation.evaluate_mutation_coverage(schema, documents)
    assert schema == FULL_SCHEMA
    assert documents == docs_snapshot


def test_draft04_boolean_exclusive_minimum_boundary():
    schema = {"type": "number", "minimum": 5, "exclusiveMinimum": True}
    result = mutation.evaluate_mutation_coverage(schema, [6, 5])
    # 6 合法 -> 下界收紧后被拒绝（杀死）；5 原本非法，不参与区分
    mutant = next(m for m in result["mutants"] if m["rule"] == "minimum_increased")
    assert mutant["killed"] is True
    assert mutant["example_indices"] == [0]
