# 业务稳定性巡检报告

> 巡检编号 `run-20260925-100000-S1`　|　场景 `S1`　|　数据源 `file`　|　状态 `成功`

## 一、巡检概览

- **巡检窗口**：2026-09-25 09:30~10:00（30分钟），粒度 60s
- **覆盖范围**：8 个服务 / 15 个实例
- **执行耗时**：12.1s

### 稳定性评分

| 评分 | 等级 | 异常分布 |
| --- | --- | --- |
| **20.4 / 100** | E 严重 | 🔴 P1 紧急: 24　🟠 P2 严重: 15　🟡 P3 一般: 5　🔵 P4 轻微: 6 |

### 扣分明细

| 维度 | 权重 | 得分 | 扣分 | 依据 |
| --- | ---: | ---: | ---: | --- |
| 可用性 | 40 | 0 | 40 | gateway 错误预算消耗达允许值的 287.4 倍（BIZ-06） |
| 性能 | 25 | 3 | 22 | 18 条异常、影响 9 个实例（占全量 60%），最严重为 gateway/gateway-0 P95 延时（P1 紧急，幅度 1.00） |
| 资源 | 20 | 2.4 | 17.6 | 18 条异常、影响 9 个实例（占全量 60%），最严重为 payment-svc/payment-svc-0 连接池使用率（P1 紧急，幅度 1.00） |
| 稳定性 | 15 | 15 | 0 | 本维度未发现异常 |

> 评分为规则算分（逐项依据见上表），AI 未调整分数。

## 二、风险总结

本次异常以 bank-channel 为最下游源头：其连接池使用率打满至 99%、P95 延时飙升至 3495ms（基线 270ms），并沿 bank-channel -> payment-svc -> order-svc -> gateway 逐级向上传导，导致 payment-svc 连接池打满 96%~99%、业务错误率升至 7.5%，order-svc/gateway 成功率跌至 91.9%/93.6%、P95 延时超 1.5s。全链路 4 个服务、10 个实例同时劣化，规则评分 20.4（E 级）与实际影响基本相符。两条变更均在异常起始前 41 分钟以上，不构成本轮根因。

- **趋势判断**：整体平稳
  - 依据：各异常指标 series 的 trend 均为 stable，first/last 值显示异常在窗口内已从峰值回落（如 bank-channel conn_usage first 69.17 -> last 58.93，gateway P95 first 46ms -> last 65ms），说明异常集中在 09:36-09:59 区间，窗口末段已开始恢复。
  - 预测：若 bank-channel 连接池容量不调整，按当前基线使用率 63% 与峰值 99% 的余量推算，下一波交易高峰（约 1 小时内）bank-channel 连接池将再次打满，payment-svc 连接池（当前峰值 99%）将同步触顶，gateway 成功率可能再次跌破 94%。
- **证据不足之处**：
  - bank-channel 在拓扑中已是最下游（无更下游依赖），无法从证据区分其连接池打满是「自身处理能力不足」还是「其对接的银行外部系统响应变慢」，因此按口径归为 resource_bottleneck。
  - 两条变更（gateway 健康检查间隔 276 分钟前、bank-channel 超时 41 分钟前）均不在异常起始前 30 分钟窗口内，不能作为根因；bank-channel 超时由 30s 调至 25s 是否加剧了连接占用，证据不足，仅作背景。
  - BIZ-06 链路证据的 baseline_confidence 为 low（static_fallback），其链路健康度绝对值可信度有限，但方向性（payment-svc 为最弱环节）与实例级证据一致。
  - 规则 rule_hypothesis 认为 bank-channel 偏离最显著，本结论与其一致；但无法排除 bank-channel 与 payment-svc 同时受第三方（如银行侧限流）影响，缺少外部依赖侧指标佐证。

## 三、异常清单

