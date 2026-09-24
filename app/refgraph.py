"""单份 JSON Schema 的 ``$ref`` 引用依赖诊断（纯只读，不访问网络 / 外部文件）。

输入一份 JSON Schema，遍历文档内部全部 ``$ref``，仅接受**片段形式的本地
引用**（``#`` 与 ``#/...`` JSON Pointer），按 RFC 6901 在本文档内解析
目标节点，输出：

* ``edges``：可解析的引用边（源节点指针 -> 规范化目标指针）；
* ``invalid_refs``：不可用引用的精确定位与原因（非本地引用 / 名称片段 /
  指针目标不存在 / 非法指针转义 / ``$ref`` 非字符串）；
* ``unreachable_defs``：从根侧（``$defs`` 之外）沿引用链不可达的
  ``$defs`` 节点（含定义内部嵌套 ``$defs`` 中的死定义）；
* ``cycles``：引用图中的循环链（含自环），以首尾相接的指针链表示。

图模型
======
图节点是各层 ``$defs`` 容器中的命名定义（顶层 ``$defs`` 的直接子节点，
以及定义内部嵌套 ``$defs`` 的子节点，嵌套定义与包裹它的顶层定义是
**不同节点**）。一条引用边按其源位置归属：源在某节点子树内（定义自身
或其 properties / items / 组合分支等）即为该节点发出的边；源在所有
$defs 节点之外（根侧结构）时，其精确指向的定义节点是可达性 BFS 的种子。

* **可达性**：从种子出发沿已到达节点内部发出的**全部**引用边传播，
  未被任何根侧引用链指向的 $defs 节点即"死定义"；
* **循环**：每个节点取源路径最浅的出边作为代表边（定义对象自身的
  ``$ref`` 优先于子树内引用），沿代表边追踪至多落入一个环，按
  "环上最小指针旋转到链首"规范化去重；根侧节点引用文档根（``#``）
  形成的自环单独报告。

其余约定：``enum`` / ``const`` / ``default`` / ``examples`` 中出现的
``$ref`` 是实例数据而非 schema 引用，遍历时不下钻；整个过程只在入参
文档内存结构内解析，绝不发起网络或文件系统访问。
"""
from __future__ import annotations

from collections import deque
from typing import Any
from urllib.parse import unquote

# 实例数据关键字：其内部的 $ref 是数据，不是 schema 引用
_INSTANCE_KEYWORDS = frozenset({"enum", "const", "default", "examples"})

# 不可用引用的原因代码
REASON_EXTERNAL = "external_reference"
REASON_NAME_FRAGMENT = "name_fragment_unsupported"
REASON_TARGET_NOT_FOUND = "target_not_found"
REASON_MALFORMED_POINTER = "malformed_pointer"
REASON_NON_STRING = "non_string_ref"


# ---------------------------------------------------------------------------
# JSON Pointer 工具
# ---------------------------------------------------------------------------


def _escape_token(token: str) -> str:
    """JSON Pointer 段编码：``~`` -> ``~0``、``/`` -> ``~1``。"""
    return token.replace("~", "~0").replace("/", "~1")


def _build_pointer(tokens: list[str]) -> str:
    """由解码后的路径段构造规范化 JSON Pointer 字符串（根为 ``#``）。"""
    if not tokens:
        return "#"
    return "#/" + "/".join(_escape_token(token) for token in tokens)


def _pointer_segments(pointer: str) -> list[str]:
    """拆分已规范化的指针字符串为解码后的路径段（根 ``#`` 为空列表）。"""
    if pointer == "#":
        return []
    return [
        seg.replace("~1", "/").replace("~0", "~")
        for seg in pointer[2:].split("/")
    ]


