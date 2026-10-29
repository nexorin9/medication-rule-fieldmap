"""跨行判定上下文：一批处方行的聚合视图。

**为什么要有这一层**：本轮判定内核里，**重复给药**（§1 适宜性审核第 5 项）与
**超常处方**（§3 尾段）是**跨行**判定——单看一行永远判不出来：

* 重复给药要看「同一就诊 · 同一药品 · 同日」这一组里有几张；
* 超常处方要看「同一医师」在本批里超常了几次。

其余判定（皮试、量限、规范性、儿童年龄）都是**行内**判定，只看一行就够。
把跨行的那部分单独抽成一个上下文对象，行内判定保持「一行一判」，
两条路径互不干扰——判定链里因此多一个参数，而不是多一层隐式全局状态。

**索引只算一次**：一批 2000 行 × 12 条规则，若每行都重扫一遍分组，
就是 2000 次全表扫描。:meth:`BatchContext.group_by` 与 :meth:`BatchContext.memo`
按口径标识缓存派生结果——同一个口径在一批数据里只建一次索引。

**范围**：上下文只覆盖**一个批次**。跨批次的统计（如同一医师的月度超常处方
次数）不在本轮范围内——落地目录一次只交一批，批次之间的时间跨度由 HIS 决定，
引擎不跨批次猜。药事委员会口径的月度统计属药事管理系统的事，
本轮只把「本批内」的判定做实，边界写在这里，不藏。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ..expression.normalize import normalize_field
from ..models import PrescriptionRow


def row_identity(row: PrescriptionRow) -> str:
    """医嘱行的身份标识（处方号优先，回落到医嘱号）。"""
    return row.prescription_no.strip() or row.order_no.strip()


def order_key(row: PrescriptionRow) -> tuple[str, str]:
    """同组 / 同医师多次开立的先后次序排序键：事件时间优先，处方号兜底。

    事件时间取归一后的 UTC 文本——同一批里所有行的时间都是同一时区基准，
    排序结果稳定可复现。事件时间缺失的排在最后，不影响前面几张的次序。
    """
    event_text = normalize_field("event_time", row.value("event_time")).text
    return (event_text, row_identity(row))


class BatchContext:
    """一批处方行的跨行上下文。

    ``rows`` 是本批次的全部医嘱行（按落地文件行序）。跨行判定模块
    （``engine/duplicate.py`` / ``engine/supernormal.py``）从这里取聚合视图。
    """

    def __init__(self, rows: Sequence[PrescriptionRow] = ()) -> None:
        self.rows: tuple[PrescriptionRow, ...] = tuple(rows)
        self._groups: dict[str, dict[str, tuple[PrescriptionRow, ...]]] = {}
        self._memo: dict[str, Any] = {}

    @classmethod
    def of(cls, rows: Sequence[PrescriptionRow]) -> "BatchContext":
        """由一批处方行构造上下文。"""
        return cls(rows)

    def group_by(
        self,
        tag: str,
        key_func: Callable[[PrescriptionRow], str | None],
    ) -> dict[str, tuple[PrescriptionRow, ...]]:
        """按 key 分组，返回 ``key → 组内医嘱行``。

        ``key_func`` 返回 ``None`` 的行**不进任何组**（键字段不全的行由判定层
        单独落「不可判定」并点名，不混进分组）。

        ``tag`` 是分组口径的标识——同一口径在一批数据里只建一次索引。
        """
        cached = self._groups.get(tag)
        if cached is None:
            buckets: dict[str, list[PrescriptionRow]] = {}
            for row in self.rows:
                key = key_func(row)
                if key is None:
                    continue
                buckets.setdefault(key, []).append(row)
            cached = {key: tuple(items) for key, items in buckets.items()}
            self._groups[tag] = cached
        return cached

    def memo(self, tag: str, factory: Callable[[], Any]) -> Any:
        """缓存一次性派生结果（如超常处方发生清单）。

        ``tag`` 是口径标识；同一个口径只算一次。
        """
        if tag not in self._memo:
            self._memo[tag] = factory()
        return self._memo[tag]

    def __len__(self) -> int:
        return len(self.rows)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"BatchContext(rows={len(self.rows)})"


__all__ = ["BatchContext", "order_key", "row_identity"]