| 级别 | 服务 / 实例 | 指标 | 实测 | 基线 | 偏离 | 持续 | 起始 | 级别理由 |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 🔴 P1 紧急 | gateway / gateway-0 | 成功率 | 93.57 | 99.97 | -6.41% | 20分钟 | 09:39 | 成功率 93.57%，较基线 99.97% 下跌 6.41 个百分点（相对跌幅 6.41%），持续 20 分钟 |
| 🔴 P1 紧急 | gateway / gateway-1 | 成功率 | 93.56 | 99.98 | -6.41% | 20分钟 | 09:39 | 成功率 93.56%，较基线 99.98% 下跌 6.41 个百分点（相对跌幅 6.41%），持续 20 分钟 |
| 🔴 P1 紧急 | gateway / gateway-2 | 成功率 | 93.56 | 99.97 | -6.41% | 20分钟 | 09:39 | 成功率 93.56%，较基线 99.97% 下跌 6.41 个百分点（相对跌幅 6.41%），持续 20 分钟 |
| 🔴 P1 紧急 | order-svc / order-svc-0 | 成功率 | 91.95 | 99.97 | -8.02% | 23分钟 | 09:36 | 成功率 91.95%，较基线 99.97% 下跌 8.02 个百分点（相对跌幅 8.02%），持续 23 分钟 |
| 🔴 P1 紧急 | order-svc / order-svc-1 | 成功率 | 91.97 | 99.97 | -7.99% | 23分钟 | 09:36 | 成功率 91.97%，较基线 99.97% 下跌 7.99 个百分点（相对跌幅 7.99%），持续 23 分钟 |
| 🔴 P1 紧急 | order-svc / order-svc-2 | 成功率 | 91.92 | 99.96 | -8.05% | 23分钟 | 09:36 | 成功率 91.92%，较基线 99.96% 下跌 8.04 个百分点（相对跌幅 8.05%），持续 23 分钟 |
| 🔴 P1 紧急 | payment-svc / payment-svc-0 | 成功率 | 90 | 99.96 | -9.97% | 18分钟 | 09:40 | 成功率 90.00%，较基线 99.96% 下跌 9.96 个百分点（相对跌幅 9.97%），持续 18 分钟 |
| 🔴 P1 紧急 | payment-svc / payment-svc-1 | 成功率 | 90 | 99.96 | -9.97% | 18分钟 | 09:40 | 成功率 90.00%，较基线 99.96% 下跌 9.96 个百分点（相对跌幅 9.97%），持续 18 分钟 |
| 🔴 P1 紧急 | bank-channel / bank-channel-0 | 成功率 | 92 | 99.96 | -7.96% | 21分钟 | 09:37 | 成功率 92.00%，较基线 99.96% 下跌 7.96 个百分点（相对跌幅 7.96%），持续 21 分钟 |
| 🔴 P1 紧急 | gateway / gateway-0 | P95 延时 | 1554.38 | 59.46 | +2508.26% | 23分钟 | 09:36 | P95 延时 1554ms，超过 200ms 红线并持续 23 分钟 |
| 🔴 P1 紧急 | gateway / gateway-1 | P95 延时 | 1559.09 | 62.24 | +2404.75% | 23分钟 | 09:36 | P95 延时 1559ms，超过 200ms 红线并持续 23 分钟 |
| 🔴 P1 紧急 | gateway / gateway-2 | P95 延时 | 1558.23 | 58.36 | +2554.97% | 23分钟 | 09:36 | P95 延时 1558ms，超过 200ms 红线并持续 23 分钟 |
| 🔴 P1 紧急 | order-svc / order-svc-0 | P95 延时 | 1974.26 | 93.65 | +2024.46% | 23分钟 | 09:36 | P95 延时 1974ms，超过 300ms 红线并持续 23 分钟 |
| 🔴 P1 紧急 | order-svc / order-svc-1 | P95 延时 | 1972.91 | 96.41 | +1983.99% | 23分钟 | 09:36 | P95 延时 1973ms，超过 300ms 红线并持续 23 分钟 |
| 🔴 P1 紧急 | order-svc / order-svc-2 | P95 延时 | 1974.20 | 100.11 | +1914.90% | 23分钟 | 09:36 | P95 延时 1974ms，超过 300ms 红线并持续 23 分钟 |
| 🔴 P1 紧急 | payment-svc / payment-svc-0 | P95 延时 | 2560.95 | 189.49 | +1258.00% | 23分钟 | 09:36 | P95 延时 2561ms，超过 400ms 红线并持续 23 分钟 |
| 🔴 P1 紧急 | payment-svc / payment-svc-1 | P95 延时 | 2543.53 | 174.55 | +1367.70% | 23分钟 | 09:36 | P95 延时 2544ms，超过 400ms 红线并持续 23 分钟 |
| 🔴 P1 紧急 | bank-channel / bank-channel-0 | P95 延时 | 3495.06 | 270.67 | +1190.65% | 21分钟 | 09:37 | P95 延时 3495ms，超过 460ms 红线并持续 21 分钟 |
| 🔴 P1 紧急 | payment-svc / payment-svc-0 | 连接池使用率 | 96 | 52.76 | +81.04% | 16分钟 | 09:41 | 连接池使用率 96.00%，接近打满，新请求将被拒绝 |
| 🔴 P1 紧急 | payment-svc / payment-svc-1 | 连接池使用率 | 99 | 57.27 | +72.30% | 16分钟 | 09:41 | 连接池使用率 99.00%，接近打满，新请求将被拒绝 |
| 🔴 P1 紧急 | bank-channel / bank-channel-0 | 连接池使用率 | 99 | 63.21 | +58.16% | 23分钟 | 09:36 | 连接池使用率 99.00%，接近打满，新请求将被拒绝 |
| 🔴 P1 紧急 | payment-svc / payment-svc | 成功率 | 82.80 | 99.93 | -17.14% | 30分钟 | 09:30 | 链路 payment-svc -> bank-channel 健康度 82.80%（基线 99.93%），下降 17.14%，最弱环节 p… |
| 🔴 P1 紧急 | gateway / payment-svc | 成功率 | 71.26 | 99.87 | -28.65% | 30分钟 | 09:30 | 链路 gateway -> user-svc -> redis-session 健康度 71.26%（基线 99.87%），下降 28.6… |
| 🔴 P1 紧急 | order-svc / payment-svc | 成功率 | 76.14 | 99.89 | -23.78% | 30分钟 | 09:30 | 链路 order-svc -> inventory-svc -> mysql-order 健康度 76.14%（基线 99.89%），下降… |
| 🟠 P2 严重 | payment-svc / payment-svc-0 | 业务错误率 | 7.50 | 0.07 | +11085.68% | 18分钟 | 09:40 | 业务错误率 7.50%，较基线 0.07% 上升 7.43 个百分点 |
| 🟠 P2 严重 | payment-svc / payment-svc-1 | 业务错误率 | 7.50 | 0.07 | +11729.65% | 18分钟 | 09:40 | 业务错误率 7.50%，较基线 0.07% 上升 7.43 个百分点 |
| 🟠 P2 严重 | gateway / gateway-0 | P99 延时 | 1603.18 | 95.76 | +1594.39% | 23分钟 | 09:36 | P99 延时 1603ms，显著超过 500ms 红线 |
| 🟠 P2 严重 | gateway / gateway-1 | P99 延时 | 1591.76 | 81.44 | +1822.06% | 23分钟 | 09:36 | P99 延时 1592ms，显著超过 500ms 红线 |
| 🟠 P2 严重 | gateway / gateway-2 | P99 延时 | 1594.16 | 86.72 | +1717.99% | 23分钟 | 09:36 | P99 延时 1594ms，显著超过 500ms 红线 |
| 🟠 P2 严重 | order-svc / order-svc-0 | P99 延时 | 2042.07 | 146.41 | +1272.79% | 23分钟 | 09:36 | P99 延时 2042ms，显著超过 900ms 红线 |
| 🟠 P2 严重 | order-svc / order-svc-1 | P99 延时 | 2045.69 | 143.17 | +1300.95% | 23分钟 | 09:36 | P99 延时 2046ms，显著超过 900ms 红线 |
| 🟠 P2 严重 | order-svc / order-svc-2 | P99 延时 | 2028.57 | 142.54 | +1350.61% | 23分钟 | 09:36 | P99 延时 2029ms，显著超过 900ms 红线 |
| 🟠 P2 严重 | payment-svc / payment-svc-0 | P99 延时 | 2615.25 | 248.79 | +935.30% | 23分钟 | 09:36 | P99 延时 2615ms，显著超过 1200ms 红线 |
| 🟠 P2 严重 | payment-svc / payment-svc-1 | P99 延时 | 2658.46 | 289.39 | +839.78% | 23分钟 | 09:36 | P99 延时 2658ms，显著超过 1200ms 红线 |
| 🟠 P2 严重 | bank-channel / bank-channel-0 | P99 延时 | 5148.58 | 375.42 | +1255.38% | 21分钟 | 09:37 | P99 延时 5149ms，显著超过 1500ms 红线 |
| 🟠 P2 严重 | order-svc / order-svc-1 | 连接池使用率 | 99 | 53.94 | +81.05% | 15分钟 | 09:44 | 连接池使用率 99.00%，持续高位 |
| 🟠 P2 严重 | order-svc / order-svc-0 | 连接池使用率 | 97.97 | 53.78 | +83.83% | 11分钟 | 09:36 | 连接池使用率 97.97%，持续高位 |
| 🟠 P2 严重 | order-svc / order-svc-2 | 连接池使用率 | 95.63 | 51.33 | +87.76% | 4分钟 | 09:53 | 连接池使用率 95.63%，持续高位 |
| 🟠 P2 严重 | gateway / gateway-1 | 连接池使用率 | 95.50 | 47.48 | +102.16% | 2分钟 | 09:48 | 连接池使用率 95.50%，持续高位 |
| 🟡 P3 一般 | gateway / gateway-0 | 连接池使用率 | 90.28 | 46.54 | +95.23% | 23分钟 | 09:36 | 连接池使用率 90.28% 超出 90% 预警线 |
| 🟡 P3 一般 | gateway / gateway-2 | 连接池使用率 | 93.09 | 47.94 | +93.59% | 23分钟 | 09:36 | 连接池使用率 93.09% 超出 90% 预警线 |
| 🟡 P3 一般 | order-svc / order-svc-0 | CPU 使用率 | 87.98 | 54.96 | +60.34% | 5分钟 | 09:52 | CPU 使用率 87.98% 超出 85% 预警线 |
| 🟡 P3 一般 | order-svc / order-svc-1 | CPU 使用率 | 85.72 | 51.25 | +69.10% | 1分钟 | 09:48 | CPU 使用率 85.72% 超出 85% 预警线 |
| 🟡 P3 一般 | order-svc / order-svc-2 | CPU 使用率 | 86.57 | 48.88 | +77.02% | 1分钟 | 09:57 | CPU 使用率 86.57% 超出 85% 预警线 |
| 🔵 P4 轻微 | gateway / gateway-0 | CPU 使用率 | 80.51 | 47.14 | +72.66% | 23分钟 | 09:36 | CPU 使用率 80.51% 显著偏离基线 47.14% |
| 🔵 P4 轻微 | gateway / gateway-1 | CPU 使用率 | 83.83 | 49.86 | +69.37% | 23分钟 | 09:36 | CPU 使用率 83.83% 显著偏离基线 49.86% |
| 🔵 P4 轻微 | gateway / gateway-2 | CPU 使用率 | 81.25 | 47.42 | +73.63% | 23分钟 | 09:36 | CPU 使用率 81.25% 显著偏离基线 47.42% |
| 🔵 P4 轻微 | payment-svc / payment-svc-0 | CPU 使用率 | 80.15 | 43.23 | +86.56% | 23分钟 | 09:36 | CPU 使用率 80.15% 显著偏离基线 43.23% |
| 🔵 P4 轻微 | payment-svc / payment-svc-1 | CPU 使用率 | 81.97 | 47.65 | +72.52% | 23分钟 | 09:36 | CPU 使用率 81.97% 显著偏离基线 47.65% |
| 🔵 P4 轻微 | bank-channel / bank-channel-0 | CPU 使用率 | 57.65 | 26.21 | +121.94% | 23分钟 | 09:36 | CPU 使用率 57.65% 显著偏离基线 26.21% |

