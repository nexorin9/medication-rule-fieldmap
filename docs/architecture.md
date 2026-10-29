# 架构手册

> 面向要接手维护这套系统的人：模块怎么分层、状态怎么流转、幂等靠哪几把钥匙、
> 失败时数据停在哪儿、以及审计链怎么把一条结论追回源头。
> 面向只想用的人：看 [`../README.md`](../README.md) 的「典型场景」即可。

## 1. 一句话架构

**事件驱动守护进程**：HIS 医嘱落地目录里出现一个批次文件 → 切批次 → 逐处方 × 逐规则
求值出**三态**结论 → 落台账 → 推企业微信卡片 → 药师确认后回写 HIS 审方复核接口。
判定链**全部确定性求值**，没有模型参与，同一批数据配同一版规则结果逐字可复现。

## 2. 模块分层

```
┌───────────────────────────────────────────────────────────────────────────┐
│ 入口层                                                                     │
│  daemon.py（常驻守护）   cli.py（运维通道）   outbound/__main__.py（预检） │
└───────────────┬───────────────────────┬───────────────────────────────────┘
                │                       │
┌───────────────▼───────────────────────▼───────────────────────────────────┐
│ 入站层 ingest/                                                             │
│  watcher.py（事件监听） → integrity.py（完整性） → suspend.py（挂起台账）    │
│  csv_reader.py（26/28 列表头映射）  hl7_orm.py（HL7 v2 ORM^O01）            │
│  fingerprint.py（事件文件 sha256 指纹）                                     │
└───────────────┬───────────────────────────────────────────────────────────┘
                │ 处方行（PrescriptionRow）
┌───────────────▼───────────────────────────────────────────────────────────┐
│ 规则层 rules/     repository.py（生效区间 + mtime 热重载）  schema.py（校验）│
└───────────────┬───────────────────────────────────────────────────────────┘
                │ 规则行（RuleRow）
┌───────────────▼───────────────────────────────────────────────────────────┐
│ 判定层 engine/                                                           │
│  sched.py（适用条件匹配）→ judge.py（逐处方 × 逐规则主链）                 │
│  expression/（lexer→parser→AST→validator→evaluator + 归一层 + LRU 缓存）  │
│  limits.py / skin_test.py / normativity.py / children_age.py /             │
│  duplicate.py / supernormal.py / indication.py（各规则族专用路径）         │
│  severity.py（严重度 / 点评分级）  evidence.py（归因文本 + 隐私约束）        │
│  statemachine.py（状态跃迁）  context.py（跨行上下文）                     │
└───────┬─────────────────────────────────────────┬─────────────────────────┘
        │                                         │
┌───────▼──────────────────────┐   ┌──────────────▼─────────────────────────┐
│ 台账层 ledger/                 │   │ 存储层 store.py（SQLite / WAL）        │
│  ledger.py（判定台账）          │   │  batches / judgements / judgement_    │
│  gap.py（不可判定台账）          │   │  versions / audit / outbound_calls /  │
│  priority.py（字段补齐顺序）    │   │  writeback_requests / pending_        │
│  rule_health.py（规则体检）     │   │  writeback / confirmations /          │
│  evidence_replay.py（证据链）   │   │  batch_progress / batch_runs          │
│  audit_export.py（审计导出）    │   │  daemon_lock.py（单实例文件锁）        │
└───────┬──────────────────────┘   └──────────────┬─────────────────────────┘
        │                                         │
        └────────────────┬────────────────────────┘
                         ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ 出站层 outbound/                                                           │
│  client.py（地址校验 + deadline 超时预算 + 退避）  idempotency.py（幂等）    │
│  his_review.py（HIS 审方复核 + 撤回通知 + 绑定歧义挂起）                    │
│  retry_queue.py（pending_writeback 队列）  wecom.py（企业微信卡片）          │
│  audit.py（追加式审计留痕）                                                │
└───────────────────────────────────────────────────────────────────────────┘
```

## 3. 主路径调用链

**输入 → 主函数 → 产出**（改这套系统时先守住这条链）：

