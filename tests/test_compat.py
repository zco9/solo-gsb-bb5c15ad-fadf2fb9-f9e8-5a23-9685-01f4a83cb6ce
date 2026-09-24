"""兼容性引擎测试。"""
import pytest

from app import compat


def analyze(old, new, samples=None):
    return compat.analyze(old, new, samples)


def codes(report, direction):
    return [i["code"] for i in report["issues"] if i["direction"] == direction]


class TestBasicCompatibility:
    def test_add_optional_field_is_compatible_both_ways(self):
        old = {"type": "object", "properties": {"a": {"type": "string"}}}
        new = {"type": "object", "properties": {"a": {"type": "string"},
                                               "b": {"type": "integer"}}}
        report = analyze(old, new)
        assert report["backward_compatible"]
        assert report["forward_compatible"]

    def test_add_required_field_breaks_backward_only(self):
        old = {"type": "object", "properties": {"a": {"type": "string"}}}
        new = {"type": "object", "properties": {"a": {"type": "string"},
                                                "b": {"type": "string"}},
               "required": ["b"]}
        report = analyze(old, new)
        assert not report["backward_compatible"]
        assert report["forward_compatible"]
        assert "required_added" in codes(report, "backward")

    def test_remove_required_field_breaks_forward_only(self):
        old = {"type": "object", "properties": {"a": {"type": "string"}},
               "required": ["a"]}
        new = {"type": "object", "properties": {"a": {"type": "string"}}}
        report = analyze(old, new)
        assert report["backward_compatible"]
        assert not report["forward_compatible"]


class TestType:
    def test_type_change_breaks_both(self):
        report = analyze({"type": "string"}, {"type": "integer"})
        assert not report["backward_compatible"]
        assert not report["forward_compatible"]
        assert codes(report, "backward") == ["type_removed"]
        assert codes(report, "forward") == ["type_removed"]

    def test_integer_to_number_backward_compatible(self):
        # number 是 integer 的超集
        report = analyze({"type": "integer"}, {"type": "number"})
        assert report["backward_compatible"]


class TestEnum:
    def test_enum_narrowed(self):
        report = analyze({"enum": ["a", "b", "c"]}, {"enum": ["a", "b"]})
        assert not report["backward_compatible"]
        assert report["forward_compatible"]
        issue = [i for i in report["issues"] if i["code"] == "enum_value_removed"][0]
        assert issue["counterexample"] == "c"

    def test_enum_widened_breaks_forward(self):
        report = analyze({"enum": ["a", "b"]}, {"enum": ["a", "b", "c"]})
        assert report["backward_compatible"]
        assert not report["forward_compatible"]


class TestConst:
    def test_const_added(self):
        report = analyze({"type": "string"}, {"const": "v1"})
        assert not report["backward_compatible"]
        issue = [i for i in report["issues"] if i["code"] == "const_added"][0]
        assert issue["counterexample"] is not None  # 空字符串也是合法反例

    def test_const_changed_breaks_both(self):
        report = analyze({"const": "a"}, {"const": "b"})
        assert not report["backward_compatible"]
        assert not report["forward_compatible"]