<details><summary>另有 9 条低级别异常被同指标更严重的问题抑制</summary>

- PERF-03 gateway/gateway-0 P99 延时（P3 一般）— 已由 PERF-01|gateway|gateway-0|latency_p99 覆盖
- PERF-03 gateway/gateway-1 P99 延时（P3 一般）— 已由 PERF-01|gateway|gateway-1|latency_p99 覆盖
- PERF-03 gateway/gateway-2 P99 延时（P3 一般）— 已由 PERF-01|gateway|gateway-2|latency_p99 覆盖
- PERF-03 order-svc/order-svc-0 P99 延时（P3 一般）— 已由 PERF-01|order-svc|order-svc-0|latency_p99 覆盖
- PERF-03 order-svc/order-svc-1 P99 延时（P3 一般）— 已由 PERF-01|order-svc|order-svc-1|latency_p99 覆盖
- PERF-03 order-svc/order-svc-2 P99 延时（P3 一般）— 已由 PERF-01|order-svc|order-svc-2|latency_p99 覆盖
- PERF-03 payment-svc/payment-svc-0 P99 延时（P3 一般）— 已由 PERF-01|payment-svc|payment-svc-0|latency_p99 覆盖
- PERF-03 payment-svc/payment-svc-1 P99 延时（P3 一般）— 已由 PERF-01|payment-svc|payment-svc-1|latency_p99 覆盖
- PERF-03 bank-channel/bank-channel-0 P99 延时（P3 一般）— 已由 PERF-01|bank-channel|bank-channel-0|latency_p99 覆盖