```
inbox/ 新文件
  → ingest/watcher.py            事件到达 → 连续两次大小/mtime 不变 → 认为写完
  → ingest/integrity.py          完整性五类检查 → 不合格则整文件挂起并停止
  → ingest/csv_reader.py         表头映射 → 处方行（PrescriptionRow）
    或 ingest/hl7_orm.py         HL7 v2 ORM^O01 → 等价处方行
  → ingest/fingerprint.py        事件文件 sha256 指纹（批次去重的依据）
  → store.py                     登记批次（batch_no + 指纹），重复投递只补审计
  → engine/sched.py              applies_when 匹配 → 候选规则集
  → engine/judge.py              逐处方 × 逐规则求值 → 三态结论 + 归因文本
  → engine/statemachine.py       判定行状态：待判定 → 已判定（合法跃迁才放行）
  → ledger/ledger.py             写 out/judgement_ledger.csv
  → ledger/gap.py                写 out/undeterminable_ledger.csv
  → ledger/priority.py           写 out/field_gap_priority.csv
  → outbound/wecom.py            推企业微信批次卡片（失败只告警）
  → outbound/his_review.py       仅「药师已确认」的结论回写 HIS
  → ledger/audit_export.py       写 out/audit_ledger.csv（追加式）
```

### 3.1 规则侧的载入与热重载

`rules/repository.py` 在规则文件 mtime 变化时热重载，并把新表达式文本绑进
`expression/cache.py` 的解析树 LRU（旧解析树**立即失效**，不等 LRU 自然淘汰）。
`expression/validator.py` 在**载入期**预检表达式：引用了未声明字段、含函数调用 / 属性访问 /
赋值 / 导入等能力越界的规则行，**拒绝生效且不影响其余规则**。

### 3.2 三档空值语义

见下一节——它是本系统全部结论的地基，先看懂它再看判定内核。

## 4. 三态判定与空值语义

| 字段落地情况 | 结论 | 归因 | 处置方向 |
|---|---|---|---|
| 字段齐备且表达式为假 | **合规** | 规则号 + 版本 + 判定条件 + 依据出处 | 归档 |
| 字段齐备且表达式为真 | **违规** | 规则号 + 版本 + 命中原因 + 依据出处 | 退回医师 / 药师复核 |
| `required_fields` 里任一字段**缺失或为空** | **不可判定** | 点名缺失字段名 + 规则号 + 依据出处 | 进字段缺口台账 → 信息科补字段 |
| 字段有值但口径不可辨（如日量写成 `3mg`） | **不可判定** | 点名字段 + 「口径不可比」 | 同上 |

**三档空值语义**（`expression/evaluator.py`）是本系统的地基：

| 档 | 判定 | 例子 |
|---|---|---|
| `MISSING` | 字段缺失 / 空串 / 纯空白 | 皮试结果栏空着 |
| `EXPLICIT_NO` | 字段被显式填成否定值 | 皮试结果写「未注明」 |
| `VALUE` | 字段有正常值 | 皮试结果写「阴性」 |

### 5.1 为什么 `MISSING` ≠ `EXPLICIT_NO`

皮试栏空着是「**系统判不了**」（信息科补接口字段后重判），
皮试栏写着「未注明」是「**医师没做**」（退回医师补写）——两者结论不同、处置方不同。

`MISSING` 与任何值比较一律返回 `MISSING`（不抛错、不静默判 false）；
`EXPLICIT_NO` 与正数比较默认判**违规**而非合规。

### 5.2 逻辑连接里的 `MISSING` 传播

| 表达式 | 传播规则 | 理由 |
|---|---|---|
| `A and B` | 任一侧 `MISSING` → 整体 `MISSING`（**刻意不短路**） | 另一侧可能自己就不成立，不能因为一边成立就下结论 |
| `A or B` | 一侧为真 → 整体为真；否则有 `MISSING` → 整体 `MISSING` | 一侧成立就够了 |
| `not A` | **不吃掉** `MISSING` | 「不是未知」仍然是未知 |

## 5. 判定状态机

