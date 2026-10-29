# 命令手册（CLI 全量）

> 本文覆盖本产品**全部**可执行入口与子命令。README 只留最常用的几条速查，
> 完整参数、退出码与边界行为以本文为准。

## 1. 入口一览

本产品是**事件驱动守护进程**，不是 HTTP 服务；没有对外端口，也没有前端界面。
三个入口分工明确：

| 入口 | 角色 | 常驻？ | 什么时候用 |
|---|---|---|---|
| `python -m med_rule_fieldmap.daemon` | **主路径**：监听医嘱落地目录，事件到达即判定 → 落盘 → 推送 → 回写 | 是 | 药学部内网单机部署后常驻运行 |
| `python -m med_rule_fieldmap.cli <子命令>` | **运维通道**：单跑一批判定、导出台账、查状态、药师确认 / 撤回 | 否 | 排障、导出、药师复核确认 |
| `python -m med_rule_fieldmap.outbound --check` | **部署预检**：只核对凭据位与目标地址，**不发任何请求** | 否 | 部署当天先跑一次 |

```bash
# 三条入口的顶层帮助
python -m med_rule_fieldmap.daemon --help
python -m med_rule_fieldmap.cli --help
python -m med_rule_fieldmap.outbound --help
```

## 2. 退出码（全产品统一）

| 退出码 | 含义 | 什么时候出现 |
|---|---|---|
| `0` | 成功 | 正常完成；`--json` 模式下载荷打到 stdout |
| `2` | **显式失败**（配置错误 / 参数非法 / 凭据缺失 / 结论过滤值非法 / 审计缺依据） | 命令自己判定「不该静默跳过」的场景，一律带原因文本 |
| `3` | 守护进程**已在运行**（状态库单实例锁未拿到） | 第二个守护实例指向同一个状态库；错误信息点名占用进程的 pid / 主机 / 启动时间 / 命令行 |

> 退出码 `2` 一律**不静默跳过**：不会有「跑完了但其实什么都没做」的情况。

## 3. 全局约定

| 约定 | 说明 |
|---|---|
| 目录默认相对当前工作目录 | `--inbox` 缺省 `inbox`，`--out` 缺省 `out`，`--rules` 缺省 `rules` |
| 状态库默认不落库 | 大多数只读命令**必须**显式给 `--db`；台账与审计的权威源是状态库，不是落地文件 |
| `--json` | 所有只读 / 导出类子命令支持，输出 JSON 载荷到 stdout，便于脚本消费 |
| 凭据位 | `HIS_BASE_URL` / `HIS_API_TOKEN` / `WECOM_WEBHOOK_URL` 全部读环境变量，程序内无硬编码 |
| 颜色 | 守护进程默认上色；重定向到文件时加 `--no-color` |
| 规则生效基准日 | `--as-of YYYY-MM-DD`；缺省取当天，用于按 `effective_from` / `effective_to` 过滤生效集 |

---

## 命令详解

每条命令给出：用途 → 参数表 → 可复制的调用示例 → 边界行为。

### 4. 守护进程 `python -m med_rule_fieldmap.daemon`

**主路径**。监听 `inbox/`，文件落地即切批次 → 三态判定 → 落台账 → 推企业微信卡片 →
（药师确认后）回写 HIS 审方复核接口。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--inbox DIR` | `inbox` | HIS 医嘱落地目录（**只读**，不改落地文件） |
| `--out DIR` | `out` | 台账与导出产物目录 |
| `--rules DIR` | `rules` | 规则行目录；按文件 mtime 热重载 |
| `--state-db PATH` | `state/mrfm.sqlite3` | 状态库（SQLite，WAL）；断点续跑与幂等都靠它 |
| `--heartbeat-sec N` | `30` | 心跳行打印间隔（秒） |
| `--log-level LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` |
| `--no-color` | 关 | 日志不上色（重定向到文件时用） |

```bash
python -m med_rule_fieldmap.daemon \
  --inbox /srv/his/orders --rules rules/sample --out out \
  --state-db state/mrfm.sqlite3 --heartbeat-sec 30
