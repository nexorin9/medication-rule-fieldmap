# 输出样例与终端回显

> 本文全部内容来自**真实运行**（仓库内脱敏样例批次 `tests/data/batch/demo_batch_001.csv`，
> 26 行医嘱 × 19 条生效规则）。路径、行号、耗时随环境不同会变，属正常。
> 命令与参数说明见 [`cli.md`](cli.md)，模块与数据流见 [`architecture.md`](architecture.md)。

## 0. 一览

| 产物 | 路径 | 谁写 |
|---|---|---|
| 判定台账 | `out/judgement_ledger.csv` | `ledger/ledger.py` |
| 不可判定台账 | `out/undeterminable_ledger.csv` | `ledger/gap.py` |
| 字段补齐顺序表 | `out/field_gap_priority.csv` | `ledger/priority.py` |
| 规则体检表 | `out/rule_health.csv` | `ledger/rule_health.py` |
| 审计台账 | `out/audit_ledger.csv` | `ledger/audit_export.py` |
| 挂起台账 | `out/suspended_ledger.csv` | `ingest/suspend.py` |
| 待重放回写队列 | `out/pending_writeback.csv` | `outbound/retry_queue.py` |
| 实体绑定歧义台账 | `out/binding_ambiguity_ledger.csv` | `outbound/his_review.py` |
| 证据链回放件 | `out/replay_<批次号>.csv` | `ledger/evidence_replay.py` |
| 规模与基线报告 | `out/scale_report.json`、`out/baseline_compare.json` | `scripts/bench_scale.py` |

---

## 样例

以下每一节都是**真实运行**的输出（脱敏样例批次 `tests/data/batch/demo_batch_001.csv`，26 行医嘱 × 19 条生效规则）。

### 1. 规则行载入校验

```bash
python -m med_rule_fieldmap.cli rules validate --rules rules/sample
```

```text
[rules validate] 规则目录 rules/sample：扫描 3 个文件 · 载入 19 条 · 生效 19 条 · 被拒 0 条
[rules validate] 全部规则行校验通过
```

**非法规则行时退出码 `2`**，逐条打印字段名与行列定位，**其余规则照常生效**：

```text
[rules validate] 规则目录 rules/sample：扫描 3 个文件 · 载入 19 条 · 生效 18 条 · 被拒 1 条
[rules validate] 规则 RX-DEMO-BAD 校验未通过：缺少必填字段 basis（规则行必须带依据出处，无依据不得生效）· 第 1 行第 1 列
```

---

### 2. 端到端判定（运维通道）

```bash
python -m med_rule_fieldmap.cli judge --inbox tests/data/batch --rules rules/sample \
    --out out --state-db state/mrfm.sqlite3
```

```text
[judge] 批次 DEMO-BATCH-001（来源 demo_batch_001.csv）：收到 26 行 · 命中规则 192 条 · 合规 170 · 违规 11 · 不可判定 11 · 判定台账 → out/judgement_ledger.csv · 新增判定 192 条 · 事件指纹 be49b10881c3
[judge] 运行摘要：批次 1 · 医嘱 26 行 · 判定 192 条（合规 170 · 违规 11 · 不可判定 11） · 回写 0 条 · 表达式解析缓存：命中 51 · 未命中 15 · 解析 15 次 · 淘汰 0 · 失效 0 · 命中率 77.3% · 存量 15/512
[judge] 不可判定台账 → out/undeterminable_ledger.csv（11 组字段缺口 · 5 个字段 · 缺口最多：days_qty 1 张 · dosage_text 1 张 · drug_category 1 张）
[judge] 字段补齐顺序表 → out/field_gap_priority.csv（5 个字段 · 先补：1) drug_category 1 张 · 2) rx_type 1 张 · 3) skin_test_result 1 张 · 其中要动接口 0 个）
[judge] 审计台账 → out/audit_ledger.csv（判定事件 192 条 · 出站调用 0 条 · 依据缺失 0 条）
```