状态真源在 `engine/statemachine.py` 的 `LEGAL_TRANSITIONS`；非法跃迁抛
`IllegalTransitionError` **并记入告警日志**，绝不静默放行，也绝不把行就地改掉。

| 当前状态 | 允许的去向 | 业务含义 |
|---|---|---|
| 待判定 | 已判定 / 待补字段 / 已挂起 | 刚出结论，或落到字段缺口，或文件坏了 |
| 已判定 | 药师已确认 / 待补字段 / 已撤回 / 已挂起 | 等药师复核 |
| 待补字段 | 已判定（字段补齐后重判）/ 已挂起 | 信息科补完字段后重投 |
| 药师已确认 | 已回写 / 已撤回 / 已挂起 | **唯一允许回写 HIS 的状态** |
| 已回写 | 已撤回 | 回写后仍可撤回（须同步走 HIS 复核更新接口并留痕） |
| 已撤回 | 待判定 | 重新进入判定流程 |
| 已挂起 | 待判定 | 人工解除挂起后重投 |

**两条典型非法跃迁**（都会被拒）：

- 「已判定」→「已回写」：跳过药师确认 —— 拒绝。
- 「待补字段」→「药师已确认」：跳过重判 —— 拒绝。

**重判留痕**：判定表存的是**当前**那一版结论；每次重判往 `judgement_versions` 追加一条
版本号递增的新版本，**旧版本行不被覆盖、不被删除**——
「这条结论当初判成什么、后来为什么改」可逐版回放。

## 6. 批次进度与断点续跑

批次进度表 `batch_progress` 的阶段**只前进不后退**：

```
已接收 → 已判定 → 已推送 → 已闭环
```

**续跑时靠三个计数挡住重复劳动**（不是一个布尔位）：

| 挡什么 | 靠什么 |
|---|---|
| 重复判定 | 判定幂等键（唯一索引） |
| 重复推卡片 | 「已推送的判定行数」比对（规则改版后同一批的结论数变了，也不会多推一张） |
| 重复回写 | 回写幂等键（`outbound/idempotency.py` 的 `WritebackIdempotencyGuard`） |

源文件不在了或被改过（指纹变了）→ **保留断点并告警**，等人工重投，不猜。

## 7. 幂等键体系（三层，别搞混）

| 层 | 键 | 作用 | 落地位置 |
|---|---|---|---|
| 事件指纹 | `sha256(文件字节)` | 判定「是不是同一份落地文件」 | `batches` 表主键 `(batch_no, event_fingerprint)` |
| 判定幂等键 | `sha256(batch_no｜prescription_no｜rule_id｜rule_version)` | 判定「这条结论是不是已经算过」 | `judgements` 表唯一索引 |
| 回写幂等键 | `sha256(判定幂等键｜请求体哈希)` | 回写「这条复核请求是不是已经发过」 | `writeback_requests` 表 + 幂等守卫 |

> 判定幂等键里带 `rule_version`：规则改版后同一处方会**新增**一条判定行（两个版本并存），
> 旧结论不被覆盖——这正是药师与飞检要的「改版前后对照」。

### 7.1 为什么判定幂等键里要带 `rule_version`

规则改版是药事审核岗的**日常动作**，不是异常。若幂等键不含版本，改一次规则就会让
同一张处方的新结论撞上旧结论——要么被当成重复投递丢弃（新规则白上），要么被覆盖
（旧结论凭空消失）。带版本之后：改版 = 新增一条判定行，旧行留在 `judgement_versions`
里可逐版回放，药师能看到「这张处方上一版判成什么、这一版为什么改了」。

### 7.2 重复投递与改版是两件事

| 场景 | 事件指纹变了吗 | 判定行数 | 审计行数 |
|---|---|---|---|
| HIS 重复投递同一份文件 | 没变 | **不变**（只补审计） | 增加 |
| 人工重投同一份文件（换文件名） | 没变（指纹按文件内容算） | **不变** | 增加 |
| 规则改版后重判 | 没变 | **翻倍**（两个版本并存） | 增加 |

**重复投递的处置**：同一 `batch_no` + 同一事件指纹再次到达 → `duplicate=True`，
**只补审计行、不新增判定行**，回写请求数不变。