</details>

## 四、多粒度巡检统计

> 统计对象为**未抑制异常**；影响面 = 该维度下出现异常的实例数 / 实例总数。本次覆盖 8 个服务（其中 4 个有异常）、5 个集群。

### 服务维度

| 服务 | 集群 | 异常数 | 最严重 | 受影响实例 | 影响面 | 主要指标 | 命中规则 |
| --- | --- | ---: | --- | --- | ---: | --- | --- |
| gateway | 交易集群 | 16 | 🔴 P1 紧急 | gateway-0、gateway-1、gateway-2（3 / 3） | 100% | 成功率 | BIZ-01、BIZ-06、PERF-01、PERF-02、RES-01、RES-04 |
| order-svc | 交易集群 | 16 | 🔴 P1 紧急 | order-svc-0、order-svc-1、order-svc-2（3 / 3） | 100% | 成功率 | BIZ-01、BIZ-06、PERF-01、PERF-02、RES-01、RES-04 |
| payment-svc | 支付集群 | 13 | 🔴 P1 紧急 | payment-svc-0、payment-svc-1（2 / 2） | 100% | 成功率 | BIZ-01、BIZ-02、BIZ-06、PERF-01、PERF-02、RES-01、RES-04 |
| bank-channel | 外部依赖集群 | 5 | 🔴 P1 紧急 | bank-channel-0（1 / 1） | 100% | 成功率 | BIZ-01、PERF-01、PERF-02、RES-01、RES-04 |
| inventory-svc | 供应链集群 | 0 | — | —（0 / 2） | 0% | — | — |
| mysql-order | 外部依赖集群 | 0 | — | —（0 / 1） | 0% | — | — |
| redis-session | 外部依赖集群 | 0 | — | —（0 / 1） | 0% | — | — |
| user-svc | 用户集群 | 0 | — | —（0 / 2） | 0% | — | — |