> 「其中要动接口 0 个」= 本批的缺口全部是**医嘱未填写**（退回医师补写），
> 不是接口没回传——两种成因的处置方不同，见下文第 4 节。

---

### 3. 判定台账 `out/judgement_ledger.csv`

逐处方 × 逐规则一行，结论三态，附归因文本与依据出处。

列：`batch_no,prescription_no,rule_id,rule_version,conclusion,severity,reason,basis,missing_fields`

```csv
batch_no,prescription_no,rule_id,rule_version,conclusion,severity,reason,basis,missing_fields
DEMO-BATCH-001,RX-0001,RX-APPR-DOSE-NONPOSITIVE,2026.10,合规,用药错误,规则 RX-APPR-DOSE-NONPOSITIVE（版本 2026.10）判定：合规。判定条件「dosage_value <= 0」未命中。依据：处方审核规则体系 §1 适宜性审核第 3 项（剂量、用法的正确性）,处方审核规则体系 §1 适宜性审核第 3 项（剂量、用法的正确性）,
DEMO-BATCH-001,RX-0001,RX-LEGAL-MA-OUT-DAYS,2026.10,合规,严重不合理用药,规则 RX-LEGAL-MA-OUT-DAYS（版本 2026.10）判定：合规。开具日量 1 未超过该档上限「1 次常用量」。判定条件「开具日量超过 1 次常用量上限」未命中。依据：处方审核规则体系 §3 特殊药品处方量限（门（急）诊一般患者：麻醉药品注射剂每张处方 1 次常用量）,处方审核规则体系 §3 特殊药品处方量限（门（急）诊一般患者：麻醉药品注射剂每张处方 1 次常用量）,
```

**违规行**（归因点名命中原因）：

```text
规则 RX-NORM-USAGE-VAGUE（版本 2026.10）判定：违规。用法用量表述命中含糊字句「遵医嘱」，退回复核并请医师改写为具体用法用量。依据：处方审核规则体系 §1 规范性审核（不得使用“遵医嘱”“自用”等含糊字句）
```

**不可判定行**（归因点名缺失字段）：

```text
规则 RX-LEGAL-MA-OUT-DAYS（版本 2026.10）判定：不可判定。缺少字段 rx_type、drug_category、days_qty，无法完成判定（口径不可比或依赖字段未落地）。依据：处方审核规则体系 §3 特殊药品处方量限
```

> 归因文本里**只有** `patient_ref` 这一支脱敏标识允许出现；姓名、身份证号等可识别明细
> 一律被掩码成〔已隐去〕（`engine/evidence.py` 的隐私约束）。

---

### 4. 不可判定台账 `out/undeterminable_ledger.csv`

只收「判不了」的条目，按**缺失字段 × 规则**聚合。

列：`missing_field,rule_id,cause,affected_prescriptions,affected_depts`

```csv
missing_field,rule_id,cause,affected_prescriptions,affected_depts
days_qty,RX-LEGAL-MA-OUT-DAYS,医嘱未填写,1,1
dosage_text,RX-NORM-USAGE-VAGUE,医嘱未填写,1,1
drug_category,RX-LEGAL-CR-OUT-DAYS,医嘱未填写,1,1
```

`cause` 一列说清缺口的成因，**两种成因处置方不同**：

| `cause` | 含义 | 处置方 |
|---|---|---|
| `接口未回传` | 整列没落地（接口没接这一列） | 信息科改接口 |
| `医嘱未填写` | 栏位在但内容为空 | 退回医师补写 |

> 不分开就会把「医师漏填」排到信息科的接口改造清单上——这是本产品最核心的一条区分。

---

### 5. 字段补齐顺序表 `out/field_gap_priority.csv`

按「受影响处方数降序 → 受影响规则数降序 → 字段名升序」**稳定排序**，同输入同输出。