```

**启动前置条件**：`HIS_BASE_URL` **必须**已配置——回写是本产品唯一改变业务状态的路径，
缺失时守护进程**显式失败并退出**（不静默跳过）：

```text
[alert] [his_review] HIS_BASE_URL 未配置：HIS 审方复核回写是本产品唯一改变业务状态的路径，缺失时必须显式失败并退出，不得静默跳过。
[daemon] 启动失败：HIS_BASE_URL 未配置：…
```

`WECOM_WEBHOOK_URL` 未配置**不影响**启动：推送只递话、不改业务状态，缺失时告警、判定与回写照常。

**单实例锁**：同一个状态库只允许一个守护进程。第二个实例退出码 `3`，并打印占用者信息。

**优雅退出**：`SIGTERM` / `SIGINT` → 完成当前批次后退出，不留半批状态。

---

### 5. `cli judge` — 跑一批判定

对落地目录里的医嘱批次跑三态判定，产出判定台账与字段缺口台账。**运维通道**，不常驻。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--inbox DIR` | `inbox` | 医嘱落地目录（只读） |
| `--out DIR` | `out` | 产物输出目录 |
| `--rules DIR` | `rules` | 规则行目录 |
| `--as-of YYYY-MM-DD` | 当天 | 规则生效区间判定基准日 |
| `--state-db PATH` | 无 | 给了就落库：按事件指纹去重（重复投递只补审计、不新增判定），并写审计台账 |
| `--json` | 关 | 以 JSON 输出批次摘要 |

```bash
# 单跑一批，不落库
python -m med_rule_fieldmap.cli judge --inbox inbox --rules rules/sample --out out

# 带状态库跑一遍：同时落审计台账（判定事件行数 == 判定行数）
python -m med_rule_fieldmap.cli judge --inbox inbox --rules rules/sample \
    --out out --state-db state/mrfm.sqlite3
```

**产物**：`out/judgement_ledger.csv`、`out/undeterminable_ledger.csv`、
`out/field_gap_priority.csv`；给了 `--state-db` 还会落 `out/audit_ledger.csv`。

**边界**：文件截断 / 乱码 / 必需列缺失 → **整文件挂起**（记入挂起台账并告警），
**绝不做部分判定**——半截判定比不判定更危险。

---

### 6. `cli scan` — 扫描落地目录

只做「写完的文件切批次、写坏的文件整文件挂起」，不判定。用于部署当天试跑与排障。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--inbox DIR` | `inbox` | 医嘱落地目录（只读） |
| `--out DIR` | `out` | 挂起台账落在这里 |
| `--wait-sec N` | `5` | 等待文件写完的秒数 |
| `--settle-checks N` | `2` | 连续几次观察大小 / mtime 不变才算写完 |
| `--release FILE_NAME` | 无 | **人工解除**某份挂起文件（解除后该文件可重投）；执行后退出 |
| `--json` | 关 | 以 JSON 输出扫描结果 |

```bash
python -m med_rule_fieldmap.cli scan --inbox inbox --out out --wait-sec 3

# 挂起文件修好后人工解除（历史挂起行不改写，追加一条「已解除」）
python -m med_rule_fieldmap.cli scan --inbox inbox --out out --release demo_batch_001.csv
```

**挂起原因分五类**（处置动作不同）：空文件 / 编码不可识别 / 缺少表头 / 文件截断 / 缺少必需列。

---

### 7. `cli state` — 查状态库

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--db PATH` | **必填** | 状态库路径（SQLite） |
| `--batch BATCH_NO` | 无 | 只看某个批次号 |
| `--json` | 关 | 以 JSON 输出状态快照 |

```bash
python -m med_rule_fieldmap.cli state --db state/mrfm.sqlite3
python -m med_rule_fieldmap.cli state --db state/mrfm.sqlite3 --batch DEMO-BATCH-001 --json
```

输出含：批次个数、每批的处方行数 / 判定行数 / 审计行数 / 状态 / 事件指纹、重复投递标记。

---

