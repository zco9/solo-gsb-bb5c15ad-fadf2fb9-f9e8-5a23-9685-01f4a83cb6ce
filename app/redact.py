"""基于新 Schema 的 ``x-sensitive`` 实例脱敏引擎（纯只读）。

读取分析记录保存的**新 schema**，对提交的每篇 JSON 文档做 schema 与
实例的并行遍历：仅当实例当前位置对应的 schema 节点显式声明
``"x-sensitive": true`` 时，把该实例值标记为脱敏，最终在文档的深拷贝
上统一替换为固定掩码 ``"***"``。

* **只改提交文档的深拷贝**：不修改原文入参，也不改动库中保存的样本；
* **遍历对象属性与数组元素**：``properties`` / ``patternProperties`` /
  ``additionalProperties``、``prefixItems`` / 元组写法 ``items`` /
  ``additionalItems`` / 数组写法 ``items`` 均按实例实际结构下钻；
* **解析文档内 ``#/...`` $ref**：只接受片段形式的本地引用
  （``#`` 与 ``#/...`` JSON Pointer，RFC 6901），与引用依赖诊断一致，
  绝不访问网络或外部文件；
* **组合关键字**：``oneOf`` / ``anyOf`` 只遍历对**当前实例（原值）**
  校验通过的分支，``allOf`` 遍历全部分支；
* **递归引用停止重复展开**：同一实例位置上，一个引用目标只展开一次
  （``#`` 自指、定义互引、链表式自引用均安全终止）；进入子实例位置后
  引用展开计数重新开始；
* 无法解析的引用（外部引用 / 名称片段 / 目标不存在 / 非法指针转义 /
  非字符串）抛出 :class:`UnresolvableReference`，由接口层转为带 schema
  指针的 ``422``。

实现分两步：先在**原始文档**上做一次只收集位置的遍历（组合分支判定
因此基于未脱敏的当前实例），再把全部命中指针按由深到浅的顺序施加到
深拷贝上，避免祖先节点整体掩码后无法定位更深的命中点。
"""
from __future__ import annotations

import copy
import re
from typing import Any

from app.compat import escape_pointer, make_validator
from app.refgraph import (
    _classify_ref,
    _pointer_segments,
)

# 脱敏掩码：所有 x-sensitive 命中位置统一替换为该字符串。
REDACTION_MASK = "***"

# 单次脱敏允许的最大文档数（与 schemas.RedactRequest 保持一致）。
MAX_REDACT_DOCUMENTS = 5000


class UnresolvableReference(Exception):
    """遍历时遇到无法在本文档内解析的 ``$ref``。

    ``schema_pointer`` 指向声明该 ``$ref`` 的 schema 节点位置，
    ``instance_pointer`` 指向正在遍历的实例位置。
    """

    def __init__(self, schema_pointer: str, ref: str | None,
                 reason: str, message: str, instance_pointer: str = "#"):
        self.schema_pointer = schema_pointer
        self.ref = ref
        self.reason = reason
        self.message = message
        self.instance_pointer = instance_pointer
        super().__init__(
            f"无法解析的 $ref（schema {schema_pointer}，实例 {instance_pointer}）: {message}"
        )


# ---------------------------------------------------------------------------
# 指针工具
# ---------------------------------------------------------------------------


def _child_pointer(pointer: str, token: Any) -> str:
    """在 schema / 实例指针后追加一段（对象键或数组下标）。"""
    segment = escape_pointer(str(token))
    return f"#/{segment}" if pointer == "#" else f"{pointer}/{segment}"


def _node_at_pointer(root: Any, pointer: str) -> Any:
    """按规范化后的本地指针取节点（``#`` 为根）。"""
    node = root
    for segment in _pointer_segments(pointer):
        node = node[segment] if isinstance(node, dict) else node[int(segment)]
    return node


def _sort_key(pointer: str) -> tuple:
    """实例指针的确定性排序键：按路径深度，数组下标段按数值比较。"""
    key: tuple = ()
    for token in _pointer_segments(pointer):
        if token.isdigit() and not (len(token) > 1 and token.startswith("0")):
            # (类型标志, 数值, 键名)：数字段统一三元组，避免跨段比较类型冲突
            key += (0, int(token), "")
        else:
            key += (1, 0, token)
    return key