### 集群维度

| 集群 | 服务数 | 异常服务 | 异常数 | 最严重 | 受影响实例 | 影响面 | 根因簇 |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| 交易集群 | 2 | 2 | 32 | 🔴 P1 紧急 | 6 / 6 | 100% | 0 |
| 支付集群 | 1 | 1 | 13 | 🔴 P1 紧急 | 2 / 2 | 100% | 0 |
| 外部依赖集群 | 3 | 1 | 5 | 🔴 P1 紧急 | 1 / 3 | 33% | 1 |
| 供应链集群 | 1 | 0 | 0 | — | 0 / 2 | 0% | 0 |
| 用户集群 | 1 | 0 | 0 | — | 0 / 2 | 0% | 0 |

## 五、根因分析

> 本次 50 条未抑制异常全部归入 1 个根因簇，逐簇给出根因。另有 9 条被抑制异常不单独归因（见异常清单折叠区）。

### CLUS-01　bank-channel　[P1 紧急]

- **信号规模**：50 条异常，涉及 4 个服务（bank-channel、gateway、order-svc、payment-svc）
- **传播路径**：`gateway → order-svc → payment-svc → bank-channel`　（数据流方向，故障影响由此向上游扩散）
- **规则假设**：4 个服务出现关联异常，疑似同一根因传导，bank-channel 的偏离最显著
- **AI 根因**：**资源瓶颈** — bank-channel 作为链路最下游，其连接池使用率被用到 99% 打满、P95 延时升至 3495ms，自身处理能力耗尽后向上游 payment-svc、order-svc、gateway 逐级传导超时与失败。（置信度 72%）
- **AI 认定的根因服务**：`bank-channel`
- **影响面**：gateway 三个实例成功率由 99.97% 跌至 93.56%（错误预算消耗达允许值 287 倍），P95 延时由 ~60ms 升至 ~1555ms；order-svc 成功率跌至 91.9%、P95 延时 1974ms；payment-svc 成功率跌至 90%、业务错误率升至 7.5%、连接池打满；bank-channel 自身成功率跌至 92%。链路 payment-svc->bank-channel 健康度 82.8%，gateway 侧链路健康度仅 71.26%，交易主链路整体不可用。
- **建议责任方**：支付组（bank-channel 对接方）

