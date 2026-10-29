"""事件文件指纹：给每份落地批次一个稳定的内容指纹。

**对应源产品能力**：源仓库 ``contrib/database/webhooks/models.py`` 的
``TableWebhookCall``（每触发一次就留一条可复核记录，请求/响应副本可导出）与
``core/workflow_actions/handler.py`` 的 ``create_workflow_action()``
（动作实例按序创建，重复触发不产生第二份实例）。

**改写**：源对象是「出站调用记录」，本轮改写为「入站事件身份」——落地文件按
内容 sha256 取指纹；**同一批次号 + 同一指纹**再次到达即判为同一事件的重复投递，
系统只补审计行、不新增判定行（spec.md §5「幂等与并发重试」）。

**为什么指纹按内容而不是按文件名**：HIS 同一批医嘱可能换名重投（导出文件名带
时间戳），也可能同名重投（覆盖写）。按内容取指纹时，两种重投都能识别成同一事件；
按文件名取指纹则只能挡住后者。

**判定幂等键**与文件指纹是两件事，别混：

* **事件指纹** —— 回答「这份落地文件是不是同一份」（sha256 文件字节）；
* **判定幂等键** —— 回答「这条结论是不是同一条」（sha256 业务键，见
  :func:`idempotency_key`）。规则版本变了就是新结论，幂等键随之变。

**指纹的稳定可复现是硬要求**：同一份文件在任何进程、任何时刻算出来的指纹必须
一致，否则重复投递识别失效、台账会凭空多出一份。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..models import BUSINESS_KEY_SEP

#: 指纹算法（spec.md §5 幂等口径：sha256）
FINGERPRINT_ALGORITHM = "sha256"

#: 流式读取分块大小（事件文件可能很大，不整份读进内存）
CHUNK_SIZE = 1 << 20

#: 判定业务键分隔符（与 :data:`models.BUSINESS_KEY_SEP` 同源，勿各写一份）
SEPARATOR = BUSINESS_KEY_SEP


class FingerprintError(RuntimeError):
    """指纹算不出来（文件不存在 / 读不到）。"""


def bytes_fingerprint(data: bytes) -> str:
    """字节串的 sha256 指纹。"""
    return hashlib.sha256(data).hexdigest()


def text_fingerprint(text: str) -> str:
    """文本的 sha256 指纹（按 UTF-8 编码）。"""
    return bytes_fingerprint(text.encode("utf-8"))


def file_fingerprint(path: str | Path) -> str:
    """事件文件的 sha256 指纹（流式读取）。

    文件读不到时抛 :class:`FingerprintError` —— 显式失败，不返回空串，
    否则空指纹会把两次不同的投递误判成同一事件。
    """
    target = Path(path)
    digest = hashlib.sha256()
    try:
        with target.open("rb") as handle:
            while True:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise FingerprintError(f"无法计算事件文件指纹：{target}（{exc}）") from exc
    return digest.hexdigest()


def short_fingerprint(fingerprint: str, length: int = 12) -> str:
    """指纹短显示形（前 12 位）。

    只用于终端提示与日志行，**不作幂等依据**——短指纹必然碰撞。
    """
    if length <= 0:
        raise ValueError("length 必须为正整数")
    return fingerprint[:length]


def business_key(
    batch_no: str,
    prescription_no: str,
    rule_id: str,
    rule_version: str,
) -> str:
    """判定业务键明文：``batch_no｜prescription_no｜rule_id｜rule_version``。

    分隔符用全角竖线，避免与 CSV 的半角逗号、半角竖线混淆。
    """
    return SEPARATOR.join((batch_no, prescription_no, rule_id, rule_version))


def idempotency_key(
    batch_no: str,
    prescription_no: str,
    rule_id: str,
    rule_version: str,
) -> str:
    """判定幂等键 = sha256(业务键)（spec.md §5）。

    与 :attr:`models.JudgementRow.idempotency_key` 同一口径：
    同一批次、同一处方、同一规则、**同一规则版本**才算出同一条；
    规则版本一变（规则改版后重判），幂等键随之变，历史结论不被覆盖。
    """
    return bytes_fingerprint(
        business_key(batch_no, prescription_no, rule_id, rule_version).encode("utf-8")
    )


__all__ = [
    "CHUNK_SIZE",
    "FINGERPRINT_ALGORITHM",
    "SEPARATOR",
    "FingerprintError",
    "business_key",
    "bytes_fingerprint",
    "file_fingerprint",
    "idempotency_key",
    "short_fingerprint",
    "text_fingerprint",
]