列：`priority,missing_field,affected_prescriptions,affected_rules,affected_depts,interface_gap_prescriptions,causes,route_to`

```csv
priority,missing_field,affected_prescriptions,affected_rules,affected_depts,interface_gap_prescriptions,causes,route_to
1,drug_category,1,4,1,0,医嘱未填写,退回医师补写
2,rx_type,1,3,1,0,医嘱未填写,退回医师补写
3,skin_test_result,1,2,1,0,医嘱未填写,退回医师补写
```

`route_to` 直接给出承接方；`interface_gap_prescriptions` 是其中「接口未回传」的处方数——
这张表就是给信息科排改造顺序用的。

---

### 6. 规则体检 `out/rule_health.csv`

```bash
python -m med_rule_fieldmap.cli rules health --rules rules/sample \
    --db state/mrfm.sqlite3 --inbox inbox --out out
```

```text
[rules health] 规则体检：19 条规则 · 整体覆盖率 85.8% · 僵尸规则 3 条 · 恒不可判定 1 条 · 不在生效集 0 条
[rules health] 僵尸规则：RX-APPR-INDICATION-MATCH · 适用 0 张 · 判出结论 0 张 · 覆盖率 0.0% · 本批无适用处方，规则从未被触发：适用条件（applies_when）可能写得过窄或字段名写错，请核对规则行与落地文件表头。
[rules health] 恒不可判定：RX-APPR-SKIN-TEST-POSITIVE · 适用 1 张 · 判出结论 0 张 · 覆盖率 0.0% · 判了 1 张处方、全部落不可判定：规则依赖的字段（skin_test_result）在本批始终拿不到——要么改规则依赖，要么让信息科补接口字段。
[rules health] 部分不可判定：RX-LEGAL-MA-OUT-DAYS · 适用 8 张 · 判出结论 5 张 · 覆盖率 62.5% · 适用 8 张、判出结论 5 张、不可判定 3 张（缺 days_qty、drug_category、rx_type）：补齐这些字段可把覆盖率从 62.5% 提到 100%。
[rules health] 规则体检表 → out/rule_health.csv
```

列：`rule_id,rule_version,layer,applicable_prescriptions,hit_prescriptions,undeterminable_prescriptions,coverage_pct,health,is_zombie,always_undeterminable,blocking_fields,reason`

---

### 7. 台账导出与审计导出

```bash
python -m med_rule_fieldmap.cli ledger export --db state/mrfm.sqlite3 --inbox inbox --out out
python -m med_rule_fieldmap.cli audit export --db state/mrfm.sqlite3 --out out
```

```text
[ledger export] 判定台账：192 条 · 合规 170 · 违规 11 · 不可判定 11 · 判定台账 → out/judgement_ledger.csv
[ledger export] 不可判定台账 → out/undeterminable_ledger.csv（11 组字段缺口）
[ledger export] 字段补齐顺序表 → out/field_gap_priority.csv（5 个字段）
[audit export] 审计流水：判定事件 192 条 · 出站调用 0 条 · 依据缺失 0 条 · 审计台账 → out/audit_ledger.csv
```

**审计台账** `out/audit_ledger.csv` 列：

```text
audit_id,action,batch_no,prescription_no,rule_id,rule_version,conclusion,basis,event_fingerprint,writeback_state,retry_count,channel,error_code,created_at
```

```csv
判定-DEMO-BATCH-001-RX-0001-RX-APPR-DOSE-NONPOSITIVE-2026.10-2026-10-29T08:46:36+00:00,判定,DEMO-BATCH-001,RX-0001,RX-APPR-DOSE-NONPOSITIVE,2026.10,合规,处方审核规则体系 §1 适宜性审核第 3 项（剂量、用法的正确性）,be49b10881c3761aec4e7acfa64a6b0c0e5d0ecf5a8abdd3a4e2a7e6abcd529d,未回写,0,,,2026-10-29T08:46:36+00:00
```

