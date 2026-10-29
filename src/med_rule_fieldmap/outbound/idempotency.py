"""出站回写幂等：抑制重复回写请求。

**回写幂等键** = ``sha256(判定幂等键 ｜ 请求体哈希)``（spec.md §5「幂等与并发重试」）

* **判定幂等键** = ``sha256(batch_no｜prescription_no｜rule_id｜rule_version)``
  ——同一批次同一条处方同一条规则版本，判定侧只落一行
  （:attr:`med_rule_fieldmap.models.JudgementRow.idempotency_key`）；
* **请求体哈希** = ``sha256(规范化 JSON)``
  ——请求内容变了（药师改了结论或依据），才允许再次发出。

两者合起来保证现场最关心的两件事：

* **同一批次被重投 → 判定行数不变 → 回写请求数不变**
  （药师端不会看到同一条处方两条复核记录，临床上不可接受）；
* **判定结论真的改了 → 请求体哈希变了 → 允许再发一条**
  （不误杀真正需要重发的回写）。

**为什么请求体也要进键里**：光有判定幂等键不够——药师撤回确认后又重新确认，
判定行还是那一行，但回写请求体里的 ``reviewed_by`` / ``reviewed_at`` 变了，
这时应当允许再发一次；把请求体哈希并进键里，才能把「重复投递」和
「结论变更后的重发」区分开。

**与状态库的关系**：守卫可挂在一个 :class:`med_rule_fieldmap.store.Store` 上
（``writeback_requests`` 表），守护进程重启后从库里恢复已发出的回写身份，
不会因为重启把同一条结论再发一遍。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from ..models import WritebackAttempt, WritebackStatus, now_iso

#: 回写幂等键内部的分隔符（判定幂等键 与 请求体哈希 之间）。
WRITE_BACK_KEY_SEP = "|"

#: 请求体哈希的十六进制长度（sha256）。
_HASH_LEN = 64


# ---------------------------------------------------------------------------
# 请求体规范化与哈希
# ---------------------------------------------------------------------------


def _json_default(value: object) -> Any:
    """把非 JSON 原生类型折成稳定可比的文本（不抛错、不丢信息）。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def canonical_request_body(payload: Mapping[str, Any]) -> str:
    """请求体的规范形式（键序无关）。

    同一份请求体无论字段书写顺序如何，规范化结果一致——否则「同一个请求」
    会被算出两个不同的哈希，重复回写就抑制不住。
    """
    if not isinstance(payload, Mapping):
        raise TypeError(f"回写请求体必须是映射类型，收到：{type(payload).__name__}")
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )


def hash_request_body(payload: Mapping[str, Any]) -> str:
    """请求体哈希（sha256 十六进制）。"""
    return hashlib.sha256(canonical_request_body(payload).encode("utf-8")).hexdigest()