def _apply_masks(document: Any, pointers: list[str]) -> Any:
    """把实例指针处的值替换为掩码（由深到浅，作用于深拷贝）。"""
    if "#" in pointers:
        # 根节点被标记：整篇文档替换为掩码，更深的命中点不再存在
        return REDACTION_MASK
    result = copy.deepcopy(document)
    for pointer in sorted(pointers, key=lambda p: len(_pointer_segments(p)),
                          reverse=True):
        tokens = _pointer_segments(pointer)
        parent: Any = result
        for token in tokens[:-1]:
            parent = parent[token] if isinstance(parent, dict) else parent[int(token)]
        last = tokens[-1]
        if isinstance(parent, dict):
            if last in parent:
                parent[last] = REDACTION_MASK
        else:
            index = int(last)
            if 0 <= index < len(parent):
                parent[index] = REDACTION_MASK
    return result


# ---------------------------------------------------------------------------
# 遍历（只收集命中位置，不改动实例）
# ---------------------------------------------------------------------------


class _RedactionWalker:
    """单篇文档的脱敏位置收集器（schema 根在全部文档间由调用方复用）。"""

    def __init__(self, root_schema: Any):
        self.root_schema = root_schema
        # 选择校验器时尽量贴合 schema 声明的 draft（oneOf/anyOf 分支判定用）
        self.validator = make_validator(root_schema)
        self._replaced: set[str] = set()
        self.replacements: list[dict] = []

    def _record(self, instance_pointer: str, schema_pointer: str) -> None:
        # 同一实例位置只记一次（如 properties 与 allOf 分支重复命中），
        # 保留首次经过的 schema 指针
        if instance_pointer not in self._replaced:
            self._replaced.add(instance_pointer)
            self.replacements.append(
                {"instance_pointer": instance_pointer,
                 "schema_pointer": schema_pointer}
            )

    def _classify(self, node: dict, schema_pointer: str,
                  instance_pointer: str) -> str:
        """分类节点上的 ``$ref``，返回规范化目标指针；非法则抛 422 异常。"""
        target_pointer, invalid = _classify_ref(
            self.root_schema, node.get("$ref"))
        if invalid is not None:
            raise UnresolvableReference(
                schema_pointer=schema_pointer,
                ref=invalid["ref"],
                reason=invalid["reason"],
                message=invalid["message"],
                instance_pointer=instance_pointer,
            )
        return target_pointer

    def _branch_valid(self, branch: Any, value: Any) -> bool:
        """子 schema 分支是否对当前实例（原值）校验通过。"""
        try:
            branch_validator = self.validator.evolve(schema=branch)
        except Exception:
            # 分支本身不是合法子 schema 时视为不匹配，不阻断其余遍历
            return False
        return next(branch_validator.iter_errors(value), None) is None

    def collect(self, node: Any, value: Any, schema_pointer: str,
                instance_pointer: str, seen: frozenset[str]) -> None:
        """按 schema 节点 ``node`` 遍历原始实例 ``value``，收集脱敏位置。"""
        # 布尔 schema：true 无约束、false 拒绝一切，均无 x-sensitive
        if node is True or node is False or not isinstance(node, dict):
            return

        # x-sensitive 节点：标记当前位置后不再下钻该子树
        if node.get("x-sensitive") is True:
            self._record(instance_pointer, schema_pointer)
            return

        # $ref 解析；同一实例位置的重复递归引用停止再次展开
        if "$ref" in node:
            target_pointer = self._classify(node, schema_pointer,
                                            instance_pointer)
            if target_pointer in seen:
                return
            target = _node_at_pointer(self.root_schema, target_pointer)
            self.collect(target, value, target_pointer,
                         instance_pointer, seen | {target_pointer})
            return

        # 组合分支：oneOf/anyOf 只保留对当前实例校验通过的分支，
        # allOf 遍历全部分支。判定基于尚未脱敏的原始实例值。
        branches: list[tuple[Any, str]] = []
        all_branches = node.get("allOf")
        if isinstance(all_branches, list):
            branches.extend(
                (branch, f"{schema_pointer}/allOf/{i}")
                for i, branch in enumerate(all_branches)
            )
        for keyword in ("oneOf", "anyOf"):
            keyword_branches = node.get(keyword)
            if isinstance(keyword_branches, list):
                branches.extend(
                    (branch, f"{schema_pointer}/{keyword}/{i}")
                    for i, branch in enumerate(keyword_branches)
                    if self._branch_valid(branch, value)
                )

        if isinstance(value, dict):
            self._collect_object(node, value, schema_pointer,
                                 instance_pointer, seen, branches)
        elif isinstance(value, list):
            self._collect_array(node, value, schema_pointer,
                                instance_pointer, seen, branches)
        else:
            # 标量 / null：组合分支可能直接带 x-sensitive
            for branch, branch_pointer in branches:
                self.collect(branch, value, branch_pointer,
                             instance_pointer, seen)

    # ------------------------------------------------------------------
    # 对象 / 数组
    # ------------------------------------------------------------------

    def _collect_object(self, node: dict, value: dict, schema_pointer: str,
                        instance_pointer: str, seen: frozenset[str],
                        branches: list[tuple[Any, str]]) -> None:
        properties = node.get("properties")
        properties = properties if isinstance(properties, dict) else {}

        pattern_props = node.get("patternProperties")
        pattern_props = pattern_props if isinstance(pattern_props, dict) else {}
        compiled_patterns: list[tuple[re.Pattern, Any, str]] = []
        for pattern, sub in pattern_props.items():
            try:
                regex = re.compile(pattern)
            except re.error:
                # 非法正则与 jsonschema 行为对齐：忽略该 patternProperties
                continue
            compiled_patterns.append(
                (regex, sub,
                 f"{schema_pointer}/patternProperties/{escape_pointer(pattern)}")
            )

        additional = node.get("additionalProperties", True)

        # 按实例键的原始顺序遍历；同一键可能同时命中 properties 与多个
        # patternProperties，每个适用子 schema 都基于原值独立收集
        for key, item in value.items():
            item_pointer = _child_pointer(instance_pointer, key)
            sub_schemas: list[tuple[Any, str]] = []
            if key in properties:
                sub_schemas.append(
                    (properties[key],
                     f"{schema_pointer}/properties/{escape_pointer(str(key))}"))
            for regex, sub, sub_pointer in compiled_patterns:
                if regex.search(str(key)):
                    sub_schemas.append((sub, sub_pointer))
            if not sub_schemas and additional is not True:
                # additionalProperties: false 无约束可遍历；对象 schema 时遍历
                sub_schemas.append(
                    (additional, f"{schema_pointer}/additionalProperties"))

            for sub, sub_pointer in sub_schemas:
                self.collect(sub, item, sub_pointer, item_pointer, frozenset())

        # 组合分支可能在 properties 之外补充约束（分支内 properties /
        # 直接 x-sensitive 等）；seen 沿同层组合分支继续传播
        for branch, branch_pointer in branches:
            self.collect(branch, value, branch_pointer,
                         instance_pointer, seen)

    def _collect_array(self, node: dict, value: list, schema_pointer: str,
                       instance_pointer: str, seen: frozenset[str],
                       branches: list[tuple[Any, str]]) -> None:
        prefix_items = node.get("prefixItems")
        legacy_tuple = False
        if not isinstance(prefix_items, list) and isinstance(node.get("items"), list):
            prefix_items = node["items"]  # draft-04/06/07 元组写法
            legacy_tuple = True

        if isinstance(prefix_items, list):
            tuple_keyword = "items" if legacy_tuple else "prefixItems"
            for index, item in enumerate(value):
                item_pointer = _child_pointer(instance_pointer, index)
                if index < len(prefix_items):
                    self.collect(prefix_items[index], item,
                                 f"{schema_pointer}/{tuple_keyword}/{index}",
                                 item_pointer, frozenset())
                elif legacy_tuple:
                    rest = node.get("additionalItems", True)
                    if rest is not True:
                        self.collect(rest, item,
                                     f"{schema_pointer}/additionalItems",
                                     item_pointer, frozenset())
                else:
                    rest = node.get("items", True)
                    if rest is not True:
                        self.collect(rest, item, f"{schema_pointer}/items",
                                     item_pointer, frozenset())
        else:
            items = node.get("items")
            if isinstance(items, dict):
                for index, item in enumerate(value):
                    self.collect(items, item, f"{schema_pointer}/items",
                                 _child_pointer(instance_pointer, index),
                                 frozenset())

        for branch, branch_pointer in branches:
            self.collect(branch, value, branch_pointer,
                         instance_pointer, seen)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def redact_documents(schema: Any, documents: list[Any]) -> dict:
    """按 schema 的 ``x-sensitive`` 标记脱敏一批文档（纯函数）。

    ``documents`` 由调用方保证为 1..MAX_REDACT_DOCUMENTS 个任意 JSON 值。
    不修改入参文档与 schema；无法解析的引用抛出
    :class:`UnresolvableReference`。
    """
    results: list[dict] = []
    for index, document in enumerate(documents):
        walker = _RedactionWalker(schema)
        walker.collect(schema, document, "#", "#", frozenset())
        # walker 已按实例位置去重；这里仅按实例指针做与遍历路径无关的
        # 确定性排序（数组下标段按数值比较）
        replacements = sorted(walker.replacements,
                              key=lambda item: _sort_key(item["instance_pointer"]))
        redacted = _apply_masks(
            document, [r["instance_pointer"] for r in replacements])
        results.append({
            "index": index,
            "redacted": redacted,
            "replacements": replacements,
        })
    return {"results": results}