- **判定事件行数 == 判定行数**：一批医嘱判一次就有一条判定事件。
- **同一批次被重投时只补审计、不新增判定**：审计行数增加，判定行数不变。
- **失败的出站调用同样在册**：带累计重试次数与最后错误码——「发了几次都没成」
  是飞检现场要当场答得出的问题。
- **依据出处缺一行就不导出**：显式报错并指出是哪几行，不落盘。

---

### 8. 判定证据链回放

```bash
python -m med_rule_fieldmap.cli batch replay --db state/mrfm.sqlite3 \
    --batch DEMO-BATCH-001 --out out --rules rules/sample --json
```

```json
{
  "batch_no": "DEMO-BATCH-001",
  "replay_path": "out/replay_DEMO-BATCH-001.csv",
  "rows": 192,
  "counts": { "合规": 170, "违规": 11, "不可判定": 11 },
  "chain_health": { "总条数": 192, "断链条数": 0, "链完整条数": 192 },
  "issues": []
}
```

`chain_health` 是**断链检查**：每条结论都要能追到「批次 → 规则号 → 规则版本 → 依据出处 →
事件文件指纹 → 当前状态」，追不到就是断链（`issues` 里点名）。

---

### 9. 状态库与守护状态

```bash
python -m med_rule_fieldmap.cli state --db state/mrfm.sqlite3
python -m med_rule_fieldmap.cli status --db state/mrfm.sqlite3
```

```text
[state] 状态库 state/mrfm.sqlite3 · 批次 1 个
[state] 批次 DEMO-BATCH-001（来源 demo_batch_001.csv）：处方 26 行 · 判定 192 条 · 审计 192 行 · 状态 已判定 · 指纹 be49b10881c3
[state] 合计：判定 192 条 · 审计 192 行

[status] 守护进程：未在运行
[status] 批次 1 个 · 判定 192 条 · 待药师复核 192 条 · 没走完的批次 0 个
[status] 累计：收到 0 行 · 合规 0 · 违规 0 · 不可判定 0 · 回写 0 条（失败 0 条） · 推送 0 次 · 平均耗时 0.0s
```

「待药师复核」是**回写门禁的队列长度**：这些行还没被药师确认，因此**不会**出现在任何
回写请求体里。

---

### 10. 药师确认 / 撤回 与待重放队列

```bash
python -m med_rule_fieldmap.cli confirm --db state/mrfm.sqlite3 --list
python -m med_rule_fieldmap.cli writeback --db state/mrfm.sqlite3 --out out
```

```text
[confirm] 待回写候选 0 条（只有已确认的结论在这里）
[confirm] 确认 / 撤回留痕 0 条

[writeback] 待重放队列：待重放 0 条 · 已重放 0 条 · 累计重试 0 次
[writeback] 队列文件 → out/pending_writeback.csv
```

确认一条结论（**只确认不发请求**）：

```bash
python -m med_rule_fieldmap.cli confirm --db state/mrfm.sqlite3 \
    --key <判定幂等键> --by <药师标识> --note "已复核，同意退回医师"
```

确认并触发回写（需 HIS 凭据，缺则**显式失败**）：

```bash
python -m med_rule_fieldmap.cli confirm --db state/mrfm.sqlite3 \
    --key <判定幂等键> --by <药师标识> --trigger
```

撤回：

```bash
python -m med_rule_fieldmap.cli revoke --db state/mrfm.sqlite3 \
    --key <判定幂等键> --by <药师标识> --note "误判"
```

---

### 11. 出站链路预检（不发任何请求）

```bash
python -m med_rule_fieldmap.outbound --check
python -m med_rule_fieldmap.outbound --check-push
```

```text
预检未通过：HIS 审方复核回写不可用（显式失败，不静默跳过）。
[缺失] HIS_BASE_URL 未配置 → 回写不可用（必须显式失败）
[缺失] HIS_API_TOKEN 未配置 → 拒绝发出请求
```

