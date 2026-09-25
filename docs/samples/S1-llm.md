# 业务稳定性巡检报告

> 巡检编号 `run-20260925-100000-S1`　|　场景 `S1`　|　数据源 `file`　|　状态 `成功`

## 一、巡检概览

- **巡检窗口**：2026-09-25 09:30~10:00（30分钟），粒度 60s
- **覆盖范围**：8 个服务 / 15 个实例
- **执行耗时**：13.1s

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

## 二、风险总结

本次异常以 bank-channel 连接池打满（99%）为最下游的失守点，向上游逐级传导，导致 payment-svc、order-svc、gateway 的成功率下跌与 P95/P99 延时暴涨，gateway 错误预算消耗达允许值的 287 倍。链路各环节的异常起始时间高度一致（09:36 前后），且无 30 分钟内的变更，最可能的触发机制是 bank-channel 侧连接资源被占满（自身能力/外部依赖不可区分），属于资源瓶颈类。

- **趋势判断**：整体平稳
  - 依据：各异常序列的 first/last 值均已回到基线附近（如 bank-channel conn_usage first 69.17 -> last 58.93，gateway success_rate first 99.96 -> last 99.95），trend 字段均为 stable，说明异常窗口已结束、指标已回落。
  - 预测：未来 1 小时若无新增流量或变更，链路应保持恢复状态；但 bank-channel 连接池基线水位已达 63%，若业务量回升至异常时段水平，连接池可能在数分钟内再次逼近 99% 并重演本轮故障，建议在扩容完成前持续观察其 conn_usage。
- **证据不足之处**：
  - bank-channel 在拓扑中已是最下游（无更下游依赖），因此无法区分「其自身连接处理能力不足」与「其外部对接的银行渠道变慢导致连接归还延迟」，本结论按可观测的连接池水位事实归为 resource_bottleneck
  - 证据包中无 QPS/流量指标，无法判断本轮是否存在全链路流量同向变化，故未采用 traffic_fluctuation；若实际存在流量上涨，则触发机制可能应改判为流量波动
  - bank-channel 的配置变更（超时 30s->25s）发生在异常起始前 41 分钟，超出 30 分钟窗口，未作为根因，但其是否加剧了连接占用无法从证据确认
  - 规则给出的 rule_hypothesis 指向 bank-channel 偏离最显著，与本结论一致；但 propagation_path 为 gateway->order-svc->payment-svc->bank-channel（上游到下游），实际影响传播方向应为反向，已在结论中按下游->上游表述
  - BIZ-06 链路证据的 baseline_confidence 为 low 且 series.samples 为 0，其链路健康度数值的可靠性有限，仅作为影响面佐证

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

## 四、根因分析

### CLUS-01　bank-channel　[P1 紧急]

- **信号规模**：50 条异常，涉及 4 个服务（bank-channel、gateway、order-svc、payment-svc）
- **传播路径**：`gateway → order-svc → payment-svc → bank-channel`　（数据流方向，故障影响由此向上游扩散）
- **规则假设**：4 个服务出现关联异常，疑似同一根因传导，bank-channel 的偏离最显著
- **AI 根因**：**资源瓶颈** — bank-channel 连接池使用率打满至 99%（基线 63.2%，持续 23 分钟），新请求被拒绝，导致其自身成功率跌至 92%、P95 延时升至 3495ms，并沿 payment-svc -> order-svc -> gateway 逐级向上游传导。（置信度 72%）
- **AI 认定的根因服务**：`bank-channel`
- **影响面**：gateway 三个实例成功率由 99.97% 跌至 93.56%（错误预算消耗 287 倍），P95 延时由 ~60ms 升至 ~1555ms；order-svc 成功率跌至 91.9%、P95 升至 1974ms；payment-svc 成功率跌至 90%、业务错误率升至 7.5%、连接池使用率 96%~99%；bank-channel 成功率 92%、P95 3495ms、P99 5149ms。全链路 10 个实例受影响，持续约 23 分钟。
- **建议责任方**：支付组（bank-channel 对接方）

