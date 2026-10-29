"""出站通道的运维入口：``python -m med_rule_fieldmap.outbound --check``。

目前提供**两条链路的预检**（凭据位 + 目标地址），**都不发任何请求**——
让信息科在部署当天就能发现环境变量没配，而不是等到第一批回写 / 推送失败才发现。

* ``--check``（默认）—— HIS 审方复核**回写**链路预检；
* ``--check-push`` —— 企业微信**推送**链路预检。

退出码：``0`` 齐备、``2`` 缺凭据或地址非法（**显式失败**，不静默跳过）。
两条链路的退出码**各自独立**：回写链路不可用是**致命**的（它是本产品唯一改变
业务状态的路径），推送链路不可用**只影响递话**（判定与回写照常），因此
``--check-push`` 的结论不与 ``--check`` 混在一起报。
"""

from __future__ import annotations

import sys

from .his_review import main as his_review_main
from .wecom import main as wecom_main


def main(argv: list[str] | None = None) -> int:
    """运维入口：按参数路由到对应链路的预检。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--check-push" in args:
        return wecom_main(["--check"])
    return his_review_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
