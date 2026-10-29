"""数据模型：处方行 / 规则行 / 判定行 / 审计行 / 回写请求。

五类行共享同一套字段口径，供台账、状态库与出站回写复用：

* **处方行** 字段集 = spec.md §2.1 的 CSV 列全集（:data:`PRESCRIPTION_FIELDS`）。
* **规则行** 字段集 = spec.md §2.2 的 YAML 规则行（见 ``rules/schema.py`` 的校验）。
* **判定行** 结论固定三态：合规 / 违规 / 不可判定（:class:`Conclusion`），
  业务键 = ``batch_no + prescription_no + rule_id + rule_version``。
* **审计行** 只追加、不覆盖，供飞检与内审自证。
* **回写请求行** 记一次出站回写的幂等身份（:class:`WritebackAttempt`），
  供 ``outbound/idempotency.py`` 抑制重复回写。
* **待重放回写行** 记一条退避重试超次后落库的待重放回写
  （:class:`PendingWriteback`），供 ``outbound/retry_queue.py`` 排队与重放。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum

# ---------------------------------------------------------------------------
# 字段口径
# ---------------------------------------------------------------------------

#: HIS 医嘱落地 CSV 的列全集（spec.md §2.1 A 段，按表头名映射、列序不敏感）。
PRESCRIPTION_FIELDS: tuple[str, ...] = (
    "batch_no",
    "order_no",
    "prescription_no",
    "visit_no",
    "patient_ref",
    "dept_code",
    "dept_name",
    "prescriber_id",
    "prescriber_name",
    "drug_code",
    "drug_name",
    "spec",
    "dosage_form",
    "dosage_text",
    "dosage_value",
    "dosage_unit",
    "route",
    "freq",
    "quantity",
    "rx_type",
    "drug_category",
    "days_qty",
    "extend_reason",
    "skin_test_result",
    "clinical_diag",
    "age_value",
    "age_unit",
    "event_time",
)

#: 数值型字段（原始落地文件里可能带单位后缀，读入时按容错解析）。
NUMERIC_FIELDS: tuple[str, ...] = ("dosage_value", "quantity", "days_qty", "age_value")

#: 判定业务键的分隔符（spec.md §5 幂等键口径）。
BUSINESS_KEY_SEP = "｜"

_LEADING_NUMBER = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)")


class Conclusion(str, Enum):
    """判定结论三态。

    第三态「不可判定」不是失败，而是正式结论：系统筛出的不合理处方与
    **系统不能审核的部分必须人工审核**（《处方审核规则体系》§4）。
    """

    COMPLIANT = "合规"
    VIOLATION = "违规"
    UNDETERMINABLE = "不可判定"

    @property
    def wire_value(self) -> str:
        """出站回写请求体使用的英文取值（spec.md §2.3）。"""
        return {
            Conclusion.COMPLIANT: "compliant",
            Conclusion.VIOLATION: "violation",
            Conclusion.UNDETERMINABLE: "undeterminable",
        }[self]

    @classmethod
    def from_value(cls, value: object) -> "Conclusion":
        """接受中文结论名、英文 wire 值与枚举本身。"""
        if isinstance(value, cls):
            return value
        text = str(value).strip()
        for member in cls:
            if text == member.value or text == member.wire_value:
                return member
        raise ValueError(f"未知判定结论：{value!r}（合法取值：合规 / 违规 / 不可判定）")


class JudgementState(str, Enum):
    """判定行状态（spec.md §5 业务状态生命周期）。

    合法跃迁表与重判留痕由 ``engine/statemachine.py`` 负责，本模块只给枚举。
    """

    PENDING = "待判定"
    DECIDED = "已判定"
    PENDING_FIELDS = "待补字段"
    CONFIRMED = "药师已确认"
    WRITTEN_BACK = "已回写"
    WITHDRAWN = "已撤回"
    SUSPENDED = "已挂起"


class RuleLayer(str, Enum):
    """审核三层次（《处方审核规则体系》§1）。"""

    LEGALITY = "合法性"
    NORMATIVITY = "规范性"
    APPROPRIATENESS = "适宜性"

    @classmethod
    def from_value(cls, value: object) -> "RuleLayer":
        text = str(value).strip()
        for member in cls:
            if text == member.value:
                return member
        alias = {
            "合法性审核": cls.LEGALITY,
            "legality": cls.LEGALITY,
            "规范性审核": cls.NORMATIVITY,
            "normativity": cls.NORMATIVITY,
            "适宜性审核": cls.APPROPRIATENESS,
            "appropriateness": cls.APPROPRIATENESS,
        }
        if text in alias:
            return alias[text]
        raise ValueError(
            f"未知审核层次：{value!r}（合法取值：合法性 / 规范性 / 适宜性）"
        )


def now_iso() -> str:
    """当前时间（UTC，秒级 ISO8601），供审计行与回写请求体使用。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# 处方行
