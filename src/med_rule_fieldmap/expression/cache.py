"""解析树 LRU 缓存 + 规则文本热重载失效钩子。

**对应源产品能力**：源仓库 ``web-frontend/modules/core/formula/index.js`` 的
``getCachedParseTree()`` 与 ``PARSE_TREE_CACHE_MAX_SIZE = 512``——同一条公式文本
共享一棵解析树，避免逐行重复解析；源实现里 ``Map`` 按插入顺序迭代，命中后
重新插入，因此淘汰的是**最近最少使用**而不是先进先出。

**本轮改写**：源产品缓存的键只有「公式文本」。本轮多一层**规则号绑定**：
缓存条目同时记录「哪条规则在用这棵树、这棵树对应哪一版表达式文本」。
药事审核岗改了一条规则的表达式之后，旧解析树必须**立即作废**，不能等 LRU
自然淘汰——否则热重载之后语义仍是旧版，台账里会带着一条已经不存在的规则在跑。

**共享前提**：AST 节点全是冻结数据类（不可变），求值器只读不写，因此同一棵树
可以安全地被多条规则、多条医嘱行共用。源产品源码对这条有同样的注释：
缓存只在访问器把树当只读时才安全。本轮把它写进类文档，是为了让后来改代码的人
知道这条前提被打破时会出什么事。

**缓存里存什么、不存什么**

* 存：解析成功的 AST（键 = 表达式文本）；
* 不存：解析失败的表达式——配置错误必须在载入期暴露，不能被缓存悄悄吞掉；
* 不存：求值结果——求值依赖医嘱行上下文，缓存结果会引入过期语义。
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from .ast import Node
from .lexer import Token
from .parser import parse

#: 解析树缓存上限（对齐源产品 ``PARSE_TREE_CACHE_MAX_SIZE = 512``）。
PARSE_TREE_CACHE_MAX_SIZE = 512


def _parse_with_tokens(text: str, tokens: list[Token] | None = None) -> Node:
    """默认解析器：与 :func:`parser.parse` 同源，额外接受已切好的 token。

    载入期预检（``validator.py``）需要 token 列表做能力扫描，把切好的 token
    传进来可以避免同一次载入里切两遍词。
    """
    return parse(text, tokens=tokens)


#: 解析器协议：``(表达式文本, 已切好的 token 或 None) -> AST``。
#: 注入自定义解析器（测试里用来数解析次数）必须遵守这个签名。
ParserCallable = Callable[[str, list[Token] | None], Node]


def expression_digest(expression: str) -> str:
    """表达式内容哈希（sha256 前 16 位）。

    规则绑定以「规则号 + 内容哈希」为键：内容变了哈希就变，旧解析树随之作废。
    """
    return hashlib.sha256(expression.strip().encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CacheStats:
    """缓存运行指标（进运行摘要，供 CLI status 与规模化回归读取）。

    字段口径：

    * ``hits`` —— 命中缓存的次数；
    * ``misses`` —— 未命中、需要解析的次数（解析失败也算一次未命中）；
    * ``parses`` —— **成功产出 AST** 的解析次数；解析失败不计入——失败不会进缓存，
      下次还得重来，所以「未命中 − 解析」正是反复失败的那部分；
    * ``evictions`` —— 因超出上限被 LRU 淘汰的条目数；
    * ``invalidations`` —— 因规则文本变更被显式作废的条目数；
    * ``size`` / ``max_size`` —— 当前存量与上限。
    """

    hits: int = 0
    misses: int = 0
    parses: int = 0
    evictions: int = 0
    invalidations: int = 0
    size: int = 0
    max_size: int = PARSE_TREE_CACHE_MAX_SIZE

    @property
    def hit_rate(self) -> float:
        """命中率；一次都没查过时按 0 计（不抛除零错误）。"""
        total = self.hits + self.misses
        if total == 0:
            return 0.0
        return self.hits / total

    def to_dict(self) -> dict[str, float | int]:
        """转成可直接进 JSON 的字典（运行摘要与 CLI ``--json`` 共用）。"""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "parses": self.parses,
            "evictions": self.evictions,
            "invalidations": self.invalidations,
            "size": self.size,
            "max_size": self.max_size,
            "hit_rate": round(self.hit_rate, 6),
        }

    def render(self) -> str:
        """一行可读摘要（终端摘要行与运行摘要共用同一口径）。"""
        return (
            f"命中 {self.hits} · 未命中 {self.misses} · 解析 {self.parses} 次 · "
            f"淘汰 {self.evictions} · 失效 {self.invalidations} · "
            f"命中率 {self.hit_rate * 100:.1f}% · 存量 {self.size}/{self.max_size}"
        )


class ParseTreeCache:
    """表达式解析树 LRU 缓存。

    典型用法::

        cache = ParseTreeCache()
        tree = cache.get_for_rule("RX-LEGAL-MA-OUT-DAYS", "days_qty > 1")
        # 同一规则、同一文本反复求值 → 只解析一次
        tree = cache.get_for_rule("RX-LEGAL-MA-OUT-DAYS", "days_qty > 1")

    规则文本改写后语义立即变化::

        cache.get_for_rule("RX-A", "days_qty > 1")   # 绑定旧文本
        cache.get_for_rule("RX-A", "days_qty > 5")   # 旧解析树立即作废，重新解析
    """

    def __init__(
        self,
        max_size: int = PARSE_TREE_CACHE_MAX_SIZE,
        parser: ParserCallable | None = None,
    ) -> None:
        self._max_size = max(1, int(max_size))
        self._parser: ParserCallable = parser or _parse_with_tokens
        #: 表达式文本 -> AST（``OrderedDict`` 兼作 LRU 表：命中后 ``move_to_end``）
        self._entries: OrderedDict[str, Node] = OrderedDict()
        #: 规则号 -> 当前绑定的表达式内容哈希
        self._rule_digest: dict[str, str] = {}
        #: 规则号 -> 当前绑定的表达式文本
        self._rule_text: dict[str, str] = {}
        #: 表达式文本 -> 绑定它的规则数（引用计数，决定能否作废该条目）
        self._text_refs: dict[str, int] = {}
        self._hits = 0
        self._misses = 0
        self._parses = 0
        self._evictions = 0
        self._invalidations = 0
        #: 可重入锁：守护进程（T33）会用线程池跑批次，命中后 ``move_to_end``
        #: 与淘汰是非原子操作，多线程共用一个缓存时必须串起来。
        self._lock = threading.RLock()

    # -- 查询 ---------------------------------------------------------------

    def get(self, expression: str, *, tokens: list[Token] | None = None) -> Node:
        """取解析树；未命中则解析并入缓存。

        ``tokens`` 是已经切好的词法单元（载入期预检会先切词做能力扫描，
        传进来可以少切一次）。命中缓存时忽略该参数——解析树只由文本决定。
        """
        text = expression.strip()
        with self._lock:
            node = self._entries.get(text)
            if node is not None:
                self._hits += 1
                self._entries.move_to_end(text)
                return node
            self._misses += 1
            node = self._parser(text, tokens)
            self._parses += 1
            self._store(text, node)
            return node

    def get_for_rule(self, rule_id: str, expression: str) -> Node:
        """按规则号取解析树，并在表达式文本变更时把旧解析树立即作废。

        绑定键是「规则号 + 表达式内容哈希」：文本变了哈希就变，旧条目当场作废，
        不等 LRU 自然淘汰。
        """
        text = expression.strip()
        digest = expression_digest(text)
        with self._lock:
            previous = self._rule_digest.get(rule_id)
            if previous == digest:
                # 绑定未变：直接命中（不重复计数引用）
                return self.get(text)
            if previous is not None:
                self._release(rule_id)
            self._rule_digest[rule_id] = digest
            self._rule_text[rule_id] = text
            self._text_refs[text] = self._text_refs.get(text, 0) + 1
            return self.get(text)

    # -- 失效 ---------------------------------------------------------------

    def invalidate_rule(self, rule_id: str) -> bool:
        """解除一条规则的绑定；它独占的解析树立即作废。

        规则被删掉（规则行下线）时调用。返回是否确实作废了一条解析树。
        """
        with self._lock:
            if rule_id not in self._rule_digest:
                return False
            self._release(rule_id)
            return True

    def invalidate_expression(self, expression: str) -> bool:
        """作废某个表达式文本对应的解析树（不经规则号，直接点名）。"""
        text = expression.strip()
        with self._lock:
            if self._entries.pop(text, None) is None:
                return False
            self._invalidations += 1
            return True

    def clear(self) -> None:
        """清空缓存内容（保留累计统计）。"""
        with self._lock:
            self._entries.clear()
            self._rule_digest.clear()
            self._rule_text.clear()
            self._text_refs.clear()

    def reset_stats(self) -> None:
        """清零累计统计（内容不动）。"""
        with self._lock:
            self._hits = 0
            self._misses = 0
            self._parses = 0
            self._evictions = 0
            self._invalidations = 0

    # -- 观测 ---------------------------------------------------------------

    @property
    def stats(self) -> CacheStats:
        """当前缓存统计。"""
        with self._lock:
            return CacheStats(
                hits=self._hits,
                misses=self._misses,
                parses=self._parses,
                evictions=self._evictions,
                invalidations=self._invalidations,
                size=len(self._entries),
                max_size=self._max_size,
            )

    @property
    def max_size(self) -> int:
        """缓存上限。"""
        return self._max_size

    def bound_rule_ids(self) -> tuple[str, ...]:
        """当前已绑定的规则号（按规则号排序）。"""
        with self._lock:
            return tuple(sorted(self._rule_digest))

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, expression: object) -> bool:
        if not isinstance(expression, str):
            return False
        with self._lock:
            return expression.strip() in self._entries

    # -- 内部 ---------------------------------------------------------------

    def _store(self, text: str, node: Node) -> None:
        """写入缓存；超出上限时淘汰**最近最少使用**的那一条。"""
        if text in self._entries:
            self._entries.move_to_end(text)
            self._entries[text] = node
            return
        while len(self._entries) >= self._max_size:
            self._entries.popitem(last=False)
            self._evictions += 1
        self._entries[text] = node

    def _release(self, rule_id: str) -> None:
        """解除一条规则的绑定；它独占的解析树立即作废。

        引用计数的作用：两条规则用同一段表达式文本时，其中一条改文本，
        另一条仍在用——那棵树不能被作废，否则另一条规则被迫重新解析。
        """
        self._rule_digest.pop(rule_id, None)
        if rule_id not in self._rule_text:
            return
        text = self._rule_text.pop(rule_id)
        count = self._text_refs.get(text, 0) - 1
        if count > 0:
            self._text_refs[text] = count
            return
        self._text_refs.pop(text, None)
        if self._entries.pop(text, None) is not None:
            self._invalidations += 1


# ---------------------------------------------------------------------------
# 进程级默认缓存
# ---------------------------------------------------------------------------

_DEFAULT_CACHE = ParseTreeCache()


def default_cache() -> ParseTreeCache:
    """进程级默认缓存（守护进程与运维 CLI 共用同一份统计口径）。"""
    return _DEFAULT_CACHE


def cache_stats() -> CacheStats:
    """默认缓存的统计快照。"""
    return _DEFAULT_CACHE.stats


__all__ = [
    "PARSE_TREE_CACHE_MAX_SIZE",
    "CacheStats",
    "ParseTreeCache",
    "ParserCallable",
    "cache_stats",
    "default_cache",
    "expression_digest",
]