<details><summary>证据引用（32 条）</summary>

- `RES-04:bank-channel/bank-channel-0/conn_usage`
- `BIZ-01:bank-channel/bank-channel-0/success_rate`
- `PERF-02:bank-channel/bank-channel-0/latency_p95`
- `PERF-01:bank-channel/bank-channel-0/latency_p99`
- `RES-04:payment-svc/payment-svc-0/conn_usage`
- `RES-04:payment-svc/payment-svc-1/conn_usage`
- `BIZ-01:payment-svc/payment-svc-0/success_rate`
- `BIZ-01:payment-svc/payment-svc-1/success_rate`
- `BIZ-02:payment-svc/payment-svc-0/business_error_rate`
- `BIZ-02:payment-svc/payment-svc-1/business_error_rate`
- `PERF-02:payment-svc/payment-svc-0/latency_p95`
- `PERF-02:payment-svc/payment-svc-1/latency_p95`
- …其余 20 条同类证据（见 JSON 报告）

</details>

## 五、修复建议

### 立即止血（本次窗口内）

- **CLUS-01 · 资源瓶颈**
  - 立即对 bank-channel 连接池做紧急扩容（提高 max connections 或增加实例），并临时下调 payment-svc 对 bank-channel 的并发调用量以释放连接占用

### 短期加固（本周内）

- [CLUS-01] 在 payment-svc 侧对 bank-channel 调用增加熔断/降级（如失败率超阈值快速失败并返回可重试提示），避免连接被慢请求长期占用
- [CLUS-01] 核查 bank-channel 侧是否存在慢查询或下游银行接口变慢导致连接归还延迟，必要时缩短其连接获取超时并开启连接泄漏检测
- [CLUS-01] 复盘 bank-channel 连接池容量规划：结合历史峰值 QPS 与单连接处理时延重新计算 max connections，并配置连接池使用率 85% 的预警阈值

### 长期治理

- 为 bank-channel 连接池建立基于历史峰值的容量模型，并设置 80%/90% 两级水位告警，避免再次出现 99% 打满
- 在 payment-svc -> bank-channel 调用链路上补齐熔断、限流与超时（当前超时 25s 偏长，建议按 P99 时延下调），防止下游慢导致上游连接池连锁打满
- 对 gateway/order-svc/payment-svc 的连接池使用率做统一监控与容量评审，明确各服务连接池上限与实例数的匹配关系
- 补充链路级健康度看板（如 BIZ-06 链路成功率），使最弱环节可在故障早期被定位，而不必等到 gateway 错误预算被大量消耗

## 六、稳定性趋势

**对比对象**：`run-20260925-100000-S0`（2026-09-26T03:19+08:00）

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

## 七、附录

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
| 数据采集 | 成功 | 637ms | file 通道采集 215676 个点 / 150 条时序（634ms） |
| 标准化清洗 | 成功 | 2464ms | 216000 个有效点 / 150 条时序；缺失率 0.00%，置信度 高 |
| 结构化存储 | 成功 | 142ms | 数据集 S1 就绪（216000 个指标点） |
| 动态基线 | 成功 | 1567ms | 计算 150 条基线，动态基线覆盖率 100%（归档 603894 点） |
| 规则巡检 | 成功 | 114ms | 16 条规则命中 59 项原始发现（114ms） |
| 聚合去重 | 成功 | 3ms | 59 条异常（抑制 9 条）聚成 1 个根因簇 |
| 稳定性评分 | 成功 | 0ms | 规则算分 20.4（E 严重） |
| AI 智能分析 | 成功 | 8194ms | deepseek/deepseek-chat；1 条根因结论，token 20315，耗时 8183ms |
| 报告输出 | 成功 | 13ms | Markdown 报告 13191 字符，已写入 run-20260925-100000-S1.md |

- AI 分析：deepseek/deepseek-chat；调用 1 次，耗时 8183ms，token 20315，提示词版本 v1
