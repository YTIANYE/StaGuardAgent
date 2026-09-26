# 业务稳定性巡检报告

> 巡检编号 `run-20260925-100000-S2`　|　场景 `S2`　|　数据源 `file`　|　状态 `部分降级`

## 一、巡检概览

- **巡检窗口**：2026-09-25 09:30~10:00（30分钟），粒度 60s
- **覆盖范围**：8 个服务 / 15 个实例
- **执行耗时**：5.0s

### 稳定性评分

| 评分 | 等级 | 异常分布 |
| --- | --- | --- |
| **45.0 / 100** | E 严重 | 🔴 P1 紧急: 10　🟠 P2 严重: 7　🟡 P3 一般: 11　🔵 P4 轻微: 4 |

### 扣分明细

| 维度 | 权重 | 得分 | 扣分 | 依据 |
| --- | ---: | ---: | ---: | --- |
| 可用性 | 40 | 40 | 0 | 全部服务成功率与业务错误率均在 SLO 之内，错误预算未消耗 |
| 性能 | 25 | 4 | 21 | 18 条异常、影响 7 个实例（占全量 47%），最严重为 gateway/gateway-0 P95 延时（P1 紧急，幅度 1.00） |
| 资源 | 20 | 3.2 | 16.8 | 14 条异常、影响 7 个实例（占全量 47%），最严重为 inventory-svc/inventory-svc-1 CPU 使用率（P1 紧急，幅度 1.00） |
| 稳定性 | 15 | 15 | 0 | 本维度未发现异常 |

> **封顶生效**：紧急（P1）异常命中核心链路服务，故障会直接放大到用户侧，封顶 45

> AI 降级运行（配置中已关闭 AI 分析（STAGUARD_LLM_ENABLED=false）），评分完全由规则算出，未经模型调整。

## 二、风险总结

本次巡检共识别 1 个根因簇，最严重的是 inventory-svc 的CPU 使用率 异常（P1 紧急），已扩散至 3 个服务。归因类别集中于：资源瓶颈。

- **趋势判断**：持续恶化
  - 依据：当前仍有 1 个根因簇未恢复，涉及 inventory-svc CPU 使用率、gateway P95 延时、order-svc P95 延时
  - 预测：若未在窗口内处置，异常会继续沿依赖链向上游扩散，建议在下一轮巡检前完成止血
- **证据不足之处**：
  - 当前结论由规则归因生成（原因：配置中已关闭 AI 分析（STAGUARD_LLM_ENABLED=false）），未经大模型复核，对复杂链路的因果判断能力有限
  - 如需更深入的根因推理，请配置 LLM 凭据后重跑本次巡检

## 三、异常清单