# ---------------------------------------------------------------------------


@dataclass
class PrescriptionRow:
    """一条 HIS 医嘱落地记录（26+1 列全集）。

    字段一律存原始文本：落地文件里「3日量」「5mg」「qd」这类写法要到归一层
    （``expression/normalize.py``）才转成可比较的值，入站阶段不做猜测。
    """

    batch_no: str = ""
    order_no: str = ""
    prescription_no: str = ""
    visit_no: str = ""
    patient_ref: str = ""
    dept_code: str = ""
    dept_name: str = ""
    prescriber_id: str = ""
    prescriber_name: str = ""
    drug_code: str = ""
    drug_name: str = ""
    spec: str = ""
    dosage_form: str = ""
    dosage_text: str = ""
    dosage_value: str = ""
    dosage_unit: str = ""
    route: str = ""
    freq: str = ""
    quantity: str = ""
    rx_type: str = ""
    drug_category: str = ""
    days_qty: str = ""
    extend_reason: str = ""
    skin_test_result: str = ""
    clinical_diag: str = ""
    age_value: str = ""
    age_unit: str = ""
    event_time: str = ""
    #: 该条记录里**实际存在**的医嘱列名集合（入站层按表头 / 通道口径登记）。
    #:
    #: 空集合 = 未知（视为所有列都存在）——手工构造的行保持旧行为。
    #: 非空时可用于区分两种「没有值」：**整列未落地**（接口没回传这一列）
    #: 与**栏位在但内容为空**（医师没填）。前者是接口字段缺口，后者是
    #: 处方本身不合格——两者结论不同，见 ``engine/normativity.py``。
    present_columns: frozenset[str] = frozenset()

    def value(self, name: str) -> str:
        """取字段值；未知字段名抛错而不是静默返回空串。"""
        if name not in PRESCRIPTION_FIELDS:
            raise KeyError(f"未知医嘱字段：{name}")
        return getattr(self, name)

    def has_column(self, name: str) -> bool:
        """该条记录里是否存在这一列。

        未登记 ``present_columns`` 时一律视为存在（保持旧行为）；
        登记后只认登记过的列——「整列未落地」与「栏位在但内容为空」就此分开。
        """
        if name not in PRESCRIPTION_FIELDS:
            raise KeyError(f"未知医嘱字段：{name}")
        if not self.present_columns:
            return True
        return name in self.present_columns

    def is_missing(self, name: str) -> bool:
        """字段缺失判定：空串或纯空白即视为缺失。

        注意与「显式否」区分：``skin_test_result = '未注明'`` 是有值的显式否定，
        不是缺失——两者的判定结论不同（见 ``engine/skin_test.py``）。
        """
        return not self.value(name).strip()

    def missing_fields(self, required: Iterable[str]) -> tuple[str, ...]:
        """返回 ``required`` 中缺失的字段名（保持声明顺序）。"""
        return tuple(name for name in required if self.is_missing(name))

    def numeric(self, name: str) -> Decimal | None:
        """数值字段容错解析：容忍单位后缀（``3日量`` → ``3``）；不可解析返回 ``None``。"""
        if name not in NUMERIC_FIELDS:
            raise KeyError(f"{name} 不是数值型医嘱字段")
        text = self.value(name).strip()
        if not text:
            return None
        match = _LEADING_NUMBER.match(text)
        if not match:
            return None
        try:
            return Decimal(match.group(1))
        except InvalidOperation:  # pragma: no cover - 正则已保证可解析
            return None