### 8. `cli status` — 守护状态与运行摘要

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--db PATH` | **必填** | 状态库路径（SQLite） |
| `--json` | 关 | 以 JSON 输出状态快照 |

```bash
python -m med_rule_fieldmap.cli status --db state/mrfm.sqlite3
python -m med_rule_fieldmap.cli status --db state/mrfm.sqlite3 --json
```

输出含：守护进程是否在跑、批次 / 三态累计计数、待药师复核条数、回写数与失败数、推送次数、平均耗时。
这些数字**落库**，进程重启后仍在——运维看的是累计事实，不是内存里的瞬时值。

---

### 9. `cli batch replay` / `cli replay` — 判定证据链回放

把某个批次的每条结论回放到「批次 → 规则号 → 规则版本 → 依据出处 → 事件文件指纹 → 当前状态」
的完整链条上，用于飞检 / 内审自证。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--db PATH` | **必填** | 状态库路径 |
| `--batch BATCH_NO` | **必填** | 要回放的批次号 |
| `--out DIR` | `out` | 回放件输出目录 |
| `--rules DIR` | 无 | 给了就补出「严重度来源」与「点评分级」两列 |
| `--rule RULE_ID` | 无 | 只看某条规则（**可重复给多个**） |
| `--conclusion 结论` | 无 | 只看某类结论：合规 / 违规 / 不可判定（**可重复给多个**） |
| `--json` | 关 | 以 JSON 输出回放摘要 |

```bash
python -m med_rule_fieldmap.cli batch replay --db state/mrfm.sqlite3 \
    --batch DEMO-BATCH-001 --out out --rules rules/sample

# 只看违规结论里的两条规则
python -m med_rule_fieldmap.cli batch replay --db state/mrfm.sqlite3 \
    --batch DEMO-BATCH-001 --rule RX-LEGAL-MA-OUT-DAYS --rule RX-NORM-USAGE-VAGUE \
    --conclusion 违规 --json
```

> `replay` 是 `batch replay` 的顶层简写，两条命令等价。

---

### 10. `cli rules validate` — 规则行载入校验

校验规则行：必填项、依据出处、表达式语法与能力禁用、生效区间。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--rules DIR` | `rules` | 规则行目录 |
| `--as-of YYYY-MM-DD` | 当天 | 规则生效区间判定基准日 |
| `--json` | 关 | 以 JSON 输出校验结果 |

```bash
python -m med_rule_fieldmap.cli rules validate --rules rules/sample
```

**边界**：有非法规则行时**退出码 `2`**，并逐条打印字段名与行列定位（如「第 1 行第 1 列」）；
**其余规则照常生效**——一条写错的规则不拖垮整批审方。规则行缺 `basis`（依据出处）一律拒绝载入。

> `rule` 是 `rules` 的简写别名，`rule validate` / `rule health` 等价。

---

### 11. `cli rules health` — 规则集体检

体检三件事：**僵尸规则**（从没被触发过，多半适用条件写窄了或字段名写错了）、
**恒不可判定**（依赖的字段本批始终拿不到）、**覆盖率**（分母是本批该管多少张处方）。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--rules DIR` | `rules` | 规则行目录 |
| `--db PATH` | 无 | 读判定台账，算命中率与恒不可判定 |
| `--inbox DIR` | 无 | 覆盖率分母的来源：这条规则本批该管多少张处方 |
| `--out DIR` | `out` | 体检表落在这里 |
| `--json` | 关 | 以 JSON 输出体检结论 |

```bash
python -m med_rule_fieldmap.cli rules health --rules rules/sample \
    --db state/mrfm.sqlite3 --inbox inbox --out out
```

**产物**：`out/rule_health.csv`（列：规则号 / 版本 / 层次 / 适用处方数 / 判出结论数 /
不可判定处方数 / 覆盖率 / 健康度 / 是否僵尸 / 是否恒不可判定 / 卡住的字段 / 原因）。

---

### 12. `cli ledger export` — 导出台账

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--db PATH` | **必填** | 状态库路径 |
| `--out DIR` | `out` | 导出目录 |
| `--batch BATCH_NO` | 无 | 只导某个批次 |
| `--conclusion 结论` | 无 | 只导某类结论：合规 / 违规 / 不可判定（**可重复给多个**） |
| `--inbox DIR` | 无 | 给了就连**不可判定台账**与**字段补齐顺序表**一起导 |
| `--json` | 关 | 以 JSON 输出导出结果 |

```bash
# 三份台账一起导
python -m med_rule_fieldmap.cli ledger export --db state/mrfm.sqlite3 --inbox inbox --out out

