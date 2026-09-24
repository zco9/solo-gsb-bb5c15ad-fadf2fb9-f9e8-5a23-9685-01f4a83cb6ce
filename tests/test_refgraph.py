"""引用依赖诊断引擎（app.refgraph）单元测试。"""
import pytest

from app import refgraph
from app.refgraph import analyze_references


def edge_set(result):
    return {(e["source"], e["target"]) for e in result["edges"]}


def invalid_map(result):
    return {item["source"]: item["reason"] for item in result["invalid_refs"]}


def unreachable_names(result):
    return [item["name"] for item in result["unreachable_defs"]]


def cycle_chains(result):
    return [cycle["chain"] for cycle in result["cycles"]]


class TestEmptyAndSimple:
    def test_no_refs_no_defs(self):
        result = analyze_references({"type": "object", "properties": {"a": {"type": "string"}}})
        assert result["counts"] == {
            "refs": 0, "edges": 0, "invalid_refs": 0,
            "unreachable_defs": 0, "cycles": 0,
        }
        assert result["edges"] == []
        assert result["invalid_refs"] == []
        assert result["unreachable_defs"] == []
        assert result["cycles"] == []

    def test_empty_defs(self):
        result = analyze_references({"$defs": {}, "type": "string"})
        assert result["unreachable_defs"] == []

    def test_root_self_reference(self):
        result = analyze_references({"$ref": "#"})
        assert edge_set(result) == {("#", "#")}
        assert cycle_chains(result) == [["#", "#"]]
        assert result["unreachable_defs"] == []

    def test_refs_inside_instance_keywords_are_ignored(self):
        # enum / const / default / examples 里的 $ref 是实例数据
        schema = {
            "type": "object",
            "enum": [{"$ref": "#/$defs/A"}],
            "const": {"$ref": "http://example.com/x"},
            "default": {"$ref": "#/$defs/B"},
            "examples": [[{"$ref": "#/x"}]],
            "$defs": {"A": {"type": "string"}},
        }
        result = analyze_references(schema)
        assert result["edges"] == []
        assert result["invalid_refs"] == []
        assert unreachable_names(result) == ["A"]


class TestEdgesAndResolution:
    def test_basic_local_refs_into_defs(self):
        schema = {
            "type": "object",
            "properties": {"name": {"$ref": "#/$defs/Name"}, "arr": {"items": {"$ref": "#/$defs/Name"}}},
            "$defs": {"Name": {"type": "string"}},
        }
        result = analyze_references(schema)
        assert edge_set(result) == {
            ("#/properties/name", "#/$defs/Name"),
            ("#/properties/arr/items", "#/$defs/Name"),
        }
        assert result["unreachable_defs"] == []

    def test_pointer_escape_round_trip(self):
        # 键名含 / 与 ~：~1 / ~0 转义；%2F 百分号解码后与 ~1 归一到同一目标
        schema = {
            "type": "object",
            "properties": {
                "slash": {"$ref": "#/$defs/a~1b"},
                "pct": {"$ref": "#/$defs/a%2Fb"},
                "tilde": {"$ref": "#/$defs/c~0d"},
            },
            "$defs": {"a/b": {"type": "integer"}, "c~d": {"type": "null"}},
        }
        result = analyze_references(schema)
        targets = {e["source"]: e["target"] for e in result["edges"]}
        assert targets["#/properties/slash"] == "#/$defs/a~1b"
        assert targets["#/properties/pct"] == "#/$defs/a~1b"
        assert targets["#/properties/tilde"] == "#/$defs/c~0d"
        assert result["invalid_refs"] == []

    def test_array_index_pointer(self):
        schema = {
            "type": "object",
            "allOf": [{}, {"$ref": "#/$defs/A"}],
            "$defs": {"A": {"type": "string"}},
        }
        result = analyze_references(schema)
        assert ("#/allOf/1", "#/$defs/A") in edge_set(result)

    def test_edges_sorted_by_source(self):
        schema = {
            "type": "object",
            "properties": {"z": {"$ref": "#/$defs/Z"}, "a": {"$ref": "#/$defs/A"}},
            "$defs": {"Z": True, "A": True},
        }
        result = analyze_references(schema)
        sources = [e["source"] for e in result["edges"]]
        assert sources == sorted(sources)


