# 商圈客流设备重叠与人数区间估计

面向商场经营数据分析员，处理采信、判定、核算、复核与归档。

## 启动

```bash
docker compose up --build -d
```

访问 http://localhost:18251 。初次启动自动运行脏样例，可切换 clean.csv 与 legacy.csv。顶部“运行批次”会重新计算，同源输入复用批次。

## 业务模块

- 设备采信：`/m1_ingest`、`/api/m1_ingest`
- 区域映射：`/m2_mapping`、`/api/m2_mapping`
- 观测分桶：`/m3_versions`、`/api/m3_versions`
- 重叠约束：`/m4_segments`、`/api/m4_segments`
- 人数估界：`/m5_calculate`、`/api/m5_calculate`
- 日汇对账：`/m6_reconcile`、`/api/m6_reconcile`
- 报告归档：`/m7_archive`、`/api/m7_archive`
- 晚班交接：`/m8_handover`、`/api/m8_handover`（交接单 `/api/handovers`）

## 核心规则

无法识别个人时只输出满足区域交集约束的人数上下界；矛盾观测隔离而非伪造精确人数。完整数值、边界和异常定义见 `docs/DATA-DICTIONARY.md`。

## 晚班交接裁决

容量线默认 240 人，可用运行参数 `capacity` 覆盖。同一裁决函数同时决定页面清单、交接记录与处置状态：

- 下界 > 容量线：**已超限**，上一班已证实的拥挤；
- 下界 ≤ 容量线 < 上界：**跨线待证**，区间跨过容量线但未证实，既不从值班页消失，也不写成已超限；
- 上界 ≤ 容量线：未超限，不进入交接清单。

同一场连续（相邻 UTC 小时、同裁决）风险合并为一条处置记录，按 `R{序号}` 沿版本链追踪。晚到数据只追加新版交接单：页面列出重新判定的时段（新增风险/结论变化/解除风险），已签收归档不被改写；同一批结果重跑按 `run_id` 幂等，不新增风险段；补发事务失败整体回滚，旧版交接单保留。

## 验证

```bash
docker compose exec -T app pytest -q
docker compose config --quiet
```

原始样例在 `sample_data/`。测试在 `tests/`。封存文件是单份CSV，含运行清单、计算证据、隔离原因与对账。复核需提交当前版本，陈旧复核返回409。

## 数据与限制

SQLite保存在records命名卷，普通重启与构建不会清除。确认可丢弃本项目数据后，运行 `docker compose down -v` 再启动可重置。单文件上限2MB；不接入外部业务系统；历史规则由本项目样例定义，不代表实际行业标准。