<details><summary>证据引用（14 条）</summary>

- `RES-04:bank-channel/bank-channel-0/conn_usage`
- `PERF-02:bank-channel/bank-channel-0/latency_p95`
- `PERF-01:bank-channel/bank-channel-0/latency_p99`
- `BIZ-01:bank-channel/bank-channel-0/success_rate`
- `RES-04:payment-svc/payment-svc-0/conn_usage`
- `RES-04:payment-svc/payment-svc-1/conn_usage`
- `BIZ-02:payment-svc/payment-svc-0/business_error_rate`
- `BIZ-02:payment-svc/payment-svc-1/business_error_rate`
- `BIZ-01:payment-svc/payment-svc-0/success_rate`
- `BIZ-01:order-svc/order-svc-0/success_rate`
- `PERF-02:order-svc/order-svc-0/latency_p95`
- `BIZ-01:gateway/gateway-0/success_rate`
- …其余 2 条同类证据（见 JSON 报告）

</details>

## 六、修复建议

### 立即止血（本次窗口内）

- **CLUS-01 · 资源瓶颈**
  - 立即对 bank-channel 连接池做紧急扩容（提高 max connections 上限）并临时降级非核心交易请求，缓解 99% 打满状态。

### 短期加固（本周内）

- [CLUS-01] 排查 bank-channel 侧慢请求堆积原因：抓取 09:36-10:00 窗口内 P99 达 5149ms 的请求样本，确认是银行侧响应变慢还是本地连接泄漏/未释放。
- [CLUS-01] 对 payment-svc 到 bank-channel 的调用增加熔断与快速失败（超时下调至 1s 级 + 并发舱壁隔离），避免下游慢调用把 payment-svc 连接池拖满。
- [CLUS-01] 复盘 bank-channel 连接池容量规划：当前基线使用率已达 63%，峰值余量不足，需按峰值 QPS 重新核算上限。

### 长期治理

- 为 bank-channel 连接池设置基于队列等待时长的自动扩容策略，而非固定上限，避免下游慢响应时连接被长期占用。
- 在 payment-svc -> bank-channel 之间引入熔断器与并发隔离舱壁，防止单一外部渠道故障拖垮整个支付链路。
- 对 gateway/order-svc/payment-svc 的连接池使用率建立分级告警（80% 预警、90% 严重），并联动自动降级开关。
- 梳理全链路超时预算：bank-channel 25s 超时明显长于上游 gateway 的容忍度，需按调用链逐跳收敛超时时间。