| 级别 | 服务 / 实例 | 指标 | 实测 | 基线 | 偏离 | 持续 | 起始 | 级别理由 |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 🔴 P1 紧急 | gateway / gateway-0 | P95 延时 | 1073.12 | 59.46 | +1700.71% | 21分钟 | 09:38 | P95 延时 1073ms，超过 200ms 红线并持续 21 分钟 |
| 🔴 P1 紧急 | gateway / gateway-1 | P95 延时 | 1079.97 | 62.24 | +1635.01% | 21分钟 | 09:38 | P95 延时 1080ms，超过 200ms 红线并持续 21 分钟 |
| 🔴 P1 紧急 | gateway / gateway-2 | P95 延时 | 1078.65 | 58.36 | +1737.85% | 21分钟 | 09:38 | P95 延时 1079ms，超过 200ms 红线并持续 21 分钟 |
| 🔴 P1 紧急 | order-svc / order-svc-0 | P95 延时 | 1371.45 | 93.65 | +1375.79% | 21分钟 | 09:38 | P95 延时 1371ms，超过 300ms 红线并持续 21 分钟 |
| 🔴 P1 紧急 | order-svc / order-svc-1 | P95 延时 | 1379.52 | 96.41 | +1357.18% | 21分钟 | 09:38 | P95 延时 1380ms，超过 300ms 红线并持续 21 分钟 |
| 🔴 P1 紧急 | order-svc / order-svc-2 | P95 延时 | 1370.89 | 100.11 | +1299.15% | 21分钟 | 09:38 | P95 延时 1371ms，超过 300ms 红线并持续 21 分钟 |
| 🔴 P1 紧急 | inventory-svc / inventory-svc-1 | P95 延时 | 1407.28 | 83.70 | +1589.55% | 17分钟 | 09:40 | P95 延时 1407ms，超过 300ms 红线并持续 17 分钟 |
| 🔴 P1 紧急 | inventory-svc / inventory-svc-1 | CPU 使用率 | 96 | 43.34 | +120.80% | 17分钟 | 09:40 | CPU 使用率 96.00%，持续 17 分钟接近打满 |
| 🔴 P1 紧急 | order-svc / order-svc-0 | 连接池使用率 | 99 | 53.78 | +85.77% | 4分钟 | 09:43 | 连接池使用率 99.00%，接近打满，新请求将被拒绝 |
| 🔴 P1 紧急 | order-svc / order-svc-1 | 连接池使用率 | 99 | 53.94 | +81.05% | 3分钟 | 09:40 | 连接池使用率 99.00%，接近打满，新请求将被拒绝 |
| 🟠 P2 严重 | gateway / gateway-0 | P99 延时 | 1122.67 | 95.76 | +1086.54% | 21分钟 | 09:38 | P99 延时 1123ms，显著超过 500ms 红线 |
| 🟠 P2 严重 | gateway / gateway-1 | P99 延时 | 1104.88 | 81.44 | +1234.15% | 21分钟 | 09:38 | P99 延时 1105ms，显著超过 500ms 红线 |
| 🟠 P2 严重 | gateway / gateway-2 | P99 延时 | 1109.25 | 86.72 | +1164.99% | 21分钟 | 09:38 | P99 延时 1109ms，显著超过 500ms 红线 |
| 🟠 P2 严重 | order-svc / order-svc-2 | 连接池使用率 | 99 | 51.33 | +94.36% | 11分钟 | 09:40 | 连接池使用率 99.00%，持续高位 |
| 🟠 P2 严重 | inventory-svc / inventory-svc-1 | P99 延时 | 2068.35 | 125.58 | +1551.01% | 21分钟 | 09:38 | P99 延时抖动明显（CV=0.65），峰值 2068ms |
| 🟠 P2 严重 | inventory-svc / inventory-svc-1 | P99 延时 | 2068.35 | 125.58 | +1551.01% | 8分钟 | 09:40 | P99 延时 2068ms，显著超过 800ms 红线 |
| 🟠 P2 严重 | inventory-svc / inventory-svc-1 | 连接池使用率 | 95.43 | 48.98 | +93.76% | 3分钟 | 09:42 | 连接池使用率 95.43%，持续高位 |
| 🟡 P3 一般 | order-svc / order-svc-0 | P99 延时 | 1461.13 | 146.41 | +882.25% | 21分钟 | 09:38 | P99 延时 1461ms 超出 900ms 红线 |
| 🟡 P3 一般 | order-svc / order-svc-1 | P99 延时 | 1438.70 | 143.17 | +885.27% | 21分钟 | 09:38 | P99 延时 1439ms 超出 900ms 红线 |
| 🟡 P3 一般 | order-svc / order-svc-2 | P99 延时 | 1445.47 | 142.54 | +933.64% | 21分钟 | 09:38 | P99 延时 1445ms 超出 900ms 红线 |
| 🟡 P3 一般 | order-svc / order-svc-0 | P99 延时 | 1461.13 | 146.41 | +882.25% | 30分钟 | 09:30 | P99 延时抖动偏高（CV=0.57） |
| 🟡 P3 一般 | order-svc / order-svc-1 | P99 延时 | 1438.70 | 143.17 | +885.27% | 30分钟 | 09:30 | P99 延时抖动偏高（CV=0.57） |
| 🟡 P3 一般 | order-svc / order-svc-2 | P99 延时 | 1445.47 | 142.54 | +933.64% | 30分钟 | 09:30 | P99 延时抖动偏高（CV=0.56） |
| 🟡 P3 一般 | gateway / gateway-0 | 连接池使用率 | 91.96 | 46.54 | +98.87% | 16分钟 | 09:43 | 连接池使用率 91.96% 超出 90% 预警线 |
| 🟡 P3 一般 | gateway / gateway-1 | 连接池使用率 | 92.29 | 47.48 | +95.36% | 19分钟 | 09:40 | 连接池使用率 92.29% 超出 90% 预警线 |
| 🟡 P3 一般 | gateway / gateway-2 | 连接池使用率 | 94.30 | 47.94 | +96.10% | 21分钟 | 09:38 | 连接池使用率 94.30% 超出 90% 预警线 |
| 🟡 P3 一般 | order-svc / order-svc-1 | CPU 使用率 | 85.77 | 51.25 | +69.20% | 2分钟 | 09:39 | CPU 使用率 85.77% 超出 85% 预警线 |
| 🟡 P3 一般 | order-svc / order-svc-0 | CPU 使用率 | 90.32 | 54.96 | +64.61% | 3分钟 | 09:50 | CPU 使用率 90.32% 超出 85% 预警线 |
| 🔵 P4 轻微 | gateway / gateway-0 | CPU 使用率 | 81.01 | 47.14 | +73.75% | 21分钟 | 09:38 | CPU 使用率 81.01% 显著偏离基线 47.14% |
| 🔵 P4 轻微 | gateway / gateway-1 | CPU 使用率 | 84.02 | 49.86 | +69.75% | 21分钟 | 09:38 | CPU 使用率 84.02% 显著偏离基线 49.86% |
| 🔵 P4 轻微 | gateway / gateway-2 | CPU 使用率 | 82.07 | 47.42 | +75.38% | 21分钟 | 09:38 | CPU 使用率 82.07% 显著偏离基线 47.42% |
| 🔵 P4 轻微 | order-svc / order-svc-2 | CPU 使用率 | 81.99 | 48.88 | +67.66% | 21分钟 | 09:38 | CPU 使用率 81.99% 显著偏离基线 48.88% |

