"""分析报告的 SARIF 2.1.0 导出。

将一份已落库的兼容性分析报告转换为 SARIF v2.1.0（Static Analysis
Results Interchange Format）文档，便于对接 GitHub code scanning 等
消费 SARIF 的工具链。

映射约定
========
* 每条 issue -> 一个 ``result``；即使完全重复的 issue 也分别保留，不做合并；
* ``rule`` 按 issue 的 ``code`` 去重生成（同一 code 仅一条 rule，按 ruleId
  字典序排列），``defaultConfiguration.level`` 取该 code 下出现过的最高
  严重级别（error > warning）；
* issue 的 ``severity`` 映射为 result 的 ``level``
  （``error`` -> ``error``，``warning`` -> ``warning``）；
* issue 的 ``direction``（backward / forward，兼容方向不属于 SARIF 标准
  字段）放入 result 的 ``properties.direction``；
* issue 的 schema ``path``（JSON Pointer 风格，如 ``#/properties/a``）作为
  逻辑位置：去重后登记到 ``run.logicalLocations``，result 通过
  ``locations.logicalLocations`` 的 ``index`` 引用；
* ``partialFingerprints.businessIdentity/v1`` 复用基线对比的稳定业务指纹
  （``app.compare.issue_fingerprint``：direction + path + code + 规范化
  reason），**不包含 message**，同一业务问题仅措辞变化时指纹保持不变。

``results`` 按 (direction, ruleId, path, level, 指纹) 稳定排序；键完全相同
的重复 issue 保持报告中的相对顺序（稳定排序），因此重复项仍分别保留。
"""
from __future__ import annotations

from typing import Any

from app.compare import issue_fingerprint

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA_URI = "https://json.schemastore.org/sarif-2.1.0.json"

TOOL_NAME = "json-schema-compat-analyzer"
TOOL_VERSION = "1.0.0"
TOOL_INFORMATION_URI = "https://github.com/features/code-scanning"

# partialFingerprints 的键：以 /v1 结尾，便于未来指纹算法演进时版本化。
_BUSINESS_FINGERPRINT_KEY = "businessIdentity/v1"

# issue severity -> SARIF result level
_LEVEL_BY_SEVERITY = {"error": "error", "warning": "warning"}
_DEFAULT_LEVEL = "warning"

# 规则严重级别排序：聚合同一 code 时取最高级别。
_SEVERITY_RANK = {"warning": 0, "error": 1}

# 各 issue code 的人类可读说明（rule.shortDescription）；
# 未列入的 code 退化为用 code 本身作为说明，导出不会因此失败。
_RULE_DESCRIPTIONS: dict[str, str] = {
    "type_removed": "类型被移除",
    "type_restricted": "允许的类型被收窄",
    "required_added": "字段在新版中变为必填",
    "enum_value_removed": "枚举值在新版中被删除",
    "enum_added": "新版新增枚举约束",
    "const_added": "新版新增常量约束",
    "const_changed": "常量值发生变化",
    "property_no_longer_allowed": "属性被删除且新版不允许额外字段",
    "additional_properties_forbidden": "additionalProperties 收紧为 false",
    "min_properties_increased": "minProperties 下限提高",
    "max_properties_decreased": "maxProperties 上限降低",
    "min_items_increased": "minItems 下限提高",
    "max_items_decreased": "maxItems 上限降低",
    "unique_items_added": "新版新增元素唯一性要求",
    "minimum_increased": "minimum 下限提高",
    "maximum_decreased": "maximum 上限降低",
    "integer_required": "新版新增整数类型要求",
    "multiple_of_added": "新版新增 multipleOf 整除要求",
    "min_length_increased": "minLength 下限提高",
    "max_length_decreased": "maxLength 上限降低",
    "pattern_changed": "字符串正则约束发生变化",
    "anyOf_branch_missing": "anyOf 分支在新版中缺失",
    "oneOf_branch_missing": "oneOf 分支在新版中缺失",
    "anyOf_added": "新版新增 anyOf 组合约束",
    "oneOf_added": "新版新增 oneOf 组合约束",
    "anyOf_branch_rejected": "原有实例不再满足新版 anyOf 的任一分支",
    "oneOf_branch_rejected": "原有实例不再满足新版 oneOf 的任一分支",
    "not_added": "新版新增 not 否定约束",
    "if_condition_changed": "if 条件表达式变化，实例可能被路由到不同分支",
    "schema_forbids_all": "目标位置 schema 为 false，拒绝所有实例",
    "sample_rejected_by_new": "提供的样例文档被新版 schema 拒绝",
    "sample_rejected_by_old": "提供的样例文档被旧版 schema 拒绝",
}