# ---------------------------------------------------------------------------
# 规则行
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleRow:
    """一条审方规则行（spec.md §2.2）。

    ``basis`` 是硬要求：没有依据出处的规则不得进入生效集
    （《处方审核规则体系》§4「有明确的临床用药依据来源」）。
    """

    rule_id: str
    layer: str
    basis: str
    version: str
    effective_from: date | None = None
    effective_to: date | None = None
    applies_when: Mapping[str, object] = field(default_factory=dict)
    required_fields: tuple[str, ...] = ()
    expression: str = ""
    severity: str | None = None
    missing_conclusion: Conclusion = Conclusion.UNDETERMINABLE
    forbidden_terms: tuple[str, ...] = ()
    note: str = ""
    #: 量限矩阵单元引用（``层级/类别``，如 ``门急诊一般/麻醉药品注射剂``）。
    #:
    #: 非空表示本规则是**量限规则**：阈值与口径由 ``engine/limits.py`` 的
    #: §3 矩阵提供，规则行只声明「落在哪个矩阵单元」，不自己写阈值。
    limit_ref: str = ""
    source_file: str = ""

    def effective_on(self, as_of: date) -> bool:
        """生效区间判定（闭区间；``effective_to`` 为空表示长期有效）。"""
        if self.effective_from is not None and as_of < self.effective_from:
            return False
        if self.effective_to is not None and as_of > self.effective_to:
            return False
        return True

    def to_dict(self) -> dict[str, object]:
        """可 JSON 序列化的快照（CLI ``--json`` 输出用）。"""
        return {
            "rule_id": self.rule_id,
            "layer": self.layer,
            "basis": self.basis,
            "version": self.version,
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "applies_when": {k: v for k, v in self.applies_when.items()},
            "required_fields": list(self.required_fields),
            "expression": self.expression,
            "severity": self.severity,
            "missing_conclusion": self.missing_conclusion.value,
            "forbidden_terms": list(self.forbidden_terms),
            "note": self.note,
            "limit_ref": self.limit_ref,
            "source_file": self.source_file,
        }


# ---------------------------------------------------------------------------
# 判定行
# ---------------------------------------------------------------------------


@dataclass
class JudgementRow:
    """逐处方 × 逐规则的判定结论。

    业务键 = ``batch_no + prescription_no + rule_id + rule_version``；
    幂等键 = 该业务键的 sha256（spec.md §5）。
    """

    batch_no: str
    prescription_no: str
    rule_id: str
    rule_version: str
    conclusion: Conclusion
    severity: str | None = None
    reason: str = ""
    basis: str = ""
    missing_fields: tuple[str, ...] = ()
    event_fingerprint: str = ""
    state: JudgementState = JudgementState.PENDING
    created_at: str = field(default_factory=now_iso)

    @property
    def business_key(self) -> str:
        """判定业务键（未哈希的明文形式）。"""
        return BUSINESS_KEY_SEP.join(
            (self.batch_no, self.prescription_no, self.rule_id, self.rule_version)
        )

    @property
    def idempotency_key(self) -> str:
        """幂等键 = sha256(业务键)。"""
        return hashlib.sha256(self.business_key.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_no": self.batch_no,
            "prescription_no": self.prescription_no,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "conclusion": self.conclusion.value,
            "severity": self.severity,
            "reason": self.reason,
            "basis": self.basis,
            "missing_fields": list(self.missing_fields),
            "event_fingerprint": self.event_fingerprint,
            "state": self.state.value,
            "created_at": self.created_at,
        }

    def to_version(self, version: int) -> "JudgementVersion":
        """当前结论的历史版本快照（重判留痕用）。

        ``judgements`` 表保存的是**当前**那一版结论；每次重判都往
        ``judgement_versions`` 追加一条新版本，旧版本行不被覆盖、不被删除
        （见 ``engine/statemachine.py``）。
        """
        return JudgementVersion(
            business_key=self.business_key,
            version=version,
            batch_no=self.batch_no,
            prescription_no=self.prescription_no,
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            conclusion=self.conclusion,
            severity=self.severity,
            reason=self.reason,
            basis=self.basis,
            missing_fields=self.missing_fields,
            event_fingerprint=self.event_fingerprint,
            state=self.state,
            created_at=self.created_at,
        )