def _decode_pointer_fragment(fragment: str) -> list[str] | None:
    """解码 URI fragment 中的 JSON Pointer（fragment 不含前导 ``#/``）。

    先按 ``/`` 切段（``%2F`` 是键名中的字面斜杠，不做分隔符），再逐段
    百分号解码并还原 ``~1`` / ``~0`` 转义。非法 ``~`` 转义返回 ``None``。
    """
    tokens: list[str] = []
    for raw in fragment.split("/"):
        token = unquote(raw)
        decoded = ""
        i = 0
        while i < len(token):
            ch = token[i]
            if ch == "~":
                if i + 1 >= len(token) or token[i + 1] not in ("0", "1"):
                    return None
                decoded += "~" if token[i + 1] == "0" else "/"
                i += 2
            else:
                decoded += ch
                i += 1
        tokens.append(decoded)
    return tokens


def _resolve_pointer(root: Any, tokens: list[str]) -> tuple[bool, str]:
    """按 JSON Pointer 在文档内解析目标。

    返回 ``(是否成功, 失败段)``：成功时失败段为空；失败时给出首个无法
    解析的路径段（已解码原文）。数组下标遵循 RFC 6901（``0`` 合法、
    带前导零非法、``-`` 指向不存在的末位后成员）。
    """
    node = root
    for token in tokens:
        if isinstance(node, dict):
            if token in node:
                node = node[token]
            else:
                return False, token
        elif isinstance(node, list):
            if token == "-" or not token.isdigit() or (
                len(token) > 1 and token.startswith("0")
            ):
                return False, token
            index = int(token)
            if index >= len(node):
                return False, token
            node = node[index]
        else:
            return False, token
    return True, ""


def _classify_ref(root: Any, ref: Any) -> tuple[str | None, dict | None]:
    """分类单个 ``$ref`` 取值，返回 ``(target_pointer, invalid_entry)``。

    二者恰有一个非 None：可解析的本地 Pointer 引用返回目标指针；其余
    情况返回不含 source 的不可用条目（由调用方补上 source）。
    """
    if not isinstance(ref, str):
        return None, {
            "ref": None,
            "reason": REASON_NON_STRING,
            "message": f"$ref 必须是字符串，实际类型为 {type(ref).__name__}",
        }

    before, separator, fragment = ref.partition("#")
    if before or not separator:
        # 非片段形式（http(s)://、相对文件路径、裸名称等）：一律不访问
        return None, {
            "ref": ref,
            "reason": REASON_EXTERNAL,
            "message": "仅接受片段形式的本地引用（# 或 #/...），不访问网络或外部文件",
        }

    if fragment == "":
        # "#"：指向文档根
        return "#", None

    if not fragment.startswith("/"):
        # "#anchor" 之类的名称片段（draft 2019-09 锚点）：不做解析
        return None, {
            "ref": ref,
            "reason": REASON_NAME_FRAGMENT,
            "message": "仅支持 JSON Pointer 片段（#/...），不支持锚点名称片段",
        }

    tokens = _decode_pointer_fragment(fragment[1:])
    if tokens is None:
        return None, {
            "ref": ref,
            "reason": REASON_MALFORMED_POINTER,
            "message": "非法 JSON Pointer：~ 后只能为 0 或 1",
        }

    ok, failed_segment = _resolve_pointer(root, tokens)
    if not ok:
        return None, {
            "ref": ref,
            "reason": REASON_TARGET_NOT_FOUND,
            "message": (
                f"引用目标 {_build_pointer(tokens)} 在路径段 "
                f"{_escape_token(failed_segment)!r} 处不存在"
            ),
        }

    return _build_pointer(tokens), None


# ---------------------------------------------------------------------------
# Schema 树遍历：收集 $defs 节点、引用边与不可用引用
# ---------------------------------------------------------------------------


