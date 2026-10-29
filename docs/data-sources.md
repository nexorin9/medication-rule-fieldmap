# 数据来源与边界

> 这份文档回答一个问题：**读代码的人看到的那几份数据，各是什么身份？**
> 特别要说清一件事——**公开数据集只是测试数据层，不是本产品的对接边界**。

## 1. 三层划分

| 层 | 内容 | 是产品边界吗 | 落在哪 |
|---|---|---|---|
| **① 正式对接规范** | 入站 CSV 字段规范 + HL7 v2 `ORM^O01` 段字段映射 + HIS 审方复核回写请求体规范 + 撤回通知请求体规范 + 企业微信推送 body + 凭据位 | ✅ **是最终产品边界** | `spec.md` §2.1 / §2.2 / §2.3 / §2.4；本文第 2 节摘要 |
| **② 测试数据层** | 脱敏样例医嘱、确定性种子合成规模集、官方案例回归集、本地 HTTP 替身 | ❌ 仅验证用，**不是**对接方式 | `tests/data/`、`out/_scale_dataset/`；本文第 3 节 |
| **③ 公开数据集对照** | MIMIC-IV Demo v2.2 的 `prescriptions` 子集 | ❌ 对照用 | `data/mimic_demo/`、`tests/data/mimic_demo_snapshot/`；本文第 4 节 |

**一句话**：产品边界是**协议与字段规范**，不是仓库里任何一份 CSV。
仓库里的数据文件随时可以被换掉，协议与字段规范不会。

## 2. ① 正式对接规范（产品边界）

### 2.1 入站（读）：HIS 医嘱落地目录（read-only）

目录 `inbox/` 下的批次文件，两种格式**等价**，由文件后缀区分：

| 格式 | 关键口径 |
|---|---|
| **CSV**（主格式） | 首行表头；列序**不敏感**（按表头名映射，另有中文别名表归一）；UTF-8 优先，GBK 兜底解码；数值列容忍单位后缀（`3日量` 取 3） |
| **HL7 v2 `ORM^O01`** | 段分隔回车、字段分隔竖线、子组件脱字符、重复符波浪号；取 `MSH-9` `MSH-10`→`batch_no` `PID-3` `PV1-1` `PV1-7` `ORC-2` `ORC-7` `ORC-9` `OMG-4` `RXO-1/-2/-4/-11` `RXE-4/-10/-11` |

**CSV 字段全集**（按表头名映射，缺列不挂起文件、只落空值交判定层处理）：

```text
batch_no  order_no  prescription_no  visit_no  patient_ref
dept_code  dept_name  prescriber_id  prescriber_name
drug_code  drug_name  spec  dosage_form
dosage_text  dosage_value  dosage_unit  route  freq  quantity
rx_type  drug_category  days_qty  extend_reason  skin_test_result
clinical_diag  age_value  age_unit  event_time
```

★ 常缺列：`rx_type` / `drug_category` / `days_qty` / `extend_reason` / `skin_test_result`。
缺这些列**不挂起文件**，而是让相关规则落「不可判定」并点名该字段——
这正是字段缺口台账要暴露的对象。

**完整性规则**：文件截断 / 乱码 / 缺少表头 / 空文件 / 缺少必需列 → **整文件挂起**
（`out/suspended_ledger.csv`），**绝不做部分判定**。

### 2.2 出站（写）：HIS 审方复核回写

`POST {HIS_BASE_URL}/api/v1/prescription/review`，请求头
`Authorization: Bearer $HIS_API_TOKEN`、`Content-Type: application/json`。

请求体字段**严格 11 个**：

```json
{ "batch_no": "<批次号>", "prescription_no": "<处方号>",
  "rule_id": "<规则号>", "rule_version": "<规则版本>",
  "conclusion": "violation", "severity": "<严重度>",
  "reason": "<归因文本>", "basis": "<依据出处>",
  "missing_fields": [], "reviewed_by": "<药师标识>", "reviewed_at": "<ISO8601>" }
```

- `conclusion` 取 `compliant` / `violation` / `undeterminable` 三个英文值。
- 事件文件指纹**只留在本地审计**，不进请求体。
- 响应 `{"code": 0, "data": {"prescription_no": "<处方号>"}}`；
  返回的 `prescription_no` 与请求不一致 → 判为**实体绑定歧义**，进挂起队列并告警，**不落库**。

**撤回通知**走**另一个接口** `POST {HIS_BASE_URL}/api/v1/prescription/review/withdraw`，
字段**严格 10 个**：`batch_no` `prescription_no` `rule_id` `rule_version` `conclusion`
`basis` `reviewed_by` `withdrawn_by` `withdrawn_at` `reason`。
两者语义不同：复核请求说「这条结论判成什么」，撤回请求说「刚才那条复核记录不算数」。

### 2.3 出站（写）：企业微信推送

`POST $WECOM_WEBHOOK_URL`，body 固定 `{"msgtype":"markdown","markdown":{"content":"…"}}`。
正文三段：批次摘要 / 不可判定条目摘要 / 字段补齐顺序摘要。
推送失败**不影响**判定与回写，只告警；地址缺失或非法则**请求一个字节都不发**。

### 2.4 凭据位

全部读环境变量，程序内**无硬编码地址与密钥**：

| 环境变量 | 用途 |
|---|---|
| `HIS_BASE_URL` | 回写基址；未配置则守护进程显式失败并退出 |
| `HIS_API_TOKEN` | 回写 Bearer 令牌 |
| `WECOM_WEBHOOK_URL` | 群机器人地址（URL 自带凭据，告警与日志一律隐去 `key`） |
| `MIMIC_PHYSIONET_USERNAME` / `MIMIC_PHYSIONET_PASSWORD` | 仅第 4 节的公开数据集下载脚本用，**与产品运行无关** |