# 只导违规结论给药师逐条看
python -m med_rule_fieldmap.cli ledger export --db state/mrfm.sqlite3 --out out --conclusion 违规
```

**边界**：`--conclusion` 取值非法 → **退出码 `2`** 并列出合法取值，不静默导出一份空表。
`--inbox` 是不可判定台账与字段补齐顺序表的**必要条件**——缺字段的成因（接口未回传 /
医嘱未填写）要靠落地文件才判得出。

---

### 13. `cli audit export` — 导出审计台账

飞检与内审语境下的自证材料：每条判定与每次出站调用都留痕，**追加式**写入，不覆盖历史。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--db PATH` | **必填** | 状态库路径 |
| `--out DIR` | `out` | 导出目录 |
| `--batch BATCH_NO` | 无 | 只导某个批次 |
| `--json` | 关 | 以 JSON 输出导出结果 |

```bash
python -m med_rule_fieldmap.cli audit export --db state/mrfm.sqlite3 --out out
```

**边界**：任一行缺**依据出处** → 显式报错并指出是哪几行，**不落盘**——
一条结论说不出依据，审计台账等于没有台账。

---

### 14. `cli confirm` / `cli revoke` — 药师确认 / 撤回

**回写 HIS 的硬门禁**：只有「药师已确认」状态的结论才可能出现在回写请求体里；
「已判定」与「待补字段」永不自动回写。`revoke` 是 `confirm --withdraw` 的简写。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--db PATH` | **必填** | 状态库路径 |
| `--key KEY` | 无 | 判定标识：**判定幂等键**（sha256）或**业务键**（`batch_no｜prescription_no｜rule_id｜rule_version`） |
| `--by PHARMACIST_ID` | 无 | 药师标识（`reviewed_by` / `withdrawn_by`）；**系统不代签** |
| `--note NOTE` | 空 | 确认 / 撤回备注（写进留痕） |
| `--trigger` | 关 | 触发回写：把已确认的结论发到 HIS（需 HIS 凭据，缺则显式失败） |
| `--batch BATCH_NO` | 无 | 只看某个批次的判定行 |
| `--limit N` | 全部 | 本次最多处理几条 |
| `--list` | 关 | 列出待回写候选与确认 / 撤回留痕 |
| `--resync` | 关 | 重投未同步成功的**撤回通知**（需 HIS 凭据） |
| `--json` | 关 | 以 JSON 输出结果 |
| `--withdraw` | 关 | 撤回而不是确认（排队中的回写作废；已发出的同步撤回通知到 HIS） |

```bash
# 药师确认一条结论（只确认，不发请求）
python -m med_rule_fieldmap.cli confirm --db state/mrfm.sqlite3 \
    --key <判定幂等键> --by <药师标识> --note "已复核，同意退回医师"

# 确认并触发回写 HIS
python -m med_rule_fieldmap.cli confirm --db state/mrfm.sqlite3 \
    --key <判定幂等键> --by <药师标识> --trigger

# 撤回一条已确认的结论
python -m med_rule_fieldmap.cli revoke --db state/mrfm.sqlite3 \
    --key <判定幂等键> --by <药师标识> --note "误判"

# 看待回写候选与确认 / 撤回留痕
python -m med_rule_fieldmap.cli confirm --db state/mrfm.sqlite3 --list
```

**边界**：
- 还没发出去的结论被撤回 → **不发任何请求**（排队中的回写直接作废）。
- 已经发出去的才走 `POST {HIS_BASE_URL}/api/v1/prescription/review/withdraw`；
  同步失败会明确记为「**撤回待同步**」并告警，**绝不假装两边一致**，用 `--resync` 重投。
- 撤回通知与复核请求**不是同一个接口**：前者说「刚才那条复核记录不算数」，
  后者说「这条结论判成什么」。因此撤回通知**不进复核用的待重放队列**，改由 `--resync` 单独重投。

---

### 15. `cli writeback` — 待重放回写队列

HIS 抖动时的兜底通道：出站失败退避重试超次后，落 `pending_writeback` 队列排队。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--db PATH` | **必填** | 状态库路径 |
| `--out DIR` | `out` | 导出目录 |
| `--replay` | 关 | 把待重放的回写按原样再发一次（需 HIS 凭据；**幂等键保证不重复**） |
| `--limit N` | 全部 | 最多重放几条 |
| `--json` | 关 | 以 JSON 输出队列快照 |