<details><summary>另有 4 条低级别异常被同指标更严重的问题抑制</summary>

- PERF-03 gateway/gateway-0 P99 延时（P3 一般）— 已由 PERF-01|gateway|gateway-0|latency_p99 覆盖
- PERF-03 gateway/gateway-1 P99 延时（P3 一般）— 已由 PERF-01|gateway|gateway-1|latency_p99 覆盖
- PERF-03 gateway/gateway-2 P99 延时（P3 一般）— 已由 PERF-01|gateway|gateway-2|latency_p99 覆盖
- RES-05 inventory-svc/inventory-svc-1 P99 延时（P3 一般）— 已由 PERF-03|inventory-svc|inventory-svc-1|latency_p99 覆盖

</details>

## 四、多粒度巡检统计

> 统计对象为**未抑制异常**；影响面 = 该维度下出现异常的实例数 / 实例总数。本次覆盖 8 个服务（其中 3 个有异常）、5 个集群。

### 服务维度

| 服务 | 集群 | 异常数 | 最严重 | 受影响实例 | 影响面 | 主要指标 | 命中规则 |
| --- | --- | ---: | --- | --- | ---: | --- | --- |
| order-svc | 交易集群 | 15 | 🔴 P1 紧急 | order-svc-0、order-svc-1、order-svc-2（3 / 3） | 100% | P95 延时 | PERF-01、PERF-02、PERF-03、RES-01、RES-04 |
| gateway | 交易集群 | 12 | 🔴 P1 紧急 | gateway-0、gateway-1、gateway-2（3 / 3） | 100% | P95 延时 | PERF-01、PERF-02、RES-01、RES-04 |
| inventory-svc | 供应链集群 | 5 | 🔴 P1 紧急 | inventory-svc-1（1 / 2） | 50% | P95 延时 | PERF-01、PERF-02、PERF-03、RES-01、RES-04 |
| bank-channel | 外部依赖集群 | 0 | — | —（0 / 1） | 0% | — | — |
| mysql-order | 外部依赖集群 | 0 | — | —（0 / 1） | 0% | — | — |
| payment-svc | 支付集群 | 0 | — | —（0 / 2） | 0% | — | — |
| redis-session | 外部依赖集群 | 0 | — | —（0 / 1） | 0% | — | — |
| user-svc | 用户集群 | 0 | — | —（0 / 2） | 0% | — | — |