## 8. 审计链

`audit` 表**追加式**写入，历史行不改写。`action` 列把两类事件分开数——
「审了多少条」与「发出去多少次」是两个数：

| `action` | 何时写 | 关键列 |
|---|---|---|
| `判定` | 每条判定结论落库时 | `rule_id` `rule_version` `conclusion` `basis` `event_fingerprint` |
| `回写` | 每次出站 HIS 调用（含失败） | `writeback_state` `retry_count` `channel` `error_code` |
| `撤回` | 药师撤回一条已确认结论 | `reviewed_by` `withdrawn_by` `created_at` |
| `推送` | 每次企业微信卡片推送 | `channel=WECOM` |
| `重放` | 判定证据链回放 | `batch_no` |

**审计台账导出**（`ledger/audit_export.py`）：任一行缺**依据出处** → 显式报错并指出是哪几行，
**不落盘**——一条结论说不出依据，审计台账等于没有台账。

**一条结论的完整追溯链**（`cli batch replay` 的输出）：

```
批次号 → 处方号 → 规则号 → 规则版本 → 依据出处 → 事件文件指纹 → 当前状态 → 严重度来源 → 点评分级
```

## 9. 失败与降级矩阵

| 失败点 | 表现 | 数据停在哪 | 恢复方式 |
|---|---|---|---|
| 落地文件截断 / 乱码 / 缺必需列 | 整文件挂起，**零判定行** | 文件留在 `inbox/` 原地；挂起台账追加一行 | 修好后 `cli scan --release <文件名>` |
| 文件持续增长超 `wait_deadline` | 按疑似截断挂起 | 同上 | 同上 |
| 规则行缺 `basis` / 表达式语法错 / 能力越界 | 该条规则**不生效**，其余规则照常 | 规则仓库只收合法规则 | 改规则行 → mtime 热重载自动生效 |
| 表达式引用了未声明字段 | 载入期拒（配置错误，**不降级成不可判定**） | 同上 | 在 `required_fields` 里补声明 |
| 字段缺失 | 结论「不可判定」 | 判定行 + 不可判定台账 + 字段补齐顺序表 | 补接口字段后重投 → 自动重判 |
| `HIS_BASE_URL` 未配置 | 守护进程启动失败并退出 | 无（没起进程） | 配好环境变量再起 |
| `HIS_API_TOKEN` 缺失 | 回写请求**一个字节都不发** | 结论留在台账等确认 | 配好令牌 → `cli confirm --trigger` |
| HIS 回写连续失败 | 退避重试 ≤ 3 次 → 落 `pending_writeback` | 结论已落库，未回写 | `cli writeback --replay` |
| 回写响应的 `prescription_no` 与请求不一致 | 判为**实体绑定歧义**，进挂起队列，**不落库** | 判定行保持原状态 | 信息科核对主索引后重投 |
| 撤回通知同步不到 HIS | 记「撤回待同步」并告警 | 本地已撤回，HIS 端仍在 | `cli confirm --resync` |
| 企业微信推送失败 | 只告警，**不阻断**判定与回写 | 批次结论照常落盘 | 配好 webhook 后重推 |
| 第二个守护实例 | 退出码 `3`，打印占用进程信息 | 第一个实例不受影响 | 停掉旧实例再起 |
| 进程被 `kill -9` / 断电 | 锁由内核持有，旧进程退出即释放 | 批次进度落库，重启后接着走 | 自动续跑 |

**总原则**：**失败不静默改数据**。任何一步失败，要么停在原地等人处理，要么留下可核对的
台账与告警——不存在「跑完了但其实什么都没做」的状态。

## 10. 配置面

配置全部读环境变量，程序内**无硬编码地址与密钥**（`config.py` 的模块级常量只放路径类默认值）：