class TestArray:
    def test_homogeneous_item_type(self):
        report = analyze({"type": "array", "items": {"type": "string"}},
                         {"type": "array", "items": {"type": "integer"}})
        assert not report["backward_compatible"]
        assert not report["forward_compatible"]
        ce = [i for i in report["issues"] if i["direction"] == "backward"][0]["counterexample"]
        assert ce == [""]

    def test_min_items_increased(self):
        report = analyze({"type": "array"}, {"type": "array", "minItems": 2})
        assert not report["backward_compatible"]
        ce = [i for i in report["issues"]][0]["counterexample"]
        assert ce == []

    def test_max_items_decreased(self):
        report = analyze({"type": "array", "maxItems": 5},
                         {"type": "array", "maxItems": 2})
        assert not report["backward_compatible"]

    def test_unique_items_added(self):
        report = analyze({"type": "array", "items": {"type": "string"}},
                         {"type": "array", "items": {"type": "string"},
                          "uniqueItems": True})
        assert not report["backward_compatible"]

    def test_legacy_tuple_additional_items_false(self):
        old = {"type": "array", "items": [{"type": "string"}],
               "additionalItems": True}
        new = {"type": "array", "items": [{"type": "string"}],
               "additionalItems": False}
        report = analyze(old, new)
        assert not report["backward_compatible"]
        issue = report["issues"][0]
        assert issue["path"] == "#/items"
        # 反例必须是含一个超出前缀元素的数组
        assert len(issue["counterexample"]) == 2

    def test_legacy_tuple_unchanged_prefix_is_compatible(self):
        old = {"type": "array",
               "items": [{"type": "string"}, {"type": "integer"}]}
        new = {"type": "array",
               "items": [{"type": "string"}, {"type": "integer"}],
               "additionalItems": False}
        report = analyze(old, new)
        assert report["backward_compatible"]

    def test_2020_tuple_path_uses_prefix_items(self):
        old = {"type": "array", "prefixItems": [{"type": "string"}], "items": True}
        new = {"type": "array", "prefixItems": [{"type": "integer"}], "items": True}
        report = analyze(old, new)
        assert report["issues"][0]["path"] == "#/prefixItems/0"


class TestObject:
    def test_additional_properties_false(self):
        old = {"type": "object", "properties": {"a": {"type": "string"}}}
        new = {"type": "object", "properties": {"a": {"type": "string"}},
               "additionalProperties": False}
        report = analyze(old, new)
        assert not report["backward_compatible"]

    def test_removed_property_with_ap_false(self):
        old = {"type": "object", "properties": {"a": {}, "b": {}}}
        new = {"type": "object", "properties": {"a": {}},
               "additionalProperties": False}
        report = analyze(old, new)
        assert not report["backward_compatible"]
        assert any(i["code"] == "property_no_longer_allowed" for i in report["issues"])

    def test_min_properties(self):
        report = analyze({"type": "object", "minProperties": 1},
                         {"type": "object", "minProperties": 3})
        assert not report["backward_compatible"]


class TestNumbers:
    def test_minimum_raised(self):
        report = analyze({"type": "integer"}, {"type": "integer", "minimum": 0})
        assert not report["backward_compatible"]
        assert report["issues"][0]["counterexample"] == -1

    def test_draft4_exclusive_minimum(self):
        old = {"type": "number", "minimum": 0}
        new = {"type": "number", "minimum": 0, "exclusiveMinimum": True}
        report = analyze(old, new)
        assert not report["backward_compatible"]
        assert report["issues"][0]["counterexample"] == 0

    def test_maximum_lowered(self):
        report = analyze({"type": "number", "maximum": 100},
                         {"type": "number", "maximum": 50})
        assert not report["backward_compatible"]


class TestComposition:
    def test_anyof_added(self):
        old = {"type": "object", "properties": {"x": {}}}
        new = {"type": "object", "properties": {"x": {}},
               "anyOf": [{"required": ["x"]}, {"required": ["y"]}]}
        report = analyze(old, new)
        # {} 旧版合法但不满足 anyOf
        assert not report["backward_compatible"]

    def test_not_added(self):
        report = analyze({"type": "string"},
                         {"type": "string", "not": {"enum": ["x"]}})
        assert not report["backward_compatible"]


class TestRef:
    def test_internal_ref_type_change(self):
        old = {"$ref": "#/$defs/id", "$defs": {"id": {"type": "integer"}}}
        new = {"$ref": "#/$defs/id", "$defs": {"id": {"type": "string"}}}
        report = analyze(old, new)
        assert not report["backward_compatible"]
        assert not report["forward_compatible"]