class TestInvalidReferences:
    def test_external_http_reference(self):
        schema = {"properties": {"a": {"$ref": "https://example.com/schema.json#/$defs/A"}}}
        result = analyze_references(schema)
        assert invalid_map(result) == {"#/properties/a": "external_reference"}
        assert result["edges"] == []

    def test_relative_file_reference(self):
        schema = {"properties": {"a": {"$ref": "other.json#/$defs/A"}}}
        result = analyze_references(schema)
        assert invalid_map(result)["#/properties/a"] == "external_reference"

    def test_plain_name_reference_without_hash(self):
        # 裸名称不是片段形式，同样拒绝且绝不尝试解析
        result = analyze_references({"properties": {"a": {"$ref": "defs.json"}}})
        assert invalid_map(result)["#/properties/a"] == "external_reference"

    def test_name_fragment_unsupported(self):
        # #anchor 名称片段不支持；#/ 后跟空段按指针解析失败处理
        schema = {"properties": {
            "a": {"$ref": "#myAnchor"},
            "b": {"$ref": "#/$defs/"},
        }}
        result = analyze_references(schema)
        reasons = invalid_map(result)
        assert reasons["#/properties/a"] == "name_fragment_unsupported"
        assert reasons["#/properties/b"] == "target_not_found"

    def test_missing_target_pointer(self):
        schema = {"properties": {"a": {"$ref": "#/$defs/Missing"}}}
        result = analyze_references(schema)
        item = result["invalid_refs"][0]
        assert item["reason"] == "target_not_found"
        assert item["ref"] == "#/$defs/Missing"
        assert "Missing" in item["message"]

    def test_missing_target_through_array(self):
        schema = {"properties": {"a": {"$ref": "#/items/3"}}, "items": [{"type": "string"}]}
        result = analyze_references(schema)
        assert invalid_map(result)["#/properties/a"] == "target_not_found"

    def test_array_index_forms(self):
        # 前导零非法、- 指向不存在的末位后成员、非数字非法
        schema = {
            "items": [{"type": "string"}],
            "properties": {
                "leading_zero": {"$ref": "#/items/01"},
                "dash": {"$ref": "#/items/-"},
                "out_of_range": {"$ref": "#/items/5"},
                "ok": {"$ref": "#/items/0"},
            },
        }
        result = analyze_references(schema)
        reasons = invalid_map(result)
        assert reasons["#/properties/leading_zero"] == "target_not_found"
        assert reasons["#/properties/dash"] == "target_not_found"
        assert reasons["#/properties/out_of_range"] == "target_not_found"
        assert ("#/properties/ok", "#/items/0") in edge_set(result)

    def test_malformed_pointer_escape(self):
        schema = {"properties": {"a": {"$ref": "#/$defs/~2"}}}
        result = analyze_references(schema)
        assert invalid_map(result)["#/properties/a"] == "malformed_pointer"

    def test_non_string_ref(self):
        # 非字符串 $ref（check_schema 在接口层会先 400，引擎自身仍稳健）
        result = analyze_references({"properties": {"a": {"$ref": 123}}})
        item = result["invalid_refs"][0]
        assert item["reason"] == "non_string_ref"
        assert item["ref"] is None

    def test_invalid_refs_do_not_form_edges(self):
        schema = {
            "properties": {"a": {"$ref": "#/$defs/Ok"}, "b": {"$ref": "#/$defs/Missing"}},
            "$defs": {"Ok": {"type": "string"}},
        }
        result = analyze_references(schema)
        assert len(result["edges"]) == 1
        assert len(result["invalid_refs"]) == 1
        assert result["counts"]["refs"] == 2