```bash
python -m med_rule_fieldmap.cli writeback --db state/mrfm.sqlite3 --out out
python -m med_rule_fieldmap.cli writeback --db state/mrfm.sqlite3 --replay --limit 10
```

**产物**：`out/pending_writeback.csv`（含请求体原文、请求哈希、重试次数、最后错误码与错误文本）。

---

### 16. `python -m med_rule_fieldmap.outbound --check` — 出站链路预检

**只核对凭据位与目标地址，不发出任何请求**。让信息科在部署当天就能发现环境变量没配，
而不是等到第一批回写 / 推送失败才发现。

| 参数 | 说明 |
|---|---|
| `--check`（默认） | HIS 审方复核**回写**链路预检 |
| `--check-push` | 企业微信**推送**链路预检（路由到推送侧的预检入口） |

```bash
python -m med_rule_fieldmap.outbound --check
python -m med_rule_fieldmap.outbound --check-push
```

**退出码**：`0` 齐备；`2` 缺凭据或地址非法（**显式失败**，不静默跳过）。
两条链路的退出码**各自独立**：回写链路不可用是**致命**的（它是唯一改变业务状态的路径），
推送链路不可用**只影响递话**（判定与回写照常）。

缺凭据时的输出：

```text
预检未通过：HIS 审方复核回写不可用（显式失败，不静默跳过）。
[缺失] HIS_BASE_URL 未配置 → 回写不可用（必须显式失败）
[缺失] HIS_API_TOKEN 未配置 → 拒绝发出请求
```

---

## 17. 子命令速查表

| 命令 | 用途 | 关键参数 | `--db` 必填 |
|---|---|---|---|
| `daemon` | 常驻守护：监听 → 判定 → 落盘 → 推送 → 回写 | `--inbox` `--rules` `--out` `--state-db` `--heartbeat-sec` `--no-color` | 否（`--state-db`） |
| `cli judge` | 单跑一批判定并写出台账 | `--inbox` `--rules` `--out` `--as-of` `--state-db` | 否（`--state-db`） |
| `cli scan` | 切批次 / 挂起坏文件 / 人工解除 | `--inbox` `--out` `--wait-sec` `--settle-checks` `--release` | 否 |
| `cli state` | 查状态库：批次 / 判定 / 审计 / 重复投递 | `--batch` | 是 |
| `cli status` | 守护状态与运行摘要 | — | 是 |
| `cli batch replay` / `cli replay` | 判定证据链回放 | `--batch` `--rules` `--rule` `--conclusion` | 是 |
| `cli rules validate` | 规则行载入校验（有非法行退出码 2） | `--rules` `--as-of` | 否 |
| `cli rules health` | 规则集体检：僵尸 / 恒不可判定 / 覆盖率 | `--rules` `--db` `--inbox` `--out` | 否 |
| `cli ledger export` | 导出台账（含不可判定台账与字段补齐顺序表） | `--out` `--batch` `--conclusion` `--inbox` | 是 |
| `cli audit export` | 导出审计台账（缺依据不落盘） | `--out` `--batch` | 是 |
| `cli confirm` | 药师确认 / 撤回 / 触发回写 | `--key` `--by` `--note` `--trigger` `--list` `--resync` `--withdraw` | 是 |
| `cli revoke` | 撤回一条已确认的结论（= `confirm --withdraw`） | `--key` `--by` `--note` | 是 |
| `cli writeback` | 查看 / 重放待重放回写队列 | `--out` `--replay` `--limit` | 是 |
| `outbound --check` | 回写链路预检（不发请求） | `--check` | — |
| `outbound --check-push` | 推送链路预检（不发请求） | `--check-push` | — |

> 相关文档：架构、状态机、审计链与幂等键见 [`architecture.md`](architecture.md)；
> 输出样例与终端回显见 [`examples.md`](examples.md)；数据来源边界见 [`data-sources.md`](data-sources.md)。