class TestCounterexamples:
    """每个 error 若附了反例，反例必须真实满足旧版、违反新版。"""

    @pytest.mark.parametrize("old,new", [
        ({"type": "object", "properties": {"a": {"type": "string"}}},
         {"type": "object", "properties": {"a": {"type": "string"}},
          "required": ["a"]}),
        ({"type": "string", "enum": ["a", "b"]}, {"type": "string", "enum": ["a"]}),
        ({"type": "array", "items": {"type": "string"}},
         {"type": "array", "items": {"type": "integer"}}),
        ({"type": "integer"}, {"type": "integer", "minimum": 10}),
        ({"type": "object", "properties": {"kind": {"enum": ["a", "b"]}},
          "required": ["kind"],
          "if": {"required": ["kind"], "properties": {"kind": {"const": "a"}}},
          "then": {"required": ["x"]}},
         {"type": "object", "properties": {"kind": {"enum": ["a", "b"]}},
          "required": ["kind"],
          "if": {"required": ["kind"], "properties": {"kind": {"const": "a"}}},
          "then": {"required": ["x", "y"]}}),
    ])
    def test_witness_validity(self, old, new):
        report = analyze(old, new)
        for issue in report["issues"]:
            ce = issue["counterexample"]
            if ce is None:
                continue
            if issue["direction"] == "backward":
                source, target = old, new
            else:
                source, target = new, old
            assert compat.make_validator(source).is_valid(ce), \
                f"反例不满足 source: {ce} / {issue}"
            assert not compat.make_validator(target).is_valid(ce), \
                f"反例未违反 target: {ce} / {issue}"


class TestSamples:
    def test_sample_rejected_by_new(self):
        old = {"type": "object", "properties": {"status": {"type": "string"}}}
        new = {"type": "object", "properties": {"status": {"type": "string"}},
               "required": ["status"]}
        report = analyze(old, new, samples=[{"other": 1}])
        assert report["sample_results"][0]["valid_against_old"]
        assert not report["sample_results"][0]["valid_against_new"]
        assert any(i["code"] == "sample_rejected_by_new" for i in report["issues"])

    def test_sample_valid_both(self):
        schema = {"type": "object", "required": ["a"]}
        report = analyze(schema, schema, samples=[{"a": 1}])
        assert report["sample_results"][0]["valid_against_old"]
        assert report["sample_results"][0]["valid_against_new"]
        assert report["backward_compatible"] and report["forward_compatible"]