class TestUnreachableDefs:
    def test_all_reachable(self):
        schema = {
            "properties": {"a": {"$ref": "#/$defs/A"}},
            "$defs": {
                "A": {"properties": {"b": {"$ref": "#/$defs/B"}}},
                "B": {"type": "string"},
            },
        }
        result = analyze_references(schema)
        assert unreachable_names(result) == []

    def test_orphan_defs(self):
        schema = {
            "properties": {"a": {"$ref": "#/$defs/A"}},
            "$defs": {"A": True, "Orphan": True, "Orphan2": {"type": "null"}},
        }
        result = analyze_references(schema)
        assert unreachable_names(result) == ["Orphan", "Orphan2"]

    def test_orphan_chain_stays_unreachable(self):
        # Orphan2 -> Orphan：两者都只能从不可达定义到达
        schema = {
            "properties": {"a": {"$ref": "#/$defs/A"}},
            "$defs": {
                "A": True,
                "Orphan": True,
                "Orphan2": {"$ref": "#/$defs/Orphan"},
            },
        }
        result = analyze_references(schema)
        assert unreachable_names(result) == ["Orphan", "Orphan2"]

    def test_reachable_def_referencing_orphan_makes_it_reachable(self):
        schema = {
            "properties": {"a": {"$ref": "#/$defs/A"}},
            "$defs": {
                "A": {"properties": {"o": {"$ref": "#/$defs/Orphan"}}},
                "Orphan": True,
            },
        }
        result = analyze_references(schema)
        assert unreachable_names(result) == []

    def test_nested_defs_owned_by_top_level(self):
        # 根侧直接引用内部定义：Inner 被直接使用故可达；
        # 但其外层 nested 顶层定义本身未被引用，仍为不可达；
        # Reachable 也未被根侧引用，不可达。
        schema = {
            "properties": {"a": {"$ref": "#/$defs/nested/$defs/Inner"}},
            "$defs": {
                "nested": {"$defs": {"Inner": {"type": "string"}}},
                "Reachable": {"$ref": "#/$defs/nested/$defs/Inner"},
            },
        }
        result = analyze_references(schema)
        names = unreachable_names(result)
        assert "nested" in names
        assert "Reachable" in names
        assert "Inner" not in names

    def test_reachable_nested_container_keeps_unreferenced_inner_unreachable(self):
        # 根侧引用 nested（顶层定义可达）；其内部 Inner 声明了但从未被
        # 任何 $ref 使用 -> 仍是不可达的 $defs 节点（死定义）
        schema = {
            "properties": {"a": {"$ref": "#/$defs/nested"}},
            "$defs": {"nested": {"$defs": {"Inner": {"type": "string"}}}},
        }
        result = analyze_references(schema)
        pointers = [item["pointer"] for item in result["unreachable_defs"]]
        assert pointers == ["#/$defs/nested/$defs/Inner"]

    def test_inner_def_reached_from_reachable_owner(self):
        # 可达的 nested 内部引用自己的 Inner -> Inner 经引用链可达
        schema = {
            "properties": {"a": {"$ref": "#/$defs/nested"}},
            "$defs": {
                "nested": {
                    "type": "object",
                    "properties": {"i": {"$ref": "#/$defs/nested/$defs/Inner"}},
                    "$defs": {"Inner": {"type": "string"}},
                }
            },
        }
        result = analyze_references(schema)
        assert result["unreachable_defs"] == []

    def test_unreachable_sorted_by_pointer(self):
        schema = {
            "$defs": {"z": True, "a": True, "m": True},
        }
        result = analyze_references(schema)
        pointers = [item["pointer"] for item in result["unreachable_defs"]]
        assert pointers == sorted(pointers)


