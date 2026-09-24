"""兼容性反例缩减引擎（counterexample reduction）。

对**单份**已确认属于某一兼容方向破坏类别的 JSON 文档，在保持其破坏
方向不变的前提下，按固定顺序做三类删除式缩减：

1. **删除对象属性**（``delete_property``）：深度优先、根节点自身属性
   优先，对象键按字典序，逐层深入嵌套结构；
2. **删除数组元素**（``delete_element``）：按下标从小到大，同样深入
   嵌套在对象 / 数组中的数组；
3. **缩短字符串**（``shorten_string``）：按字符串在文档中的先序位置，
   对每个字符串从最后一个字符开始逐个删除。

每轮从上一个接受点重新枚举候选（贪心下降）。对每个候选：

* 序列化长度必须**严格变小**（与当前文档用同一紧凑 JSON 序列化比较）；
* 用旧、新两版 schema **重新校验**后，文档仍属于请求指定的单向破坏
  类别（``backward``：旧版合法、新版拒绝；``forward``：新版合法、
  旧版拒绝）。

两个条件同时满足才接受该候选并记录一步操作；否则尝试下一候选。循环
直到一轮枚举中没有候选可接受（不能再缩减），或重验次数达到
``max_validations`` 上限——达到上限时只返回**最后一次已验证通过**的
文档，绝不返回未经验证的候选。

入口文档若不属于指定方向（两版都合法 / 两版都拒绝 / 属于相反方向），
抛出 :class:`NonBreakingDocument`。引擎为纯函数：不读取数据库、不修改
入参文档与 schema、不做任何持久化。
"""
from __future__ import annotations

import copy
import json
from typing import Any, Iterator

from app.compat import make_validator
from app.profile import render_instance_pointer

# 单次缩减允许的最大重验次数缺省值（每次重验 = 旧、新两版各校验一次）。
DEFAULT_MAX_VALIDATIONS = 200

# max_validations 的硬上限，防止单请求校验次数失控。
MAX_VALIDATIONS_LIMIT = 10000


class NonBreakingDocument(ValueError):
    """入口文档不属于指定方向的单向破坏类别。

    属性：

    * ``direction``：请求指定的方向（``backward`` / ``forward``）；
    * ``old_valid`` / ``new_valid``：入口文档对旧 / 新 schema 的校验结果；
    * ``reason``：``valid_both``（两版都合法）、``invalid_both``
      （两版都拒绝）、``wrong_direction``（属于相反的单向破坏类别）；
    * ``actual_direction``：``wrong_direction`` 时文档实际所属方向。
    """

    def __init__(self, *, direction: str, old_valid: bool, new_valid: bool,
                 reason: str, actual_direction: str | None = None):
        self.direction = direction
        self.old_valid = old_valid
        self.new_valid = new_valid
        self.reason = reason
        self.actual_direction = actual_direction
        messages = {
            "valid_both": "文档对旧版与新版 schema 均校验通过，不属于任何单向破坏类别",
            "invalid_both": "文档被旧版与新版 schema 同时拒绝，不属于单向破坏类别",
            "wrong_direction": (
                f"文档属于 {actual_direction} 方向的破坏样本，"
                f"与请求指定的 {direction} 方向不一致"
            ),
        }
        super().__init__(messages[reason])


# ---------------------------------------------------------------------------
# 校验与方向判定
# ---------------------------------------------------------------------------


def _is_valid(validator: Any, document: Any) -> bool:
    return next(validator.iter_errors(document), None) is None


def _direction_holds(direction: str, old_ok: bool, new_ok: bool) -> bool:
    """文档是否仍属于指定方向的单向破坏类别。"""
    if direction == "backward":
        # 向后破坏：旧版合法、新版拒绝（单向，new_ok 必为 False）
        return old_ok and not new_ok
    # 向前破坏：新版合法、旧版拒绝
    return new_ok and not old_ok


def _classify(old_ok: bool, new_ok: bool) -> str:
    if old_ok and new_ok:
        return "valid_both"
    if not old_ok and not new_ok:
        return "invalid_both"
    # 恰有一版拒绝：旧版拒绝即 forward，新版拒绝即 backward
    return "forward" if new_ok else "backward"


# ---------------------------------------------------------------------------
# 序列化长度
# ---------------------------------------------------------------------------


def _serialized_length(document: Any) -> int:
    """紧凑 JSON 序列化长度；候选与当前文档使用完全相同的序列化参数。"""
    return len(json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ))


# ---------------------------------------------------------------------------
# 候选枚举（固定顺序：对象属性 -> 数组元素 -> 字符串缩短）
# ---------------------------------------------------------------------------


Path = tuple[Any, ...]


def _collect_targets(
    value: Any, path: Path,
    properties: list[Path], elements: list[Path], strings: list[Path],
) -> None:
    """深度优先收集三类候选目标。

    * 对象：先登记本节点全部属性（键按字典序），再下钻嵌套容器；
    * 数组：先登记本数组全部元素下标，再下钻嵌套容器；
    * 字符串：在所属容器的属性 / 元素遍历中登记；根节点本身为字符串
      （``path`` 为空）时同样登记，使根字符串也能进入缩短阶段。
    """
    if isinstance(value, str):
        strings.append(path)
    elif isinstance(value, dict):
        keys = sorted(value)
        for key in keys:
            properties.append((*path, key))
        for key in keys:
            child = value[key]
            if isinstance(child, str):
                strings.append((*path, key))
            elif isinstance(child, (dict, list)):
                _collect_targets(child, (*path, key), properties, elements, strings)
    elif isinstance(value, list):
        for index in range(len(value)):
            elements.append((*path, index))
        for index, item in enumerate(value):
            if isinstance(item, str):
                strings.append((*path, index))
            elif isinstance(item, (dict, list)):
                _collect_targets(item, (*path, index), properties, elements, strings)