## 七、稳定性趋势

**对比对象**：`run-20260925-100000-S0`（2026-09-27T01:54+08:00）

- 评分变化：**-76.0 分**（较上次下降）
- 稳定性评分较上次下降 76.0 分；新增异常 59 项、已恢复 1 项、持续未解决 0 项。

**新增异常（59）**

- [P1 紧急] bank-channel/bank-channel-0 成功率
- [P1 紧急] gateway/gateway-0 成功率
- [P1 紧急] gateway/gateway-1 成功率
- [P1 紧急] gateway/gateway-2 成功率
- [P1 紧急] order-svc/order-svc-0 成功率
- [P1 紧急] order-svc/order-svc-1 成功率
- [P1 紧急] order-svc/order-svc-2 成功率
- [P1 紧急] payment-svc/payment-svc-0 成功率
- [P1 紧急] payment-svc/payment-svc-1 成功率
- [P2 严重] payment-svc/payment-svc-0 业务错误率
- [P2 严重] payment-svc/payment-svc-1 业务错误率
- [P1 紧急] gateway/payment-svc 成功率

**已恢复（1）**

- order-svc/order-svc-1 mem_usage

### 评分历史

`▃▃▄▆▃█▁`　最近 7 次：45 → 45 → 60 → 84 → 45 → 96 → 20

## 八、附录

### 数据质量

- 数据质量良好，未发现缺失或越界
- 有效样本 216000 / 应有 216000（缺失率 0.00%，插值占比 0.15%）
- 结论置信度：**高**

### 规则命中统计

| 规则 | 命中 |
| --- | ---: |
| BIZ-01 | 9 |
| BIZ-02 | 2 |
| BIZ-06 | 3 |
| PERF-01 | 9 |
| PERF-02 | 9 |
| PERF-03 | 9 |
| RES-01 | 9 |
| RES-04 | 9 |

### 动态基线覆盖

- 动态基线覆盖率：**100%**（其余为样本不足时的静态阈值回退）

### 变更事件

| 时间 | 服务 | 类型 | 版本 | 说明 |
| --- | --- | --- | --- | --- |
| 18:00 | inventory-svc | 配置变更 | - | 库存服务限流阈值由 800 QPS 调整为 1200 QPS |
| 05:00 | gateway | 配置变更 | - | 网关健康检查间隔由 5s 调整为 3s |
| 08:55 | bank-channel | 配置变更 | - | 银行渠道超时时间由 30s 调整为 25s |

### 执行阶段与 AI 调用

| 阶段 | 状态 | 耗时 | 说明 |
| --- | --- | ---: | --- |
| 数据采集 | 成功 | 623ms | file 通道采集 215676 个点 / 150 条时序（620ms） |
| 标准化清洗 | 成功 | 2456ms | 216000 个有效点 / 150 条时序；缺失率 0.00%，置信度 高 |
| 结构化存储 | 成功 | 137ms | 数据集 S1 就绪（216000 个指标点） |
| 动态基线 | 成功 | 1555ms | 计算 150 条基线，动态基线覆盖率 100%（归档 603894 点） |
| 规则巡检 | 成功 | 121ms | 16 条规则命中 59 项原始发现（121ms） |
| 聚合去重 | 成功 | 3ms | 59 条异常（抑制 9 条）聚成 1 个根因簇；多粒度统计 8 个服务 / 5 个集群 |
| 稳定性评分 | 成功 | 0ms | 规则算分 20.4（E 严重） |
| AI 智能分析 | 成功 | 7200ms | deepseek/deepseek-chat；1 条根因结论，token 19352，耗时 7194ms |
| 报告输出 | 成功 | 8ms | Markdown 报告 14601 字符，已写入 run-20260925-100000-S1.md |

- AI 分析：deepseek/deepseek-chat；调用 1 次，耗时 7194ms，token 19352，提示词版本 v1