仓内 `.env.example` 只有空占位；`deploy/` 的 systemd 单元与 compose 文件都从**仓外**
`EnvironmentFile` / `env_file` 注入，仓库里没有任何真实密钥。

## 3. ② 测试数据层（仅验证用）

| 数据 | 位置 | 生成方式 | 用途 | 是产品边界吗 |
|---|---|---|---|---|
| 脱敏样例医嘱批次（CSV） | `tests/data/batch/`、`tests/data/child_batch/`、`tests/data/duplicate_batch/` | 手写**脱敏**样例 | 端到端判定、三态分流、字段缺口聚合 | ❌ |
| HL7 样例消息 | `tests/data/hl7_batch/` | 手写脱敏样例 | CSV / HL7 两种格式字段等价性 | ❌ |
| 官方案例回归集 | `domain/cases/prescription_review_cases.json`（规则来源） | 领域权威案例集 | 逐条回归 + 新增第三态案例 | ❌ |
| 确定性种子合成规模集 | `out/_scale_dataset/`（由 `scripts/gen_synthetic_orders.py` 生成） | 固定种子 `20261028`，逐字节可复现；覆盖生僻字 / GBK 混编 / 空值 / 脏行 / 超长字段 | 规模化回归（2000 行 × 19 条规则）、三态 vs 二元基线对比 | ❌ |
| 本地 HTTP 替身 | 测试内联 | 假 HIS / 假企业微信服务器 | 断言请求方法、路径、`Authorization` 头与 JSON 字段全集 | ❌ **替身不是对接方式** |

**红线**：
- **不接真实患者明细**。仓内所有数据都是脱敏样例或确定性合成数据。
- **不伪造「来自真实医院」**。样例批次里的处方号、批次号、指纹都是构造值，不是任何一家医院的真实标识。
- **替身不得被写成最终产品边界**：正式对接方式以本文第 2 节的协议与字段规范为准，
  测试里的本地 HTTP 服务器只是断言工具。

## 4. ③ 公开数据集对照（MIMIC-IV Demo · 仅对照）

真实院内 HIS 医嘱流本轮不可得，按数据来源阶梯取公开数据集做**对照**：
MIMIC-IV Demo v2.2 的 `prescriptions` 子集（MIT Laboratory for Computational Physiology
维护，PhysioNet Credentialed Access）。

| 事项 | 口径 |
|---|---|
| 获取方式 | `python3 scripts/fetch_mimic_demo.py`（可重跑：已有合法文件直接复用；`--force` 强制重下） |
| 完整性校验 | gzip 可解 + 体积下限 + 记录数下限 + 必需列齐全（`hadm_id` / `drug` / `dose_val_rx` / `route`） |
| 退出码 | `0` 下载成功并通过校验；`2` 降级到仓内结构快照（网络不可用 / 缺凭据 / HTTP 错误）并打印告警；`3` 校验不通过（硬失败，**不降级**） |
| 降级落点 | `tests/data/mimic_demo_snapshot/`（手写**结构快照**，非 MIMIC 真实数据，`order_source=SYNTHETIC` 标记） |
| **用途边界** | **只作对照与测试数据层**（字段形态对照、回归基线），**不是本产品对接边界**；正式对接口径仍是第 2 节 |
| 覆盖盲区 | MIMIC-IV Demo 不含麻精类别、皮试结果、处方类型与医师处方权——恰好覆盖本轮「不可判定」路径的触发条件 |
| 许可 | 本仓库**不转发、不再分发**该数据集本身，只保留下载脚本与结构快照 |

> **许可与再分发**：MIMIC-IV 受 PhysioNet Credentialed Health Data License 约束，
> 因此仓库内**只保留下载脚本 + 手写结构快照**，不含任何真实数据文件。

## 5. 三层之外：一律不接

| 不接的东西 | 原因 |
|---|---|
| 真实患者姓名、身份证号、手机号 | 归因文本与台账只允许出现脱敏 `patient_ref`；`engine/evidence.py` 有强制掩码与外泄检测 |
| 未经授权的院内系统直连 | 入站只读落地目录，出站只打两个已登记的接口；无凭据即显式失败 |
| 模型生成的数据 | `ai_role=none`：判定链全部确定性求值，不设模型网关，也不接受「模型补出来的」字段值 |

## 6. 怎么核对本文

| 想核对什么 | 看哪 |
|---|---|
| 入站字段到底要哪些 | 本文 §2.1 与 `spec.md` §2.1 |
| 回写请求体字段集 | 本文 §2.2 与 `spec.md` §2.3 |
| 哪份数据是脱敏样例 | 本文 §3 与 `tests/data/` |
| 公开数据集从哪来、能不能转发 | 本文 §4 与 `scripts/fetch_mimic_demo.py` |
| 凭据从哪读、缺了会怎样 | 本文 §2.4 与 [`cli.md`](cli.md) §16 |

## 7. 相关文档

| 文档 | 内容 |
|---|---|
| [`../README.md`](../README.md) | 产品说明：适用场景、快速开始、典型场景、验收自检 |
| [`cli.md`](cli.md) | 命令手册：全部入口与子命令、参数、退出码 |
| [`examples.md`](examples.md) | 输出样例与终端回显（真实运行结果） |
| [`architecture.md`](architecture.md) | 架构手册：模块分层、状态机、幂等键体系、审计链 |