### 集群维度

| 集群 | 服务数 | 异常服务 | 异常数 | 最严重 | 受影响实例 | 影响面 | 根因簇 |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| 交易集群 | 2 | 2 | 27 | 🔴 P1 紧急 | 6 / 6 | 100% | 0 |
| 供应链集群 | 1 | 1 | 5 | 🔴 P1 紧急 | 1 / 2 | 50% | 1 |
| 外部依赖集群 | 3 | 0 | 0 | — | 0 / 3 | 0% | 0 |
| 支付集群 | 1 | 0 | 0 | — | 0 / 2 | 0% | 0 |
| 用户集群 | 1 | 0 | 0 | — | 0 / 2 | 0% | 0 |

## 五、根因分析

> 本次 32 条未抑制异常全部归入 1 个根因簇，逐簇给出根因。另有 4 条被抑制异常不单独归因（见异常清单折叠区）。

### CLUS-01　inventory-svc　[P1 紧急]

- **信号规模**：32 条异常，涉及 3 个服务（gateway、inventory-svc、order-svc）
- **传播路径**：`inventory-svc`　（数据流方向，故障影响由此向上游扩散）
- **规则假设**：3 个服务出现关联异常，疑似同一根因传导，inventory-svc 的偏离最显著
- **AI 根因**：**资源瓶颈** — inventory-svc 的 CPU 使用率 越线，实例资源已接近打满；证据：CPU 使用率 96.00%，持续 17 分钟接近打满（置信度 65%）
- **AI 认定的根因服务**：`inventory-svc`
- **影响面**：影响 3 个服务（gateway、order-svc），波及 32 项指标
- **建议责任方**：供应链组

<details><summary>证据引用（32 条）</summary>

- `RES-01:inventory-svc/inventory-svc-1/cpu_usage`
- `PERF-02:gateway/gateway-0/latency_p95`
- `PERF-02:gateway/gateway-1/latency_p95`
- `PERF-02:gateway/gateway-2/latency_p95`
- `PERF-02:order-svc/order-svc-0/latency_p95`
- `PERF-02:order-svc/order-svc-1/latency_p95`
- `PERF-02:order-svc/order-svc-2/latency_p95`
- `PERF-02:inventory-svc/inventory-svc-1/latency_p95`
- `RES-04:order-svc/order-svc-0/conn_usage`
- `RES-04:order-svc/order-svc-1/conn_usage`
- `PERF-01:gateway/gateway-0/latency_p99`
- `PERF-01:gateway/gateway-1/latency_p99`
- …其余 20 条同类证据（见 JSON 报告）

</details>

## 六、修复建议

### 立即止血（本次窗口内）

- **CLUS-01 · 资源瓶颈**
  - 先摘除 inventory-svc-1 观察整体水位是否回落，再评估扩容

### 短期加固（本周内）

- [CLUS-01] 定位热点实例，评估扩容或优化热点代码路径，必要时先摘除异常实例。
- [CLUS-01] 为 CPU 使用率 配置对应的告警与自动化处置预案，缩短下次的发现时长

### 长期治理

- 把本次命中的规则阈值反哺到容量规划：反复越线的指标说明当前水位与业务量已经不匹配
- 为高频根因（依赖故障、资源瓶颈）沉淀标准处置手册，把平均恢复时间从小时级压到分钟级
- 对核心链路补充依赖隔离与熔断降级能力，避免单点依赖故障放大成全链路事故

## 七、稳定性趋势

**对比对象**：`run-20260925-100000-S1`（2026-09-27T01:54+08:00）

- 评分变化：**+24.6 分**（较上次上升）
- 稳定性评分较上次上升 24.6 分；新增异常 6 项、已恢复 29 项、持续未解决 30 项；持续未解决的项需要确认是否有人在跟进。

**新增异常（6）**

- [P2 严重] inventory-svc/inventory-svc-1 P99 延时
- [P1 紧急] inventory-svc/inventory-svc-1 P95 延时
- [P2 严重] inventory-svc/inventory-svc-1 P99 延时
- [P1 紧急] inventory-svc/inventory-svc-1 CPU 使用率
- [P2 严重] inventory-svc/inventory-svc-1 连接池使用率
- [P3 一般] inventory-svc/inventory-svc-1 P99 延时