def _at(root: Any, path: Path) -> Any:
    node = root
    for token in path:
        node = node[token]
    return node


def _candidate_removed(root: Any, path: Path) -> Any:
    """深拷贝根文档并删除 path 指向的对象属性或数组元素。"""
    candidate = copy.deepcopy(root)
    node = _at(candidate, path[:-1]) if path[:-1] else candidate
    del node[path[-1]]
    return candidate


def _candidate_shortened(root: Any, path: Path, position: int) -> Any:
    """深拷贝根文档并把 path 处字符串的 position 位置字符删除。"""
    candidate = copy.deepcopy(root)
    if not path:
        # 根节点本身为字符串：直接在副本上构造缩短结果
        return candidate[:position] + candidate[position + 1:]
    node = _at(candidate, path[:-1])
    original = node[path[-1]]
    node[path[-1]] = original[:position] + original[position + 1:]
    return candidate


def _iter_candidates(root: Any) -> Iterator[tuple[Any, dict]]:
    """按固定顺序产出 (候选文档, 操作描述)；每次迭代深拷贝，互不影响。"""
    properties: list[Path] = []
    elements: list[Path] = []
    strings: list[Path] = []
    _collect_targets(root, (), properties, elements, strings)

    # 阶段 1：删除对象属性（根节点自身属性优先，再逐层深入）
    for path in properties:
        yield _candidate_removed(root, path), {
            "op": "delete_property",
            "pointer": render_instance_pointer(path),
            "key": str(path[-1]),
            "element_index": None,
            "position": None,
            "char": None,
            "from_length": None,
            "to_length": None,
        }

    # 阶段 2：删除数组元素（按下标从小到大）
    for path in elements:
        yield _candidate_removed(root, path), {
            "op": "delete_element",
            "pointer": render_instance_pointer(path),
            "key": None,
            "element_index": int(path[-1]),
            "position": None,
            "char": None,
            "from_length": None,
            "to_length": None,
        }

    # 阶段 3：缩短字符串（先序定位；每个字符串从末位字符开始逐个删除）
    for path in strings:
        original = _at(root, path)
        for position in range(len(original) - 1, -1, -1):
            yield _candidate_shortened(root, path, position), {
                "op": "shorten_string",
                "pointer": render_instance_pointer(path),
                "key": None,
                "element_index": None,
                "position": position,
                "char": original[position],
                "from_length": len(original),
                "to_length": len(original) - 1,
            }


# ---------------------------------------------------------------------------
# 缩减主流程
# ---------------------------------------------------------------------------


def reduce_counterexample(
    old_schema: Any,
    new_schema: Any,
    document: Any,
    direction: str,
    max_validations: int = DEFAULT_MAX_VALIDATIONS,
) -> dict:
    """缩减一份单向破坏反例文档（纯函数）。

    ``max_validations`` 限制候选**重验**次数（旧、新两版各校验一次算
    一次重验）；入口分类校验不计入。达到上限时立即停止，返回最后一次
    已验证通过的文档，``limit_reached`` 为 ``True``。
    """
    old_validator = make_validator(old_schema)
    new_validator = make_validator(new_schema)

    old_ok = _is_valid(old_validator, document)
    new_ok = _is_valid(new_validator, document)
    actual = _classify(old_ok, new_ok)
    if actual != direction:
        raise NonBreakingDocument(
            direction=direction,
            old_valid=old_ok,
            new_valid=new_ok,
            reason=actual if actual in ("valid_both", "invalid_both")
            else "wrong_direction",
            actual_direction=actual if actual in ("backward", "forward") else None,
        )

    current = copy.deepcopy(document)
    current_size = _serialized_length(current)
    steps: list[dict] = []
    validations_used = 0
    limit_reached = False

    while True:
        accepted = False
        for candidate, operation in _iter_candidates(current):
            if validations_used >= max_validations:
                limit_reached = True
                break

            validations_used += 1
            candidate_size = _serialized_length(candidate)
            # 只接受序列化长度严格变小的结果（删除式操作通常恒变小，
            # 此判定为显式门禁）；长度未变小的候选不再做两版重验
            if candidate_size >= current_size:
                continue

            candidate_old_ok = _is_valid(old_validator, candidate)
            candidate_new_ok = _is_valid(new_validator, candidate)
            if not _direction_holds(direction, candidate_old_ok, candidate_new_ok):
                continue

            steps.append({
                "index": len(steps),
                **operation,
                "serialized_length_before": current_size,
                "serialized_length_after": candidate_size,
                "validations_used": validations_used,
            })
            current = candidate
            current_size = candidate_size
            accepted = True
            break  # 接受后从上一个接受点重新枚举

        if limit_reached or not accepted:
            break

    return {
        "direction": direction,
        "original": copy.deepcopy(document),
        "reduced": current,
        "steps": steps,
        "validation_count": validations_used,
        "max_validations": max_validations,
        "limit_reached": limit_reached,
        "serialized_length_original": _serialized_length(document),
        "serialized_length_reduced": current_size,
    }