```text
预检未通过：企业微信推送不可用（判定与回写主流程不受影响）。
[缺失] WECOM_WEBHOOK_URL 未配置 → 推送不可用（判定与回写不受影响）
```

**退出码各自独立**：回写链路不可用是**致命**的（它是唯一改变业务状态的路径，退出码 `2`）；
推送链路不可用**只影响递话**（判定与回写照常）。

---

### 12. 整文件挂起（坏文件不做部分判定）

投一份被截断的落地文件：

```bash
python -m med_rule_fieldmap.cli scan --inbox inbox --out out --wait-sec 2
```

```text
[watch] truncated_batch_001.csv 挂起：缺少必需列 · 文件 truncated_batch_001.csv 缺少必需列：event_time；整文件挂起，不做部分判定
[scan] 整文件挂起：truncated_batch_001.csv · 缺少必需列 · 缺列 event_time（文件 truncated_batch_001.csv 缺少必需列：event_time；整文件挂起，不做部分判定）
[scan] 挂起台账：out/suspended_ledger.csv · 当前挂起 1 份（truncated_batch_001.csv）
```

挂起台账 `out/suspended_ledger.csv`：

```csv
file_name,source_path,kind,reason,missing_columns,suspended_at,status,released_at,detail
truncated_batch_001.csv,/srv/his/orders/truncated_batch_001.csv,缺少必需列,文件 truncated_batch_001.csv 缺少必需列：event_time；整文件挂起，不做部分判定,event_time,2026-10-29T08:50:07+00:00,挂起,,
```

修好后人工解除（**追加**一条「已解除」，历史挂起行不改写；文件留在 `inbox/` 原地）：

```bash
python -m med_rule_fieldmap.cli scan --inbox inbox --out out --release truncated_batch_001.csv
```

**挂起原因分五类**（处置动作不同，不能混成一个提示）：空文件 / 编码不可识别 / 缺少表头 /
文件截断 / 缺少必需列。

---

### 13. 守护进程真实终端会话

```bash
python -m med_rule_fieldmap.daemon --inbox inbox --rules rules/sample --out out \
    --state-db state/mrfm.sqlite3 --heartbeat-sec 3
```

**推送已配置**（回写与推送两条链路都指向本地真实 HTTP 服务；完整会话见
`docs/screenshots/daemon-session.png`，终端原文见 `docs/screenshots/daemon-session.txt`）：

```text
合理用药审方字段对照系统 · 规则 19 条 · 入站 /private/tmp/mrfm-demo/inbox · 回写 http://127.0.0.1:52184/api/v1/prescription/review
  产物 /private/tmp/mrfm-demo/out · 状态库 /private/tmp/mrfm-demo/state/state.sqlite3 · 心跳 3s · 推送 已配置
[alive] 规则 19 条 · 已处理 0 批 · 待判定 0 条
[watch] 已启用 watchdog 文件系统事件监听
[alive] 规则 19 条 · 已处理 0 批 · 待判定 0 条
批次 DEMO-BATCH-001（来源 orders_20261029_0900.csv）：收到 26 行 · 命中规则 192 条 · 合规 170 · 违规 11 · 不可判定 11 · 判定台账 → /private/tmp/mrfm-demo/out/judgement_ledger.csv
  逐行判定：以下 22 条需要人工处理（合规 170 条计入判定台账）
  判定 RX-0024 · RX-LEGAL-CR-OUT-DAYS · 不可判定 · 缺少 drug_category
  判定 RX-0007 · RX-LEGAL-MA-INPATIENT-DAYS · 不可判定 · 缺少 rx_type
  判定 RX-0017 · RX-LEGAL-SPIRIT1-OUT-DAYS · 违规 · 开具日量 2 超过该档上限「1 次常用量」。
  判定 RX-0003 · RX-NORM-FRONT-DIAG · 违规 · 前记「临床诊断」缺项（字段 clinical_diag 栏位在但内容为空）。栏位已落地而内容为空，属处方本身不合格，须退回医师补写
  判定 RX-0013 · RX-LEGAL-MA-INPATIENT-DAYS · 违规 · 开具日量 2 超过该档上限「1 日常量」。
  …（违规 11 条 + 不可判定 11 条，逐条列完）
  字段缺口台账 → /private/tmp/mrfm-demo/out/undeterminable_ledger.csv（11 组）· 字段补齐顺序表 → /private/tmp/mrfm-demo/out/field_gap_priority.csv（5 个字段）
  推送 企业微信 · 已送达 · HTTP 200 · 卡片 736 字 · 尝试 1 次
回写：发出 0 条 · 失败 0 条（只发药师已确认的结论）
[audit] 批次 DEMO-BATCH-001 · 判定 192 条 · 事件指纹 be49b10881c3
  运行摘要：批次 DEMO-BATCH-001 · 收到 26 行 · 合规 170 · 违规 11 · 不可判定 11 · 回写 0 条 · 耗时 0.08s
[alive] 规则 19 条 · 已处理 1 批 · 待判定 192 条
[alive] 规则 19 条 · 已处理 1 批 · 待判定 192 条
```