**持续未解决（30）**

- [P2 严重] gateway/gateway-0 P99 延时
- [P2 严重] gateway/gateway-1 P99 延时
- [P2 严重] gateway/gateway-2 P99 延时
- [P3 一般] order-svc/order-svc-0 P99 延时
- [P3 一般] order-svc/order-svc-1 P99 延时
- [P3 一般] order-svc/order-svc-2 P99 延时
- [P1 紧急] gateway/gateway-0 P95 延时
- [P1 紧急] gateway/gateway-1 P95 延时
- [P1 紧急] gateway/gateway-2 P95 延时
- [P1 紧急] order-svc/order-svc-0 P95 延时
- [P1 紧急] order-svc/order-svc-1 P95 延时
- [P1 紧急] order-svc/order-svc-2 P95 延时

**已恢复（29）**

- bank-channel/bank-channel-0 success_rate
- gateway/gateway-0 success_rate
- gateway/gateway-1 success_rate
- gateway/gateway-2 success_rate
- order-svc/order-svc-0 success_rate
- order-svc/order-svc-1 success_rate
- order-svc/order-svc-2 success_rate
- payment-svc/payment-svc-0 success_rate
- payment-svc/payment-svc-1 success_rate
- payment-svc/payment-svc-0 business_error_rate
- payment-svc/payment-svc-1 business_error_rate
- gateway/payment-svc success_rate

### 评分历史

`▃▄▆▃█▁▃`　最近 7 次：45 → 60 → 84 → 45 → 96 → 20 → 45

## 八、附录

### 数据质量

- 数据质量良好，未发现缺失或越界
- 有效样本 216000 / 应有 216000（缺失率 0.00%，插值占比 0.15%）
- 结论置信度：**高**

### 规则命中统计

| 规则 | 命中 |
| --- | ---: |
| PERF-01 | 7 |
| PERF-02 | 7 |
| PERF-03 | 7 |
| RES-01 | 7 |
| RES-04 | 7 |
| RES-05 | 1 |

### 动态基线覆盖

- 动态基线覆盖率：**100%**（其余为样本不足时的静态阈值回退）

### 变更事件

| 时间 | 服务 | 类型 | 版本 | 说明 |
| --- | --- | --- | --- | --- |
| 18:00 | inventory-svc | 配置变更 | - | 库存服务限流阈值由 800 QPS 调整为 1200 QPS |
| 05:00 | gateway | 配置变更 | - | 网关健康检查间隔由 5s 调整为 3s |

### 执行阶段与 AI 调用

| 阶段 | 状态 | 耗时 | 说明 |
| --- | --- | ---: | --- |
| 数据采集 | 成功 | 569ms | file 通道采集 215677 个点 / 150 条时序（565ms） |
| 标准化清洗 | 成功 | 2565ms | 216000 个有效点 / 150 条时序；缺失率 0.00%，置信度 高 |
| 结构化存储 | 成功 | 226ms | 数据集 S2 就绪（216000 个指标点） |
| 动态基线 | 成功 | 1468ms | 计算 150 条基线，动态基线覆盖率 100%（归档 603894 点） |
| 规则巡检 | 成功 | 164ms | 16 条规则命中 36 项原始发现（164ms） |
| 聚合去重 | 成功 | 2ms | 36 条异常（抑制 4 条）聚成 1 个根因簇；多粒度统计 8 个服务 / 5 个集群 |
| 稳定性评分 | 成功 | 0ms | 规则算分 45.0（E 严重）；紧急（P1）异常命中核心链路服务，故障会直接放大到用户侧，封顶 45 |
| AI 智能分析 | 降级 | 3ms | 降级运行（配置中已关闭 AI 分析（STAGUARD_LLM_ENABLED=false））；1 条根因结论，toke… |
| 报告输出 | 成功 | 6ms | Markdown 报告 10774 字符，已写入 run-20260925-100000-S2.md |

- AI 分析：降级运行（配置中已关闭 AI 分析（STAGUARD_LLM_ENABLED=false））；调用 0 次，耗时 0ms，token 0，提示词版本 v1