# ---------------------------------------------------------------------------
# 判定行的历史版本（重判留痕）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgementVersion:
    """判定行的一版结论快照。

    判定表（``judgements``）只留**当前**那一版；重判时旧结论不覆盖、不删除，
    而是以版本号递增追加到版本表（``judgement_versions``）——「这条结论当初
    判成什么、后来为什么改」可逐版回放（飞检与内审语境下的自证需求）。
    """

    business_key: str
    version: int
    batch_no: str
    prescription_no: str
    rule_id: str
    rule_version: str
    conclusion: Conclusion
    severity: str | None = None
    reason: str = ""
    basis: str = ""
    missing_fields: tuple[str, ...] = ()
    event_fingerprint: str = ""
    state: JudgementState = JudgementState.PENDING
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, object]:
        return {
            "business_key": self.business_key,
            "version": self.version,
            "batch_no": self.batch_no,
            "prescription_no": self.prescription_no,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "conclusion": self.conclusion.value,
            "severity": self.severity,
            "reason": self.reason,
            "basis": self.basis,
            "missing_fields": list(self.missing_fields),
            "event_fingerprint": self.event_fingerprint,
            "state": self.state.value,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# 审计行
# ---------------------------------------------------------------------------


class AuditAction(str, Enum):
    """审计事件的动作类型。

    审计表是**事件流水**（不是状态快照）：一条判定判一次、一次出站调用记一次，
    各自留一行，历史行永不覆盖。导出时靠这一列把「判定事件」与「出站调用事件」
    分开——飞检现场要能分开数「审了多少条」与「发出去多少次」。

    * :attr:`JUDGEMENT` —— 一次判定（含重复投递补留的审计行）；
    * :attr:`WRITEBACK` —— 一次 HIS 审方复核回写；
    * :attr:`WITHDRAW` —— 一次已发出的回写的撤回通知；
    * :attr:`PUSH` —— 一次企业微信批次卡片推送；
    * :attr:`REPLAY` —— 一次待重放队列的重新发出。
    """

    JUDGEMENT = "判定"
    WRITEBACK = "回写"
    WITHDRAW = "撤回"
    PUSH = "推送"
    REPLAY = "重放"

    @classmethod
    def from_value(cls, value: object) -> "AuditAction":
        """按中文名或枚举取值解析；空值按「判定」处理。"""
        if isinstance(value, AuditAction):
            return value
        text = str(value or "").strip()
        if not text:
            return cls.JUDGEMENT
        for member in cls:
            if member.value == text or member.name == text:
                return member
        raise ValueError(f"未知的审计动作：{value!r}")


@dataclass
class AuditRow:
    """追加式审计行：每条判定与每次出站调用都留痕，不覆盖、不修改历史。"""

    batch_no: str
    rule_id: str
    rule_version: str
    basis: str
    conclusion: Conclusion
    event_fingerprint: str = ""
    writeback_state: str = ""
    retry_count: int = 0
    prescription_no: str = ""
    action: str = AuditAction.JUDGEMENT.value
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_no": self.batch_no,
            "prescription_no": self.prescription_no,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "basis": self.basis,
            "conclusion": self.conclusion.value,
            "event_fingerprint": self.event_fingerprint,
            "writeback_state": self.writeback_state,
            "retry_count": self.retry_count,
            "action": self.action,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class OutboundCallRecord:
    """一次出站调用的留痕（追加式，不覆盖历史）。

    与 :class:`AuditRow` 的分工：**审计行记「审了什么」**，本行记
    **「发出去什么、发了几次、成没成」**——HIS 审方复核回写、撤回通知、
    企业微信卡片推送、待重放队列重放都落在这里。

    字段口径与审计行对齐：批次号 / 处方号 / 规则号 / 规则版本 / 依据出处 /
    结论 / 事件文件指纹 / 回写状态 / 重试次数，另加通道（:attr:`channel`）、
    幂等键（:attr:`judgement_key`）、错误码与失败详情。

    失败的出站调用**同样留痕**（:attr:`status` = 回写失败 / 推送失败 /
    绑定歧义），并带累计重试次数——「发了几次都没成」是飞检现场必问的一句话，
    不能只留在内存里的异常栈里。
    """

    call_key: str
    channel: str = ""
    action: str = AuditAction.WRITEBACK.value
    judgement_key: str = ""
    batch_no: str = ""
    prescription_no: str = ""
    rule_id: str = ""
    rule_version: str = ""
    conclusion: Conclusion = Conclusion.COMPLIANT
    basis: str = ""
    event_fingerprint: str = ""
    status: str = ""
    retry_count: int = 0
    error_code: str = ""
    detail: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, object]:
        return {
            "call_key": self.call_key,
            "channel": self.channel,
            "action": self.action,
            "judgement_key": self.judgement_key,
            "batch_no": self.batch_no,
            "prescription_no": self.prescription_no,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "conclusion": self.conclusion.value,
            "basis": self.basis,
            "event_fingerprint": self.event_fingerprint,
            "status": self.status,
            "retry_count": self.retry_count,
            "error_code": self.error_code,
            "detail": self.detail,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# 药师确认 / 撤回留痕
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfirmationRecord:
    """一次药师确认或撤回的**追加式**留痕。

    **为什么单独一张表**：判定表存的是「当前那一版」结论与状态，会被重判、
    状态跃迁反复改写；而「谁在什么时候确认了哪一条结论」「谁撤回了它」
    是**飞检与内审要能当场答出**的问题，只能追加、不能覆盖。

    :param action: :data:`med_rule_fieldmap.engine.confirm.ACTION_CONFIRM` /
        :data:`med_rule_fieldmap.engine.confirm.ACTION_WITHDRAW`；
    :param writeback_state: 这条确认/撤回当时的回写状态
        （已确认未回写 / 已回写 / 已撤回 / 撤回待同步）。
    """

    idempotency_key: str
    business_key: str
    batch_no: str = ""
    prescription_no: str = ""
    rule_id: str = ""
    rule_version: str = ""
    action: str = ""
    pharmacist_id: str = ""
    acted_at: str = field(default_factory=now_iso)
    note: str = ""
    writeback_state: str = ""
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, object]:
        return {
            "idempotency_key": self.idempotency_key,
            "business_key": self.business_key,
            "batch_no": self.batch_no,
            "prescription_no": self.prescription_no,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "action": self.action,
            "pharmacist_id": self.pharmacist_id,
            "acted_at": self.acted_at,
            "note": self.note,
            "writeback_state": self.writeback_state,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# 出站回写请求（幂等身份）
# ---------------------------------------------------------------------------


class WritebackStatus(str, Enum):
    """出站回写请求的落库状态。

    * :attr:`CLAIMED` —— 已占位（准备发出，还没拿到响应）；
    * :attr:`SENT` —— 已成功发出并收到响应，**不可再发同一份**；
    * :attr:`FAILED` —— 发出失败（超时 / 5xx），允许按退避策略重试。
    """

    CLAIMED = "已占位"
    SENT = "已回写"
    FAILED = "回写失败"


@dataclass(frozen=True)
class WritebackAttempt:
    """一次出站回写的幂等身份。

    **回写幂等键** = ``sha256(判定幂等键 ｜ 请求体哈希)``（spec.md §5）：

    * 判定幂等键 —— 同一批次同一条处方同一条规则版本，判定侧只落一行；
    * 请求体哈希 —— 请求内容变了（药师改了结论或依据），才允许再次发出。

    合起来保证：同一批次被重投 → 判定行数不变 → 回写请求数不变；
    而判定结论真的改了 → 请求体哈希变 → 新的一条回写照常发出（不误杀）。
    """

    writeback_key: str
    judgement_key: str
    request_hash: str = ""
    status: WritebackStatus = WritebackStatus.CLAIMED
    retry_count: int = 0
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, object]:
        return {
            "writeback_key": self.writeback_key,
            "judgement_key": self.judgement_key,
            "request_hash": self.request_hash,
            "status": self.status.value,
            "retry_count": self.retry_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class PendingWritebackStatus(str, Enum):
    """待重放回写队列的状态。

    * :attr:`PENDING` —— 已入队，等人工或调度器重放；
    * :attr:`REPLAYED` —— 重放完成（发出成功，或已被幂等守卫判为「早已发出」）；
    * :attr:`WITHDRAWN` —— 药师已撤回，这条回写**不再发出**（留痕但不重放）。

    只有 :attr:`PENDING` 的记录会被重放通道取走，因此「重放后不再重复」
    与「撤回后不再发出」都由状态位本身保证，不依赖调用方记得跳过哪几条。
    """

    PENDING = "待重放"
    REPLAYED = "已重放"
    WITHDRAWN = "已撤回"


@dataclass(frozen=True)
class PendingWriteback:
    """一条退避重试超次后落库的待重放回写。

    **为什么要有这张表**：HIS 抖动或超时不能让一条已确认的结论凭空消失，
    也不能把它悄悄标成「已回写」（药师端会以为复核已经记录在案）。
    重试耗尽 → 落这张表 + 产生告警 → 判定行状态**不动** → 等重放。

    字段口径：

    * :attr:`writeback_key` —— 回写幂等键（与 ``writeback_requests`` 表同键），
      重放时靠它被幂等守卫抑制，保证「重放不重复」；
    * :attr:`request_body` —— 规范化的请求体 JSON（键序无关），
      重放时按原样再发一次，不重新拼装（避免重放出来的请求与原请求不是同一份）；
    * :attr:`retry_count` —— 累计重试次数（**不含**首次请求）；
    * :attr:`last_error_code` —— 最后一次错误码（HTTP 状态码或错误分类），
      供运维判断是「HIS 抖动」还是「请求本身有问题」。
    """

    writeback_key: str
    judgement_key: str
    batch_no: str = ""
    prescription_no: str = ""
    rule_id: str = ""
    rule_version: str = ""
    request_body: str = ""
    request_hash: str = ""
    event_fingerprint: str = ""
    retry_count: int = 0
    last_error_code: str = ""
    last_error: str = ""
    status: PendingWritebackStatus = PendingWritebackStatus.PENDING
    queued_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, object]:
        return {
            "writeback_key": self.writeback_key,
            "judgement_key": self.judgement_key,
            "batch_no": self.batch_no,
            "prescription_no": self.prescription_no,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "request_body": self.request_body,
            "request_hash": self.request_hash,
            "event_fingerprint": self.event_fingerprint,
            "retry_count": self.retry_count,
            "last_error_code": self.last_error_code,
            "last_error": self.last_error,
            "status": self.status.value,
            "queued_at": self.queued_at,
            "updated_at": self.updated_at,
        }