def writeback_idempotency_key(judgement_key: str, payload: Mapping[str, Any]) -> str:
    """回写幂等键 = ``sha256(判定幂等键 ｜ 请求体哈希)``。

    :param judgement_key: 判定幂等键（sha256 十六进制）。
    :param payload: 回写请求体（spec.md §2.3 的字段集）。
    """
    if not judgement_key:
        raise ValueError("判定幂等键为空，无法计算回写幂等键")
    body_hash = hash_request_body(payload)
    return hashlib.sha256(
        f"{judgement_key}{WRITE_BACK_KEY_SEP}{body_hash}".encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# 幂等决策
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdempotencyDecision:
    """一次回写的放行 / 抑制决策。"""

    writeback_key: str
    allowed: bool
    reason: str
    previous_status: str | None = None
    request_hash: str = ""


@dataclass(frozen=True)
class WritebackOutcome:
    """一次回写尝试的结果（供调用方记账与审计）。"""

    writeback_key: str
    judgement_key: str
    request_hash: str
    sent: bool
    suppressed: bool
    reason: str
    result: Any = None


# ---------------------------------------------------------------------------
# 幂等守卫
# ---------------------------------------------------------------------------


class WritebackIdempotencyGuard:
    """出站回写幂等守卫。

    **用法**：先把守卫挂到状态库上（守护进程重启后恢复），再由
    :class:`GuardedWriter` 在真正发出前问一次「这条能发吗」。

    :param ledger: 可选的状态库（需实现 ``claim_writeback_request`` /
        ``mark_writeback_sent`` / ``mark_writeback_failed`` /
        ``list_writeback_requests``）；不给就在内存里维护，重启后不保留。
    """

    def __init__(self, ledger: Any | None = None) -> None:
        self._ledger = ledger
        self._lock = threading.RLock()
        self._attempts: dict[str, WritebackAttempt] = {}
        self._suppressed = 0
        self._allowed = 0
        if ledger is not None:
            self.restore(ledger.list_writeback_requests())

    # -- 恢复 ---------------------------------------------------------------

    def restore(self, attempts: Iterable[WritebackAttempt]) -> int:
        """从状态库恢复已知的回写身份（守护进程重启时调用）。"""
        restored = 0
        with self._lock:
            for attempt in attempts:
                self._attempts[attempt.writeback_key] = attempt
                restored += 1
        return restored

    # -- 决策 ---------------------------------------------------------------

    def claim(self, judgement_key: str, payload: Mapping[str, Any]) -> IdempotencyDecision:
        """问一次「这条回写能发吗」。

        * 新键 → 放行（占位）；
        * 已回写（``SENT``）→ 抑制；
        * 正在回写（``CLAIMED``）→ 抑制（并发重复）；
        * 上一��失败（``FAILED``）→ 放行（按退避策略重试）。
        """
        writeback_key = writeback_idempotency_key(judgement_key, payload)
        request_hash = hash_request_body(payload)
        with self._lock:
            if self._ledger is not None:
                # 状态库侧的占位是原子的（主键冲突即视为已被占位），
                # 多进程共库时也不会两条都占成功。
                claimed, previous = self._ledger.claim_writeback_request(
                    writeback_key, judgement_key, request_hash
                )
                if claimed:
                    self._attempts[writeback_key] = WritebackAttempt(
                        writeback_key=writeback_key,
                        judgement_key=judgement_key,
                        request_hash=request_hash,
                        status=WritebackStatus.CLAIMED,
                    )
                    self._allowed += 1
                    return IdempotencyDecision(
                        writeback_key=writeback_key,
                        allowed=True,
                        reason="首次回写，允许发出",
                        request_hash=request_hash,
                    )
            else:
                previous = self._attempts.get(writeback_key)
                if previous is None:
                    self._attempts[writeback_key] = WritebackAttempt(
                        writeback_key=writeback_key,
                        judgement_key=judgement_key,
                        request_hash=request_hash,
                        status=WritebackStatus.CLAIMED,
                    )
                    self._allowed += 1
                    return IdempotencyDecision(
                        writeback_key=writeback_key,
                        allowed=True,
                        reason="首次回写，允许发出",
                        request_hash=request_hash,
                    )

            if previous.status is WritebackStatus.SENT:
                self._suppressed += 1
                return IdempotencyDecision(
                    writeback_key=writeback_key,
                    allowed=False,
                    reason="同一结论已回写过（回写幂等键一致），本次请求被抑制",
                    previous_status=previous.status.value,
                    request_hash=request_hash,
                )

            if previous.status is WritebackStatus.CLAIMED:
                self._suppressed += 1
                return IdempotencyDecision(
                    writeback_key=writeback_key,
                    allowed=False,
                    reason="同一请求正在回写中（并发重复），本次请求被抑制",
                    previous_status=previous.status.value,
                    request_hash=request_hash,
                )

            # FAILED —— 允许按退避策略重试（同��份请求，不是重复投递）
            self._allowed += 1
            return IdempotencyDecision(
                writeback_key=writeback_key,
                allowed=True,
                reason="上一次回写失败，允许按退避策略重试",
                previous_status=previous.status.value,
                request_hash=request_hash,
            )

    def mark_sent(self, writeback_key: str) -> bool:
        """标记回写已成功发出。"""
        with self._lock:
            if self._ledger is not None:
                self._ledger.mark_writeback_sent(writeback_key)
            attempt = self._attempts.get(writeback_key)
            if attempt is None:
                return False
            self._attempts[writeback_key] = replace(
                attempt, status=WritebackStatus.SENT, updated_at=now_iso()
            )
            return True

    def mark_failed(self, writeback_key: str) -> bool:
        """标记回写失败（重试次数累加）。"""
        with self._lock:
            if self._ledger is not None:
                self._ledger.mark_writeback_failed(writeback_key)
            attempt = self._attempts.get(writeback_key)
            if attempt is None:
                return False
            self._attempts[writeback_key] = replace(
                attempt,
                status=WritebackStatus.FAILED,
                retry_count=attempt.retry_count + 1,
                updated_at=now_iso(),
            )
            return True

    # -- 查询 ---------------------------------------------------------------

    def attempts(self) -> list[WritebackAttempt]:
        """已知的回写身份（按占位顺序）。"""
        with self._lock:
            return list(self._attempts.values())

    def stats(self) -> dict[str, int]:
        """运行统计（放行数 / 抑制数 / 各状态计数）。"""
        with self._lock:
            counts = {status.value: 0 for status in WritebackStatus}
            for attempt in self._attempts.values():
                counts[attempt.status.value] += 1
            return {
                "allowed": self._allowed,
                "suppressed": self._suppressed,
                "total": len(self._attempts),
                "claimed": counts[WritebackStatus.CLAIMED.value],
                "sent": counts[WritebackStatus.SENT.value],
                "failed": counts[WritebackStatus.FAILED.value],
            }

    # -- 内部 ---------------------------------------------------------------


# ---------------------------------------------------------------------------
# 接在真实出站调用上的守卫
# ---------------------------------------------------------------------------


class GuardedWriter:
    """把幂等守卫接到真实的出站调用上。

    :param send_fn: 真正的发送函数 ``(judgement_key, payload) -> Any``；
        出站客户端（``outbound/his_review.py``）在下一条任务里接进来。
    :param guard: :class:`WritebackIdempotencyGuard` 实例。
    """

    def __init__(
        self,
        send_fn: Any,
        guard: WritebackIdempotencyGuard,
    ) -> None:
        self._send = send_fn
        self._guard = guard

    def write(self, judgement_key: str, payload: Mapping[str, Any]) -> WritebackOutcome:
        """发一次回写（幂等）。

        * 被抑制 → 一次请求都不发，返回 ``sent=False, suppressed=True``；
        * 发送失败 → 记失败并把原始异常抛给调用方（退避队列据此重试），
          **不静默吞掉**。
        """
        decision = self._guard.claim(judgement_key, payload)
        if not decision.allowed:
            return WritebackOutcome(
                writeback_key=decision.writeback_key,
                judgement_key=judgement_key,
                request_hash=decision.request_hash,
                sent=False,
                suppressed=True,
                reason=decision.reason,
            )
        try:
            result = self._send(judgement_key, payload)
        except Exception:
            self._guard.mark_failed(decision.writeback_key)
            raise
        self._guard.mark_sent(decision.writeback_key)
        return WritebackOutcome(
            writeback_key=decision.writeback_key,
            judgement_key=judgement_key,
            request_hash=decision.request_hash,
            sent=True,
            suppressed=False,
            reason="回写已发出",
            result=result,
        )


__all__ = [
    "WRITE_BACK_KEY_SEP",
    "GuardedWriter",
    "IdempotencyDecision",
    "WritebackIdempotencyGuard",
    "WritebackOutcome",
    "canonical_request_body",
    "hash_request_body",
    "writeback_idempotency_key",
]
