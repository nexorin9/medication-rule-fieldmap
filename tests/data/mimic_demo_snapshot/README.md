# 断网降级用的结构快照（不是 MIMIC 真实数据）

本目录里的 `prescriptions.snapshot.csv` 是**手写的结构快照**：列名与 MIMIC-IV Demo
`prescriptions` 表一致（`row_id / subject_id / hadm_id / icustay_id / startdate /
enddate / drug_type / drug / formulary_drug_cod / gsn_ndc / prod_strength /
dose_val_rx / dose_unit_rx / form_val_disp / form_unit_disp / route / starttime /
endtime / stoptime / orderid / order_provider_id / order_category /
order_subcategory / order_reason / order_source / volume / dose_unit_syntax`），
但**行内容是本项目自造的占位样例**，不是 MIMIC-IV 的真实医嘱：

- `subject_id` / `hadm_id` / `icustay_id` 全部取 900001+ / 800001+ / 700001+ 段，
  故意避开 MIMIC 真实编号区间，避免被误当成真实数据；
- 药品名只取通用通用名（Acetaminophen、Aspirin 等），不含任何患者可识别信息；
- `order_reason` / `order_source` / `dose_unit_syntax` 一律写 `SYNTHETIC`。

**用途仅两处**：

1. `scripts/fetch_mimic_demo.py` 在断网 / 无凭据 / HTTP 错误时的**降级落点**，
   保证脚本退出码可区分「下载成功」与「降级」，且降级过程有明确告警；
2. 单元测试里校验下载脚本的降级分支。

**它不是本产品的数据基线**：正式对照数据请运行

```bash
python3 scripts/fetch_mimic_demo.py
```

拿到 MIMIC-IV Demo 的 `prescriptions.csv.gz`（需要 PhysioNet 账号并接受数据使用
协议，见脚本头部许可说明）。该数据只作**对照与测试数据层**，不是产品对接边界；
本产品的正式对接口径是 `spec.md` §2.1 入站字段规范与 §2.3 出站字段规范。

**许可口径**：MIMIC-IV Demo 由 MIT Laboratory for Computational Physiology 维护，
采用 PhysioNet Credentialed Health Data License。本仓库**不转发、不再分发**该
数据集本身，只保留下载脚本与本结构快照。