class TestCycles:
    def test_direct_mutual_reference(self):
        schema = {
            "properties": {"a": {"$ref": "#/$defs/A"}},
            "$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}},
        }
        result = analyze_references(schema)
        assert cycle_chains(result) == [
            ["#/$defs/A", "#/$defs/B", "#/$defs/A"]
        ]

    def test_self_cycle_through_own_property(self):
        # 树节点：内部 child 属性引用自身定义 -> 自环
        schema = {
            "properties": {"root": {"$ref": "#/$defs/Node"}},
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/$defs/Node"}},
                }
            },
        }
        result = analyze_references(schema)
        assert cycle_chains(result) == [["#/$defs/Node", "#/$defs/Node"]]

    def test_self_cycle_on_def_itself(self):
        schema = {"$defs": {"Loop": {"$ref": "#/$defs/Loop"}}}
        result = analyze_references(schema)
        assert cycle_chains(result) == [["#/$defs/Loop", "#/$defs/Loop"]]
        # 没有根侧引用，Loop 同时是不可达定义
        assert unreachable_names(result) == ["Loop"]

    def test_three_node_cycle_reported_once(self):
        schema = {
            "properties": {"a": {"$ref": "#/$defs/A"}},
            "$defs": {
                "A": {"$ref": "#/$defs/B"},
                "B": {"$ref": "#/$defs/C"},
                "C": {"$ref": "#/$defs/A"},
            },
        }
        result = analyze_references(schema)
        chains = cycle_chains(result)
        assert len(chains) == 1
        chain = chains[0]
        assert chain[0] == chain[-1]
        assert set(chain) == {"#/$defs/A", "#/$defs/B", "#/$defs/C", "#/$defs/A"}

    def test_root_self_reference_outside_defs(self):
        schema = {"properties": {"me": {"$ref": "#"}}, "$defs": {"Orphan": True}}
        result = analyze_references(schema)
        assert ["#/properties/me", "#"] in cycle_chains(result)
        assert unreachable_names(result) == ["Orphan"]

    def test_dangling_chain_is_not_a_cycle(self):
        # A -> B -> Missing：B 的引用无效，A/B 不成环
        schema = {
            "properties": {"a": {"$ref": "#/$defs/A"}},
            "$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/Missing"}},
        }
        result = analyze_references(schema)
        assert result["cycles"] == []


class TestDeterminism:
    def test_input_key_order_independent(self):
        schema1 = {
            "$defs": {"B": {"$ref": "#/$defs/A"}, "A": {"$ref": "#/$defs/B"}, "Z": True},
            "properties": {"b": {"$ref": "#/$defs/B"}, "a": {"$ref": "#/$defs/A"}},
        }
        schema2 = {
            "properties": {"a": {"$ref": "#/$defs/A"}, "b": {"$ref": "#/$defs/B"}},
            "$defs": {"A": {"$ref": "#/$defs/B"}, "Z": True, "B": {"$ref": "#/$defs/A"}},
        }
        assert analyze_references(schema1) == analyze_references(schema2)

    def test_input_not_mutated(self):
        schema = {"properties": {"a": {"$ref": "#/$defs/A"}}, "$defs": {"A": {"type": "string"}}}
        before = __import__("copy").deepcopy(schema)
        analyze_references(schema)
        assert schema == before


@pytest.mark.parametrize(
    "ref,expected_reason",
    [
        ("#/$defs/A", None),
        ("#", None),
        ("#anchor", "name_fragment_unsupported"),
        ("http://example.com/x#/$defs/A", "external_reference"),
        ("relative.json#/$defs/A", "external_reference"),
        ("urn:isbn:123", "external_reference"),
    ],
)
def test_classify_ref_cases(ref, expected_reason):
    root = {"$defs": {"A": {"type": "string"}}}
    target, invalid = refgraph._classify_ref(root, ref)
    if expected_reason is None:
        assert invalid is None
        assert target is not None
    else:
        assert target is None
        assert invalid["reason"] == expected_reason