**推送未配置**（告警路径：推送是递话，不是改业务状态——判定与回写照常）：

```text
合理用药审方字段对照系统 · 规则 19 条 · 入站 /private/tmp/mrfm_t40d/inbox · 回写 https://his.example.internal/api/v1/prescription/review
  产物 /private/tmp/mrfm_t40d/out · 状态库 /private/tmp/mrfm_t40d/state.sqlite3 · 心跳 3s · 推送 未配置（推送会告警，不阻断判定）
[alive] 规则 19 条 · 已处理 0 批 · 待判定 0 条
[watch] 已启用 watchdog 文件系统事件监听
[warn] [wecom] WECOM_WEBHOOK_URL 未配置：企业微信推送地址缺失，请求不发出（凭据位一律从环境变量读，不得静默跳过）。
[alert] 企业微信推送未送达（不影响判定与回写）：WECOM_WEBHOOK_URL 未配置：…
批次 DEMO-BATCH-001（来源 demo_batch_001.csv）：收到 26 行 · 命中规则 192 条 · 合规 170 · 违规 11 · 不可判定 11 · 判定台账 → /private/tmp/mrfm_t40d/out/judgement_ledger.csv
  逐行判定：以下 22 条需要人工处理（合规 170 条计入判定台账）
  判定 RX-0024 · RX-LEGAL-CR-OUT-DAYS · 不可判定 · 缺少 drug_category
  …
  字段缺口台账 → /private/tmp/mrfm_t40d/out/undeterminable_ledger.csv（11 组）· 字段补齐顺序表 → /private/tmp/mrfm_t40d/out/field_gap_priority.csv（5 个字段）
回写：发出 0 条 · 失败 0 条（只发药师已确认的结论）
[audit] 批次 DEMO-BATCH-001 · 判定 192 条 · 事件指纹 be49b10881c3
  运行摘要：批次 DEMO-BATCH-001 · 收到 26 行 · 合规 170 · 违规 11 · 不可判定 11 · 回写 0 条 · 耗时 0.04s
[alive] 规则 19 条 · 已处理 1 批 · 待判定 192 条
[alive] 规则 19 条 · 已处理 1 批 · 待判定 192 条
  收到 SIGTERM：完成当前批次后退出（不留半批状态）
合理用药审方字段对照系统 已停止 · 共处理 1 批 · 挂起 0 个文件 · 推送 0 次
```