def _result_level(issue: dict) -> str:
    return _LEVEL_BY_SEVERITY.get(issue.get("severity"), _DEFAULT_LEVEL)


def _unescape_pointer_segment(segment: str) -> str:
    """解码 JSON Pointer 段内的 ``~1`` / ``~0`` 转义。"""
    return segment.replace("~1", "/").replace("~0", "~")


def _logical_location_name(path: str) -> str:
    """逻辑位置的短名：取 JSON Pointer 最后一段（根路径为 ``#``）。"""
    if path in ("", "#"):
        return "#"
    return _unescape_pointer_segment(path.rsplit("/", 1)[-1])


def _build_rules(issues: list[dict]) -> list[dict]:
    """按 code 去重生成 rule 列表（按 ruleId 字典序）。"""
    levels: dict[str, str] = {}
    for issue in issues:
        code = str(issue.get("code") or "")
        level = _result_level(issue)
        if _SEVERITY_RANK.get(level, 0) > _SEVERITY_RANK.get(levels.get(code), -1):
            levels[code] = level

    rules: list[dict] = []
    for code in sorted(levels):
        rule: dict[str, Any] = {
            "id": code,
            "defaultConfiguration": {"level": levels[code]},
        }
        description = _RULE_DESCRIPTIONS.get(code)
        if description:
            # name 要求为标识符风格字符；code 本身即下划线标识符，可直接复用。
            rule["name"] = code
            rule["shortDescription"] = {"text": description}
        rules.append(rule)
    return rules


def _build_logical_locations(issues: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """去重收集 schema path 逻辑位置，返回 (位置表, path -> index)。"""
    indices: dict[str, int] = {}
    for issue in issues:
        path = str(issue.get("path") or "#")
        if path not in indices:
            indices[path] = len(indices)
    locations = [
        {
            "name": _logical_location_name(path),
            "fullyQualifiedName": path,
        }
        for path in sorted(indices)
    ]
    # 排序后下标发生变化，重建 path -> index 映射。
    sorted_indices = {loc["fullyQualifiedName"]: i for i, loc in enumerate(locations)}
    return locations, sorted_indices


def _build_result(issue: dict, logical_index: dict[str, int]) -> dict:
    """将单条 issue 转为 SARIF result（重复 issue 不做合并）。"""
    path = str(issue.get("path") or "#")
    message = issue.get("message")
    result: dict[str, Any] = {
        "ruleId": str(issue.get("code") or ""),
        "level": _result_level(issue),
        "message": {"text": str(message if message is not None else "")},
        "locations": [
            {
                "logicalLocations": [
                    {
                        "fullyQualifiedName": path,
                        "index": logical_index[path],
                    }
                ]
            }
        ],
        # 稳定业务指纹：direction + path + code + 规范化 reason，
        # 不含 severity 与 message —— 同一业务问题仅措辞 / 级别变化时不变。
        "partialFingerprints": {
            _BUSINESS_FINGERPRINT_KEY: issue_fingerprint(issue),
        },
        "properties": {
            "direction": str(issue.get("direction") or ""),
        },
    }
    return result


def _result_sort_key(result: dict) -> tuple:
    location = result["locations"][0]["logicalLocations"][0]
    fingerprint = result["partialFingerprints"][_BUSINESS_FINGERPRINT_KEY]
    return (
        result["properties"]["direction"],
        result["ruleId"],
        location["fullyQualifiedName"],
        result["level"],
        fingerprint,
    )


def build_sarif(analysis: dict) -> dict:
    """将完整分析记录（``Analysis.to_dict()``）转换为 SARIF 2.1.0 文档。

    纯函数：不读取数据库、不修改入参。
    """
    report = analysis.get("report", {}) or {}
    issues = list(report.get("issues", []) or [])

    rules = _build_rules(issues)
    logical_locations, logical_index = _build_logical_locations(issues)

    # 稳定排序：排序键完全相同的重复 issue 保持报告内相对顺序，
    # 因此重复 issue 仍分别保留为独立 result。
    results = [_build_result(issue, logical_index) for issue in issues]
    results.sort(key=_result_sort_key)

    return {
        "$schema": SARIF_SCHEMA_URI,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": TOOL_NAME,
                        "version": TOOL_VERSION,
                        "semanticVersion": TOOL_VERSION,
                        "informationUri": TOOL_INFORMATION_URI,
                        "rules": rules,
                    }
                },
                "logicalLocations": logical_locations,
                "results": results,
            }
        ],
    }