def _walk_defs(node: Any, pointer: str, def_nodes: list[dict]) -> None:
    """收集全部 ``$defs`` 容器中的命名定义节点。

    记录 ``pointer`` / ``name`` / ``top_level``（是否为文档根
    ``#/$defs`` 的直接子节点；定义内部嵌套 $defs 的子节点为 False）。
    """
    if isinstance(node, dict):
        defs = node.get("$defs")
        if isinstance(defs, dict):
            defs_pointer = "#/$defs" if pointer == "#" else f"{pointer}/$defs"
            top_level = pointer == "#"
            for name, child in defs.items():
                child_pointer = f"{defs_pointer}/{_escape_token(name)}"
                def_nodes.append(
                    {"pointer": child_pointer, "name": name, "top_level": top_level}
                )
                _walk_defs(child, child_pointer, def_nodes)
        for key, value in node.items():
            if key == "$defs" and isinstance(value, dict):
                continue
            child_pointer = (
                f"#/{_escape_token(key)}"
                if pointer == "#"
                else f"{pointer}/{_escape_token(key)}"
            )
            _walk_defs(value, child_pointer, def_nodes)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk_defs(item, f"{pointer}/{index}", def_nodes)


def _walk(
    node: Any,
    pointer: str,
    root: Any,
    edges: list[dict],
    invalid_refs: list[dict],
) -> None:
    """递归遍历 schema 树，收集引用边与不可用引用（实例数据关键字跳过）。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _INSTANCE_KEYWORDS:
                # enum / const / default / examples 内的 $ref 是实例数据
                continue
            child_pointer = (
                f"#/{_escape_token(key)}"
                if pointer == "#"
                else f"{pointer}/{_escape_token(key)}"
            )
            if key == "$ref":
                target, invalid = _classify_ref(root, value)
                if invalid is not None:
                    invalid_refs.append({"source": pointer, **invalid})
                else:
                    edges.append({"source": pointer, "ref": value, "target": target})
                # $ref 的取值本身（字符串）不是 schema 节点，不再下钻
                continue
            _walk(value, child_pointer, root, edges, invalid_refs)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk(item, f"{pointer}/{index}", root, edges, invalid_refs)


# ---------------------------------------------------------------------------
# $defs 节点粒度的引用图：可达性与循环
# ---------------------------------------------------------------------------


def _nearest_def(segments: list[str], def_pointers: set[str]) -> str | None:
    """路径段所属的最近 ``$defs`` 节点指针；不在任何定义子树返回 None。"""
    for end in range(len(segments), 0, -1):
        prefix = _build_pointer(segments[:end])
        if prefix in def_pointers:
            return prefix
    return None


def _def_graph(
    edges: list[dict], def_pointers: set[str]
) -> tuple[dict[str, str], dict[str, int], list[str]]:
    """把引用边折叠到 ``$defs`` 节点粒度（含顶层与嵌套定义）的有向图。

    * 源在某 $defs 节点子树内的边 -> 该节点的出边；一个节点有多条出边
      时记录源路径最浅者（定义对象**自身**的 ``$ref``）为代表边，供
      循环检测使用（可达性在调用方使用全部出边 BFS）；
    * 源在所有 $defs 节点之外（根侧）且目标**恰好**是某 $defs 节点
      -> BFS 种子；
    * 顶层定义与其内嵌 ``$defs`` 子节点是不同的图节点。

    返回 ``(节点代表出边, 代表边的源相对深度, 根侧种子节点列表)``。
    """
    def_out: dict[str, str] = {}
    source_depth: dict[str, int] = {}
    seeds: list[str] = []
    for edge in edges:
        source_segments = _pointer_segments(edge["source"])
        source_def = _nearest_def(source_segments, def_pointers)
        target_def = edge["target"] if edge["target"] in def_pointers else None
        if source_def is None:
            if target_def is not None:
                seeds.append(target_def)
            continue
        if target_def is None:
            continue
        depth = len(source_segments) - len(_pointer_segments(source_def))
        if source_def not in source_depth or depth < source_depth[source_def]:
            source_depth[source_def] = depth
            def_out[source_def] = target_def
    return def_out, source_depth, seeds


def _reachable_defs(edges: list[dict], def_pointers: set[str]) -> set[str]:
    """在 $defs 节点粒度上做 BFS，返回从根侧可达的节点指针集合。

    种子 = 根侧引用边精确指向的 $defs 节点；传播时沿**已到达节点内部
    发出的全部引用边**展开——定义内部属性 / 分支引用到的其它定义
    （包括嵌套 $defs 节点）同样可达。
    """
    _, _, seeds = _def_graph(edges, def_pointers)
    reached: set[str] = set()
    queue: deque[str] = deque()
    for seed in seeds:
        if seed not in reached:
            reached.add(seed)
            queue.append(seed)

    while queue:
        owner = queue.popleft()
        for edge in edges:
            source = edge["source"]
            if source != owner and not source.startswith(owner + "/"):
                continue
            target = edge["target"]
            if target in def_pointers and target not in reached:
                reached.add(target)
                queue.append(target)
    return reached


def _find_cycles(edges: list[dict], def_pointers: set[str]) -> list[dict]:
    """找出全部引用循环链（确定性、去重）。

    循环分两类：

    * **定义节点之间的环**：沿每个节点的代表出边（自身 ``$ref`` 优先）
      追踪，自环（树节点内部引用自身定义）与多节点互引均检出；
    * **根侧自指**：$defs 之外的节点引用文档根 ``#`` 形成的自环。

    每个节点至多一条代表出边，沿出边追踪至多落入一个环；以"环上最小
    指针旋转到链首"规范化，使同一环只报一次且与遍历起点无关。
    """
    def_out, _, _ = _def_graph(edges, def_pointers)
    root_self_sources = sorted(
        edge["source"]
        for edge in edges
        if _nearest_def(_pointer_segments(edge["source"]), def_pointers) is None
        and edge["target"] == "#"
    )

    cycles: dict[tuple[str, ...], list[str]] = {}

    for start in sorted(def_out):
        order: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while current in def_out and current not in positions:
            positions[current] = len(order)
            order.append(current)
            current = def_out[current]
        if current in positions:
            cycle = order[positions[current]:]
            pivot = min(range(len(cycle)), key=cycle.__getitem__)
            canonical = tuple(cycle[pivot:] + cycle[:pivot])
            cycles.setdefault(canonical, list(canonical) + [canonical[0]])

    for source in root_self_sources:
        chain = [source, "#"]
        cycles.setdefault(tuple(chain), chain)

    return [{"chain": chain} for chain in sorted(cycles.values())]


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def analyze_references(schema: dict[str, Any]) -> dict:
    """诊断单份 JSON Schema 的内部 ``$ref`` 依赖（纯函数，不修改入参）。"""
    edges: list[dict] = []
    invalid_refs: list[dict] = []
    def_nodes: list[dict] = []

    _walk_defs(schema, "#", def_nodes)
    _walk(schema, "#", schema, edges, invalid_refs)

    edges.sort(key=lambda edge: (edge["source"], edge["target"]))
    invalid_refs.sort(key=lambda item: (item["source"], str(item.get("ref") or "")))

    def_pointers = {node["pointer"] for node in def_nodes}
    reached = _reachable_defs(edges, def_pointers)

    unreachable: list[dict] = sorted(
        (
            {"pointer": node["pointer"], "name": node["name"]}
            for node in def_nodes
            if node["pointer"] not in reached
        ),
        key=lambda node: node["pointer"],
    )

    cycles = _find_cycles(edges, def_pointers)

    return {
        "counts": {
            "refs": len(edges) + len(invalid_refs),
            "edges": len(edges),
            "invalid_refs": len(invalid_refs),
            "unreachable_defs": len(unreachable),
            "cycles": len(cycles),
        },
        "edges": edges,
        "invalid_refs": invalid_refs,
        "unreachable_defs": unreachable,
        "cycles": cycles,
    }