画面分层：**批次摘要行最显眼 → 逐行判定次级 → 心跳与审计最暗灰**——
与 `docs/concepts/visual-target.md` 的视觉目标卡一致（目标 vs 实测的差距见 README
「概念 / 实测对照」一节的诚实标注）。逐行判定只列**违规与不可判定**两类
（合规明细在判定台账里，画面上不刷屏）；不可判定行点名缺哪个字段，
违规行给出违规要点。

> 启动前 `HIS_BASE_URL` 未配置时，守护进程**显式失败并退出**：

```text
[alert] [his_review] HIS_BASE_URL 未配置：HIS 审方复核回写是本产品唯一改变业务状态的路径，缺失时必须显式失败并退出，不得静默跳过。
[daemon] 启动失败：HIS_BASE_URL 未配置：…
```

---

### 14. 规模化回归与三态 vs 二元基线对比

```bash
python3 scripts/bench_scale.py --out out --rules rules/sample
```

### 规模回归 `out/scale_report.json`

```text
规模 2000 行 × 19 条规则 · 判定 13804 条 · 吞吐 3503.3 行/秒 · 缓存命中率 99.35%
解析缓存：命中 2301 · 未命中 15 · 解析 15 次 · 淘汰 0 · 失效 0 · 存量 15/512
三态分布：合规 12443 · 违规 507 · 不可判定 854
逐处方结论分布：合规 1200 · 违规 500 · 不可判定 300
阈值校验：缓存命中率 ≥ 90% ✓ · 吞吐 ≥ 200 行/秒 ✓ · 行数一致 ✓
```

**两项硬阈值**（低于阈值即回归不通过）：解析树缓存命中率 ≥ **90%**、吞吐 ≥ **200 行/秒**。

### 基线对比 `out/baseline_compare.json`

基线定义：**同链去三态**——同一套规则、同一份数据、同一条判定链，只把结论空间从三态压成二元。

| 实现 | 不可判定召回率 | 误拦（判违规） | 静默放行（判合规） | 逐处方结论一致率 |
|---|---|---|---|---|
| **三态判定（本产品）** | **100.0%** | 0% | 0% | **100%** |
| 二元 · 字段缺失即跳过 | 0.0% | 0% | 100% | 85% |
| 二元 · 字段缺失即判违规 | 0.0% | 100% | 0% | 85% |
| 独立二元实现（只做表达式规则） | 0.0% | — | — | 60% |

同一批 **300 张缺字段的处方**：

| 实现 | 这 300 张的结论 | 后果 |
|---|---|---|
| **三态判定（本产品）** | 不可判定 300 | 进字段缺口台账，信息科排接口补齐顺序、药师人工复核 |
| 二元 · 字段缺失即跳过 | 合规 300（静默放行） | 台账看着一切正常，这批**一条没审** |
| 二元 · 字段缺失即判违规 | 违规 300（误拦） | 缺字段被当成「不合理」硬拦，药师白复核、患者被挡 |

> **边界**：规模集是**确定性合成**的（`scripts/gen_synthetic_orders.py`，固定种子 20261028，
> 逐字节可复现），只作测试数据层；基准标签是合成器的**设计结论**，不是外部权威标注集。
> 这份对比度量的是「判定链在规模下是否稳定、三态相对二元的差异有多大」，
> **不等于对真实院内医嘱的准确率**。独立于合成集的官方口径正确性证据见 10 条官方案例回归
> （`tests/test_baseline_cases.py`）。

---

## 15. 公开数据集对照（只作对照，不是对接方式）

```bash
python3 scripts/fetch_mimic_demo.py
```

退出码：`0` 下载成功并通过完整性校验；`2` 降级到仓内结构快照（网络不可用 / 缺凭据 /
HTTP 错误）并打印告警；`3` 校验不通过（硬失败，**不降级**）。

用途边界见 [`data-sources.md`](data-sources.md)：公开数据集**只是测试数据层**，
正式对接口径是入站 CSV / HL7 字段规范与 HIS 审方复核请求体规范。