class TestConditional:
    """if / then / else 条件分支（draft-07+）。"""

    IF_A = {"required": ["kind"], "properties": {"kind": {"const": "a"}}}
    IF_B = {"required": ["kind"], "properties": {"kind": {"const": "b"}}}

    @staticmethod
    def conditional(if_schema, then=None, otherwise=None):
        schema = {
            "type": "object",
            "properties": {"kind": {"enum": ["a", "b"]}},
            "required": ["kind"],
            "if": if_schema,
        }
        if then is not None:
            schema["then"] = then
        if otherwise is not None:
            schema["else"] = otherwise
        return schema

    @staticmethod
    def issues(report, direction=None, severity=None):
        out = report["issues"]
        if direction is not None:
            out = [i for i in out if i["direction"] == direction]
        if severity is not None:
            out = [i for i in out if i["severity"] == severity]
        return out

    def test_then_tightened_breaks_backward(self):
        old = self.conditional(self.IF_A, then={"required": ["x"]})
        new = self.conditional(self.IF_A, then={"required": ["x", "y"]})
        report = analyze(old, new)
        assert not report["backward_compatible"]
        assert report["forward_compatible"]
        issue = self.issues(report, "backward", "error")[0]
        assert issue["code"] == "required_added"
        assert issue["path"] == "#/then"
        # 反例命中 if（kind=a -> 走 then），满足旧版、被新版拒绝
        ce = issue["counterexample"]
        assert ce["kind"] == "a" and "x" in ce and "y" not in ce
        assert compat.make_validator(old).is_valid(ce)
        assert not compat.make_validator(new).is_valid(ce)

    def test_else_tightened_breaks_backward(self):
        old = self.conditional(self.IF_A, then={"required": ["x"]},
                               otherwise={"required": ["p"]})
        new = self.conditional(self.IF_A, then={"required": ["x"]},
                               otherwise={"required": ["p", "q"]})
        report = analyze(old, new)
        assert not report["backward_compatible"]
        issue = self.issues(report, "backward", "error")[0]
        assert issue["code"] == "required_added"
        assert issue["path"] == "#/else"
        # 反例未命中 if（kind=b -> 走 else）
        assert issue["counterexample"]["kind"] == "b"

    def test_then_relaxed_breaks_forward_only(self):
        old = self.conditional(self.IF_A, then={"required": ["x", "y"]})
        new = self.conditional(self.IF_A, then={"required": ["x"]})
        report = analyze(old, new)
        assert report["backward_compatible"]
        assert not report["forward_compatible"]
        issue = self.issues(report, "forward", "error")[0]
        assert issue["path"] == "#/then"

    def test_identical_conditional_is_compatible(self):
        schema = self.conditional(self.IF_A, then={"required": ["x"]},
                                  otherwise={"required": ["y"]})
        report = analyze(schema, schema)
        assert report["backward_compatible"] and report["forward_compatible"]
        assert report["issues"] == []

    def test_if_condition_rerouting_breaks(self):
        # 仅 if 条件从 kind=a 改为 kind=b：两侧路由都出现破坏
        old = self.conditional(self.IF_A, then={"required": ["x"]},
                               otherwise={"required": ["y"]})
        new = self.conditional(self.IF_B, then={"required": ["x"]},
                               otherwise={"required": ["y"]})
        report = analyze(old, new)
        assert not report["backward_compatible"]
        assert not report["forward_compatible"]
        backward = self.issues(report, "backward", "error")
        # 命中旧条件（走 then）的实例被新版路由到 else 拒绝，反之亦然
        assert {i["path"] for i in backward} == {"#/then", "#/else"}
        for issue in self.issues(report, severity="error"):
            ce = issue["counterexample"]
            source, target = (old, new) if issue["direction"] == "backward" else (new, old)
            assert compat.make_validator(source).is_valid(ce)
            assert not compat.make_validator(target).is_valid(ce)

    def test_if_condition_changed_without_branch_change_warns(self):
        # 分支内容相同、仅 if 表达式变化：无法构造验证反例 -> warning，不判破坏
        branch = {"properties": {"s": {"type": "string"}}}
        old = self.conditional(self.IF_A, then=branch, otherwise=branch)
        new = self.conditional(self.IF_B, then=branch, otherwise=branch)
        report = analyze(old, new)
        assert report["backward_compatible"] and report["forward_compatible"]
        warnings = self.issues(report, "backward", "warning")
        assert len(warnings) == 1
        assert warnings[0]["code"] == "if_condition_changed"
        assert warnings[0]["path"] == "#/if"
        assert warnings[0]["counterexample"] is None

    def test_conditional_added_breaks_backward(self):
        old = {"type": "object",
               "properties": {"kind": {"enum": ["a", "b"]}, "x": {}},
               "required": ["kind"]}
        new = self.conditional(self.IF_A, then={"required": ["x"]})
        new["properties"]["x"] = {}
        report = analyze(old, new)
        assert not report["backward_compatible"]
        assert report["forward_compatible"]
        issue = self.issues(report, "backward", "error")[0]
        assert issue["code"] == "required_added"
        assert issue["path"] == "#/then"
        assert issue["counterexample"]["kind"] == "a"

    def test_conditional_removed_breaks_forward_only(self):
        old = self.conditional(self.IF_A, then={"required": ["x"]})
        new = {"type": "object",
               "properties": {"kind": {"enum": ["a", "b"]}, "x": {}},
               "required": ["kind"]}
        report = analyze(old, new)
        # 删除条件约束：向后兼容（新版更宽松），向前破坏
        assert report["backward_compatible"]
        assert not report["forward_compatible"]
        # 不向後方向不应出现伪报
        assert self.issues(report, "backward") == []
        issue = self.issues(report, "forward", "error")[0]
        assert issue["path"] == "#/then"

    def test_nested_conditional_inside_properties(self):
        old = {"type": "object",
               "properties": {"cfg": self.conditional(self.IF_A, then={"required": ["x"]})}}
        new = {"type": "object",
               "properties": {"cfg": self.conditional(self.IF_A, then={"required": ["x", "y"]})}}
        report = analyze(old, new)
        assert not report["backward_compatible"]
        issue = self.issues(report, "backward", "error")[0]
        assert issue["path"] == "#/properties/cfg/then"
        assert issue["counterexample"]["cfg"]["kind"] == "a"

    def test_nested_conditional_inside_then(self):
        inner_if = {"required": ["sub"], "properties": {"sub": {"const": 1}}}
        old = self.conditional(self.IF_A, then={"if": inner_if, "then": {"required": ["d"]}})
        new = self.conditional(self.IF_A,
                               then={"if": inner_if, "then": {"required": ["d", "e"]}})
        report = analyze(old, new)
        assert not report["backward_compatible"]
        issue = self.issues(report, "backward", "error")[0]
        assert issue["path"] == "#/then/then"
        ce = issue["counterexample"]
        assert ce["kind"] == "a" and ce["sub"] == 1

    def test_unreachable_branch_is_not_breaking(self):
        # if 永假（const 5 不是 string）-> then 永不激活，差异只产生 warning
        old = {"if": {"type": "string", "const": 5}, "then": {"const": "x"}}
        new = {"if": {"type": "string", "const": 5}, "then": {"const": "y"}}
        report = analyze(old, new)
        assert report["backward_compatible"] and report["forward_compatible"]
        assert self.issues(report, severity="error") == []
        warnings = self.issues(report, "backward", "warning")
        assert any(i["path"] == "#/then" for i in warnings)

    def test_bare_if_without_branches_ignored(self):
        old = {"type": "string", "if": {"minLength": 1}}
        new = {"type": "string", "if": {"minLength": 2}}
        report = analyze(old, new)
        assert report["backward_compatible"] and report["forward_compatible"]

    def test_branch_content_type_change(self):
        old = {"if": {"type": "string"}, "then": {"minLength": 1}}
        new = {"if": {"type": "string"}, "then": {"minLength": 3}}
        report = analyze(old, new)
        assert not report["backward_compatible"]
        issue = self.issues(report, "backward", "error")[0]
        assert issue["code"] == "min_length_increased"
        assert issue["path"] == "#/then"
        assert issue["counterexample"] == "aa"

    def test_conditional_via_ref(self):
        old = {"$defs": {"c": {"if": {"type": "string"}, "then": {"const": "a"}}},
               "$ref": "#/$defs/c"}
        new = {"$defs": {"c": {"if": {"type": "string"}, "then": {"const": "b"}}},
               "$ref": "#/$defs/c"}
        report = analyze(old, new)
        assert not report["backward_compatible"]
        assert self.issues(report, "backward", "error")[0]["path"] == "#/then"

    def test_heuristic_inside_branch_stays_warning(self):
        # 分支内的启发式问题（pattern 变化）保持 warning
        old = self.conditional(
            self.IF_A, then={"properties": {"s": {"type": "string", "pattern": "^a"}}})
        new = self.conditional(
            self.IF_A, then={"properties": {"s": {"type": "string", "pattern": "^b"}}})
        report = analyze(old, new)
        assert report["backward_compatible"]
        warnings = self.issues(report, "backward", "warning")
        assert any(i["code"] == "pattern_changed" and i["path"] == "#/then/properties/s"
                   for i in warnings)

    def test_conditional_in_array_items(self):
        old = {"type": "array",
               "items": {"if": {"type": "string"}, "then": {"const": "a"}}}
        new = {"type": "array",
               "items": {"if": {"type": "string"}, "then": {"const": "b"}}}
        report = analyze(old, new)
        assert not report["backward_compatible"]
        issue = self.issues(report, "backward", "error")[0]
        assert issue["path"] == "#/items/then"
        assert issue["counterexample"] == ["a"]