| 变量 | 缺省 | 用途 |
|---|---|---|
| `HIS_BASE_URL` | 无 | HIS 审方复核基址；**未配置则守护进程显式失败并退出** |
| `HIS_API_TOKEN` | 无 | 回写 Bearer 令牌；缺失时拒绝发出请求 |
| `WECOM_WEBHOOK_URL` | 无 | 企业微信群机器人地址；缺失时推送告警，不阻断主流程 |
| `MRFM_INBOX_DIR` | `inbox` | 医嘱落地目录（只读） |
| `MRFM_OUT_DIR` | `out` | 台账与导出产物目录 |
| `MRFM_STATE_DB` | `state/mrfm.sqlite3` | 状态库（SQLite / WAL） |
| `MRFM_RULES_DIR` | `rules` | 规则行目录（mtime 热重载） |
| `MRFM_HEARTBEAT_SEC` | `30` | 心跳行打印间隔 |
| `MRFM_BASE_DIR` | 无 | 相对路径的基准目录（部署时把数据目录挂到别处用） |

## 11. 落盘产物

| 文件 | 内容 | 谁写 |
|---|---|---|
| `out/judgement_ledger.csv` | 逐处方 × 逐规则的三态结论 + 归因 + 依据 | `ledger/ledger.py` |
| `out/undeterminable_ledger.csv` | 不可判定项按「缺失字段 × 规则」聚合，带**成因**（接口未回传 / 医嘱未填写） | `ledger/gap.py` |
| `out/field_gap_priority.csv` | 按缺失字段排序的接口补齐顺序表（受影响处方数 / 规则数 / 科室数 / 优先级 / 承接方） | `ledger/priority.py` |
| `out/rule_health.csv` | 规则体检：僵尸规则 / 恒不可判定 / 覆盖率 | `ledger/rule_health.py` |
| `out/audit_ledger.csv` | 追加式审计流水（判定 / 回写 / 撤回 / 推送） | `ledger/audit_export.py` |
| `out/suspended_ledger.csv` | 整文件挂起台账（含人工解除记录） | `ingest/suspend.py` |
| `out/pending_writeback.csv` | 退避超次后待重放的回写队列 | `outbound/retry_queue.py` |
| `out/binding_ambiguity_ledger.csv` | 回写实体绑定歧义（两个处方号都记下来，交信息科核对） | `outbound/his_review.py` |
| `out/replay_<批次号>.csv` | 判定证据链回放件 | `ledger/evidence_replay.py` |

## 12. 测试与验证

| 层 | 位置 | 验证什么 |
|---|---|---|
| 单元测试 | `tests/test_*.py` | 表达式求值器、判定内核、台账聚合、出站客户端、状态机 |
| 官方案例回归 | `tests/test_baseline_cases.py` | 对 `domain/cases/prescription_review_cases.json` 10 条官方案例逐条回归 + 新增第三态案例 |
| 规模化回归 | `tests/test_scale_regression.py` | 2000 行 × 生效规则集：正确性 + 解析树缓存命中率 ≥ 90% + 吞吐 ≥ 200 行/秒 |
| 三态 vs 二元基线 | `tests/test_baseline_compare.py` | 不可判定召回率对比，产出 `out/baseline_compare.json` |
| 数据准备脚本 | `scripts/fetch_mimic_demo.py`、`scripts/gen_synthetic_orders.py`、`scripts/bench_scale.py` | 公开数据集下载（断网降级）、确定性种子合成规模集、规模与基线报告 |

> **数据来源边界**（公开数据集只是测试数据层，正式对接口径在 `spec.md`）见
> [`data-sources.md`](data-sources.md)。

## 13. 相关文档

| 文档 | 内容 |
|---|---|
| [`../README.md`](../README.md) | 产品说明：适用场景、快速开始、典型场景、验收自检 |
| [`cli.md`](cli.md) | 命令手册：全部入口与子命令、参数、退出码 |
| [`examples.md`](examples.md) | 输出样例与终端回显（真实运行结果） |
| [`data-sources.md`](data-sources.md) | 数据来源与边界：哪层是正式对接、哪层只是测试数据 |
| [`concepts/visual-target.md`](concepts/visual-target.md) | 视觉目标卡（结构化，无图像能力不伪造图） |
| [`reports/`](reports/) | 规模化回归与基线对比报告副本 |
