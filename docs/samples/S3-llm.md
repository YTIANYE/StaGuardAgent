# 业务稳定性巡检报告

> 巡检编号 `run-20260925-100000-S3`　|　场景 `S3`　|　数据源 `file`　|　状态 `成功`

## 一、巡检概览

- **巡检窗口**：2026-09-25 09:30~10:00（30分钟），粒度 60s
- **覆盖范围**：8 个服务 / 15 个实例
- **执行耗时**：12.0s

### 稳定性评分

| 评分 | 等级 | 异常分布 |
| --- | --- | --- |
| **45.0 / 100** | E 严重 | 🔴 P1 紧急: 3　🟠 P2 严重: 15　🟡 P3 一般: 15　🔵 P4 轻微: 12 |

### 扣分明细

| 维度 | 权重 | 得分 | 扣分 | 依据 |
| --- | ---: | ---: | ---: | --- |
| 可用性 | 40 | 40 | 0 | 全部服务成功率与业务错误率均在 SLO 之内，错误预算未消耗 |
| 性能 | 25 | 25 | 0 | 本维度未发现异常 |
| 资源 | 20 | 0 | 20 | 30 条异常、影响 15 个实例（占全量 100%），最严重为 order-svc/order-svc-0 CPU 使用率（P1 紧急，幅度 1.00） |
| 稳定性 | 15 | 1.7 | 13.3 | 15 条异常、影响 15 个实例（占全量 100%），最严重为 bank-channel/bank-channel-0 QPS（P2 严重，幅度 0.84） |

> **封顶生效**：紧急（P1）异常命中核心链路服务，故障会直接放大到用户侧，封顶 45

> 评分为规则算分（逐项依据见上表），AI 未调整分数。

## 二、风险总结

本次巡检在 09:46 起观测到全链路 8 个服务、15 个实例的 QPS 同向同幅度突增（约 2.6~3.4 倍基线），并伴随 CPU、连接池水位普遍越线，属于典型流量驱动型压力事件。触发机制是流量波动，最先失守的环节是 order-svc（CPU 99% 持续 21 分钟）与 bank-channel/mysql-order 的连接池打满（99%）。当前成功率与业务错误率仍在 SLO 内，但连接池已接近拒绝新请求的临界点，需立即扩容与限流保护。

- **趋势判断**：持续恶化
  - 依据：QPS 异常自 09:46 起持续，order-svc CPU 已连续 21 分钟维持 99%，bank-channel 连接池 99% 持续 14 分钟、mysql-order 连接池 99% 持续 11 分钟，且各指标 series 的 last 值仍处高位（如 bank-channel conn_usage last=98.4、mysql-order conn_usage last=99.0），未见回落迹象。
  - 预测：若流量维持当前水平，预计 15~30 分钟内 bank-channel 与 mysql-order 连接池将出现请求拒绝，payment-svc 成功率随之下降并向上游 order-svc、gateway 传导；order-svc CPU 已打满，进一步流量增长将直接导致其响应时间上升与超时。
- **证据不足之处**：
  - 流量突增的外部触发源（如大促活动、客户端重试、负载均衡策略变化）无法从证据包中确认，仅能观测到全链路 QPS 同向同幅度上升。
  - CHG-20260925-0062（order-svc 扩容，76 分钟前）不在异常起始前 30 分钟窗口内，且描述为「实际生效 3 实例」，无法确认其是否与本次流量突增有直接因果关系，仅作为背景信息。
  - rule_hypothesis 认为 order-svc 的 QPS 异常沿依赖链向上游传导，但证据显示各服务 QPS 几乎同时突增（gateway 持续 21~23 分钟、order-svc 持续 4~23 分钟），更符合全链路同源流量上升，而非单一服务向下游传导，故未采纳该假设。
  - 各服务成功率与业务错误率仍在 SLO 内，无法从证据判断连接池打满是否已造成实际请求拒绝或超时。

## 三、异常清单

| 级别 | 服务 / 实例 | 指标 | 实测 | 基线 | 偏离 | 持续 | 起始 | 级别理由 |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 🔴 P1 紧急 | order-svc / order-svc-0 | CPU 使用率 | 99 | 54.96 | +80.43% | 21分钟 | 09:37 | CPU 使用率 99.00%，持续 21 分钟接近打满 |
| 🔴 P1 紧急 | bank-channel / bank-channel-0 | 连接池使用率 | 99 | 63.21 | +58.16% | 14分钟 | 09:38 | 连接池使用率 99.00%，接近打满，新请求将被拒绝 |
| 🔴 P1 紧急 | mysql-order / mysql-order-0 | 连接池使用率 | 99 | 61.18 | +63.91% | 11分钟 | 09:49 | 连接池使用率 99.00%，接近打满，新请求将被拒绝 |
| 🟠 P2 严重 | order-svc / order-svc-1 | CPU 使用率 | 99 | 51.25 | +95.29% | 18分钟 | 09:40 | CPU 使用率 99.00%，持续 18 分钟处于高位 |
| 🟠 P2 严重 | mysql-order / mysql-order-0 | CPU 使用率 | 99 | 57.05 | +75.50% | 25分钟 | 09:35 | CPU 使用率 99.00%，持续 25 分钟处于高位 |
| 🟠 P2 严重 | bank-channel / bank-channel-0 | QPS | 1735.84 | 539.46 | +222.60% | 10分钟 | 09:39 | QPS 1735.8 为基线 539.5 的 3.22 倍 |
| 🟠 P2 严重 | order-svc / order-svc-2 | CPU 使用率 | 99 | 48.88 | +102.43% | 7分钟 | 09:39 | CPU 使用率 99.00%，持续 7 分钟处于高位 |
| 🟠 P2 严重 | payment-svc / payment-svc-1 | QPS | 1924.42 | 588.75 | +228.95% | 6分钟 | 09:54 | QPS 1924.4 为基线 588.7 的 3.27 倍 |
| 🟠 P2 严重 | payment-svc / payment-svc-1 | 连接池使用率 | 99 | 57.27 | +72.30% | 6分钟 | 09:37 | 连接池使用率 99.00%，持续高位 |
| 🟠 P2 严重 | order-svc / order-svc-0 | QPS | 2349.75 | 714.73 | +229.20% | 5分钟 | 09:53 | QPS 2349.8 为基线 714.7 的 3.29 倍 |
| 🟠 P2 严重 | payment-svc / payment-svc-0 | QPS | 1889.39 | 574.17 | +230.56% | 5分钟 | 09:53 | QPS 1889.4 为基线 574.2 的 3.29 倍 |
| 🟠 P2 严重 | inventory-svc / inventory-svc-1 | QPS | 1641.25 | 503.58 | +222.99% | 6分钟 | 09:35 | QPS 1641.2 为基线 503.6 的 3.26 倍 |
| 🟠 P2 严重 | order-svc / order-svc-1 | QPS | 2449.17 | 756.44 | +225.04% | 4分钟 | 09:46 | QPS 2449.2 为基线 756.4 的 3.24 倍 |
| 🟠 P2 严重 | payment-svc / payment-svc-1 | CPU 使用率 | 98.22 | 47.65 | +106.72% | 3分钟 | 09:41 | CPU 使用率 98.22%，持续 3 分钟处于高位 |
| 🟠 P2 严重 | inventory-svc / inventory-svc-0 | QPS | 1684.85 | 507.88 | +234.51% | 4分钟 | 09:56 | QPS 1684.8 为基线 507.9 的 3.32 倍 |
| 🟠 P2 严重 | order-svc / order-svc-0 | 连接池使用率 | 95.84 | 53.78 | +79.85% | 2分钟 | 09:42 | 连接池使用率 95.84%，持续高位 |
| 🟠 P2 严重 | order-svc / order-svc-1 | 连接池使用率 | 94.56 | 53.94 | +72.93% | 2分钟 | 09:42 | 连接池使用率 94.56%，持续高位 |
| 🟠 P2 严重 | mysql-order / mysql-order-0 | QPS | 3800.35 | 1132.59 | +242.09% | 3分钟 | 09:40 | QPS 3800.3 为基线 1132.6 的 3.36 倍 |
| 🟡 P3 一般 | gateway / gateway-0 | QPS | 2712.03 | 1023.76 | +164.60% | 23分钟 | 09:36 | QPS 2712.0 较基线翻倍（2.65 倍） |
| 🟡 P3 一般 | gateway / gateway-1 | QPS | 2474.05 | 944.94 | +164.73% | 21分钟 | 09:37 | QPS 2474.0 较基线翻倍（2.62 倍） |
| 🟡 P3 一般 | gateway / gateway-2 | QPS | 2789.49 | 1049.65 | +166.43% | 21分钟 | 09:37 | QPS 2789.5 较基线翻倍（2.66 倍） |
| 🟡 P3 一般 | order-svc / order-svc-2 | QPS | 2578.30 | 792.89 | +229.13% | 23分钟 | 09:36 | QPS 2578.3 较基线翻倍（3.25 倍） |
| 🟡 P3 一般 | user-svc / user-svc-0 | QPS | 971.99 | 375.62 | +158.82% | 25分钟 | 09:35 | QPS 972.0 较基线翻倍（2.59 倍） |
| 🟡 P3 一般 | user-svc / user-svc-1 | QPS | 1029.13 | 375.89 | +172.98% | 25分钟 | 09:35 | QPS 1029.1 较基线翻倍（2.74 倍） |
| 🟡 P3 一般 | redis-session / redis-session-0 | QPS | 2086.38 | 774.17 | +169.95% | 25分钟 | 09:35 | QPS 2086.4 较基线翻倍（2.69 倍） |
| 🟡 P3 一般 | payment-svc / payment-svc-0 | 连接池使用率 | 89.76 | 52.76 | +69.28% | 10分钟 | 09:40 | 连接池使用率 89.76% 超出 90% 预警线 |
| 🟡 P3 一般 | payment-svc / payment-svc-0 | CPU 使用率 | 91.29 | 43.23 | +112.49% | 2分钟 | 09:42 | CPU 使用率 91.29% 超出 85% 预警线 |
| 🟡 P3 一般 | gateway / gateway-1 | CPU 使用率 | 85.03 | 49.86 | +71.80% | 1分钟 | 09:51 | CPU 使用率 85.03% 超出 85% 预警线 |
| 🟡 P3 一般 | gateway / gateway-2 | CPU 使用率 | 85.39 | 47.42 | +82.47% | 1分钟 | 09:43 | CPU 使用率 85.39% 超出 85% 预警线 |
| 🟡 P3 一般 | inventory-svc / inventory-svc-1 | CPU 使用率 | 88.19 | 43.34 | +102.83% | 2分钟 | 09:47 | CPU 使用率 88.19% 超出 85% 预警线 |
| 🟡 P3 一般 | inventory-svc / inventory-svc-1 | 连接池使用率 | 86.92 | 48.98 | +76.48% | 2分钟 | 09:45 | 连接池使用率 86.92% 超出 90% 预警线 |
| 🟡 P3 一般 | order-svc / order-svc-2 | 连接池使用率 | 88.22 | 51.33 | +73.21% | 3分钟 | 09:41 | 连接池使用率 88.22% 超出 90% 预警线 |
| 🟡 P3 一般 | inventory-svc / inventory-svc-0 | CPU 使用率 | 94.07 | 42.51 | +122.76% | 1分钟 | 09:40 | CPU 使用率 94.07% 超出 85% 预警线 |
| 🔵 P4 轻微 | gateway / gateway-0 | CPU 使用率 | 82.78 | 47.14 | +77.55% | 23分钟 | 09:36 | CPU 使用率 82.78% 显著偏离基线 47.14% |
| 🔵 P4 轻微 | bank-channel / bank-channel-0 | CPU 使用率 | 53.09 | 26.21 | +104.41% | 25分钟 | 09:35 | CPU 使用率 53.09% 显著偏离基线 26.21% |
| 🔵 P4 轻微 | gateway / gateway-0 | 连接池使用率 | 72.70 | 46.54 | +57.21% | 22分钟 | 09:36 | 连接池使用率 72.70% 显著偏离基线 46.54% |
| 🔵 P4 轻微 | inventory-svc / inventory-svc-0 | 连接池使用率 | 73.36 | 43.06 | +70.96% | 25分钟 | 09:35 | 连接池使用率 73.36% 显著偏离基线 43.06% |
| 🔵 P4 轻微 | gateway / gateway-1 | 连接池使用率 | 77.14 | 47.48 | +63.29% | 13分钟 | 09:37 | 连接池使用率 77.14% 显著偏离基线 47.48% |
| 🔵 P4 轻微 | gateway / gateway-2 | 连接池使用率 | 73.45 | 47.94 | +52.73% | 13分钟 | 09:41 | 连接池使用率 73.45% 显著偏离基线 47.94% |
| 🔵 P4 轻微 | user-svc / user-svc-0 | CPU 使用率 | 66.04 | 35.42 | +86.93% | 25分钟 | 09:35 | CPU 使用率 66.04% 显著偏离基线 35.42% |
| 🔵 P4 轻微 | user-svc / user-svc-1 | CPU 使用率 | 60.30 | 34.48 | +76.51% | 25分钟 | 09:35 | CPU 使用率 60.30% 显著偏离基线 34.48% |
| 🔵 P4 轻微 | redis-session / redis-session-0 | CPU 使用率 | 39.38 | 21.22 | +87.10% | 25分钟 | 09:35 | CPU 使用率 39.38% 显著偏离基线 21.22% |
| 🔵 P4 轻微 | user-svc / user-svc-0 | 连接池使用率 | 59.41 | 39.81 | +50.39% | 25分钟 | 09:35 | 连接池使用率 59.41% 显著偏离基线 39.81% |
| 🔵 P4 轻微 | user-svc / user-svc-1 | 连接池使用率 | 57.31 | 38.03 | +52.72% | 25分钟 | 09:35 | 连接池使用率 57.31% 显著偏离基线 38.03% |
| 🔵 P4 轻微 | redis-session / redis-session-0 | 连接池使用率 | 49.52 | 32.08 | +53.47% | 23分钟 | 09:37 | 连接池使用率 49.52% 显著偏离基线 32.08% |

## 四、多粒度巡检统计

> 统计对象为**未抑制异常**；影响面 = 该维度下出现异常的实例数 / 实例总数。本次覆盖 8 个服务（其中 8 个有异常）、5 个集群。

### 服务维度

| 服务 | 集群 | 异常数 | 最严重 | 受影响实例 | 影响面 | 主要指标 | 命中规则 |
| --- | --- | ---: | --- | --- | ---: | --- | --- |
| order-svc | 交易集群 | 9 | 🔴 P1 紧急 | order-svc-0、order-svc-1、order-svc-2（3 / 3） | 100% | CPU 使用率 | BIZ-03、RES-01、RES-04 |
| bank-channel | 外部依赖集群 | 3 | 🔴 P1 紧急 | bank-channel-0（1 / 1） | 100% | 连接池使用率 | BIZ-03、RES-01、RES-04 |
| mysql-order | 外部依赖集群 | 3 | 🔴 P1 紧急 | mysql-order-0（1 / 1） | 100% | 连接池使用率 | BIZ-03、RES-01、RES-04 |
| inventory-svc | 供应链集群 | 6 | 🟠 P2 严重 | inventory-svc-0、inventory-svc-1（2 / 2） | 100% | QPS | BIZ-03、RES-01、RES-04 |
| payment-svc | 支付集群 | 6 | 🟠 P2 严重 | payment-svc-0、payment-svc-1（2 / 2） | 100% | QPS | BIZ-03、RES-01、RES-04 |
| gateway | 交易集群 | 9 | 🟡 P3 一般 | gateway-0、gateway-1、gateway-2（3 / 3） | 100% | QPS | BIZ-03、RES-01、RES-04 |
| user-svc | 用户集群 | 6 | 🟡 P3 一般 | user-svc-0、user-svc-1（2 / 2） | 100% | QPS | BIZ-03、RES-01、RES-04 |
| redis-session | 外部依赖集群 | 3 | 🟡 P3 一般 | redis-session-0（1 / 1） | 100% | QPS | BIZ-03、RES-01、RES-04 |

### 集群维度

| 集群 | 服务数 | 异常服务 | 异常数 | 最严重 | 受影响实例 | 影响面 | 根因簇 |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| 交易集群 | 2 | 2 | 18 | 🔴 P1 紧急 | 6 / 6 | 100% | 1 |
| 外部依赖集群 | 3 | 3 | 9 | 🔴 P1 紧急 | 3 / 3 | 100% | 0 |
| 支付集群 | 1 | 1 | 6 | 🟠 P2 严重 | 2 / 2 | 100% | 0 |
| 供应链集群 | 1 | 1 | 6 | 🟠 P2 严重 | 2 / 2 | 100% | 0 |
| 用户集群 | 1 | 1 | 6 | 🟡 P3 一般 | 2 / 2 | 100% | 0 |

## 五、根因分析

> 本次 45 条未抑制异常全部归入 1 个根因簇，逐簇给出根因。

### CLUS-01　order-svc　[P1 紧急]

- **信号规模**：45 条异常，涉及 8 个服务（bank-channel、gateway、inventory-svc、mysql-order、order-svc、payment-svc、redis-session、user-svc）
- **传播路径**：`order-svc → payment-svc → bank-channel`　（数据流方向，故障影响由此向上游扩散）
- **规则假设**：order-svc 的 QPS 异常沿依赖链向上游传导，已影响 4 个上游服务（bank-channel、inventory-svc、mysql-order、payment-svc），建议优先排查 order-svc
- **AI 根因**：**流量波动** — 全链路 QPS 同向同幅度突增（gateway/order-svc/payment-svc/inventory-svc/user-svc/bank-channel/mysql-order/redis-session 普遍达基线 2.6~3.4 倍），把各服务 CPU 与连接池推到打满，order-svc 因 CPU 最先饱和而成为最先失守的环节。（置信度 88%）
- **AI 认定的根因服务**：`order-svc`
- **影响面**：全链路 8 个服务、15 个实例受影响：order-svc 三实例 CPU 均达 99%（order-svc-0 持续 21 分钟），bank-channel 与 mysql-order 连接池使用率 99% 已接近拒绝新请求，payment-svc/inventory-svc/gateway/user-svc 的 CPU 与连接池同步抬升。当前成功率与业务错误率仍在 SLO 内，但连接池打满后若流量继续维持，将出现请求排队与超时，进而向 gateway 上游扩散。
- **建议责任方**：交易平台组（order-svc/gateway）、支付组（payment-svc/bank-channel）、DBA（mysql-order）

<details><summary>证据引用（22 条）</summary>

- `BIZ-03:order-svc/order-svc-1/qps`
- `BIZ-03:order-svc/order-svc-0/qps`
- `BIZ-03:order-svc/order-svc-2/qps`
- `BIZ-03:payment-svc/payment-svc-1/qps`
- `BIZ-03:payment-svc/payment-svc-0/qps`
- `BIZ-03:inventory-svc/inventory-svc-1/qps`
- `BIZ-03:inventory-svc/inventory-svc-0/qps`
- `BIZ-03:bank-channel/bank-channel-0/qps`
- `BIZ-03:mysql-order/mysql-order-0/qps`
- `BIZ-03:gateway/gateway-0/qps`
- `BIZ-03:gateway/gateway-1/qps`
- `BIZ-03:gateway/gateway-2/qps`
- …其余 10 条同类证据（见 JSON 报告）

</details>

## 六、修复建议

### 立即止血（本次窗口内）

- **CLUS-01 · 流量波动**
  - 立即对 order-svc 执行扩容（当前实际生效 3 实例，CHG-20260925-0062 计划扩至 6 实例但未生效，需确认并补齐），并临时将 gateway 入口对 order-svc 的并发限流下调至安全水位。

### 短期加固（本周内）

- [CLUS-01] 立即核查 bank-channel 与 mysql-order 连接池上限，评估在 3 倍流量下是否需要临时上调 max_connections，并确认连接池等待超时配置，避免新请求被直接拒绝。
- [CLUS-01] 短期：为 order-svc、payment-svc、inventory-svc 配置基于 QPS 的自动扩缩容（HPA），触发阈值设为基线 1.8 倍并持续 3 分钟。
- [CLUS-01] 短期：在 gateway 层增加全链路 QPS 突增的熔断/降级预案，当入口 QPS 超过基线 2.5 倍时对非核心接口（如用户信息查询）降级。

### 长期治理

- 为 order-svc、payment-svc、inventory-svc 建立基于 QPS 与 CPU 双指标的自动扩缩容策略，避免大促前扩容未生效（CHG-20260925-0062 实际生效 3 实例）的情况再次发生。
- 对 bank-channel、mysql-order 等下游依赖做连接池容量规划，按峰值 QPS 的 1.5 倍预留连接数，并配置连接池等待队列与快速失败策略。
- 在 gateway 层建立全链路流量突增的自动限流与降级机制，按服务维度设置 QPS 阈值，超过阈值时优先保护核心交易链路。
- 定期演练大促流量场景，验证扩容、限流、降级预案在真实流量下的生效情况。

## 七、稳定性趋势

**对比对象**：`run-20260925-100000-S2`（2026-09-27T03:16+08:00）

- 评分变化：**+0.0 分**（与上次持平）
- 稳定性评分较上次基本持平 0.0 分；新增异常 31 项、已恢复 22 项、持续未解决 14 项；持续未解决的项需要确认是否有人在跟进。

**新增异常（31）**

- [P2 严重] bank-channel/bank-channel-0 QPS
- [P3 一般] gateway/gateway-0 QPS
- [P3 一般] gateway/gateway-1 QPS
- [P3 一般] gateway/gateway-2 QPS
- [P2 严重] inventory-svc/inventory-svc-0 QPS
- [P2 严重] inventory-svc/inventory-svc-1 QPS
- [P2 严重] mysql-order/mysql-order-0 QPS
- [P2 严重] order-svc/order-svc-0 QPS
- [P2 严重] order-svc/order-svc-1 QPS
- [P3 一般] order-svc/order-svc-2 QPS
- [P2 严重] payment-svc/payment-svc-0 QPS
- [P2 严重] payment-svc/payment-svc-1 QPS

**持续未解决（14）**

- [P4 轻微] gateway/gateway-0 CPU 使用率
- [P3 一般] gateway/gateway-1 CPU 使用率
- [P3 一般] gateway/gateway-2 CPU 使用率
- [P3 一般] inventory-svc/inventory-svc-1 CPU 使用率
- [P1 紧急] order-svc/order-svc-0 CPU 使用率
- [P2 严重] order-svc/order-svc-1 CPU 使用率
- [P2 严重] order-svc/order-svc-2 CPU 使用率
- [P4 轻微] gateway/gateway-0 连接池使用率
- [P4 轻微] gateway/gateway-1 连接池使用率
- [P4 轻微] gateway/gateway-2 连接池使用率
- [P3 一般] inventory-svc/inventory-svc-1 连接池使用率
- [P2 严重] order-svc/order-svc-0 连接池使用率

**已恢复（22）**

- gateway/gateway-0 latency_p99
- gateway/gateway-1 latency_p99
- gateway/gateway-2 latency_p99
- inventory-svc/inventory-svc-1 latency_p99
- order-svc/order-svc-0 latency_p99
- order-svc/order-svc-1 latency_p99
- order-svc/order-svc-2 latency_p99
- gateway/gateway-0 latency_p95
- gateway/gateway-1 latency_p95
- gateway/gateway-2 latency_p95
- inventory-svc/inventory-svc-1 latency_p95
- order-svc/order-svc-0 latency_p95

### 评分历史

`▄▆▃█▁▃▃`　最近 7 次：60 → 84 → 45 → 96 → 20 → 45 → 45

## 八、附录

### 数据质量

- 数据质量良好，未发现缺失或越界
- 有效样本 216000 / 应有 216000（缺失率 0.00%，插值占比 0.15%）
- 结论置信度：**高**

### 规则命中统计

| 规则 | 命中 |
| --- | ---: |
| BIZ-03 | 15 |
| RES-01 | 15 |
| RES-04 | 15 |

### 动态基线覆盖

- 动态基线覆盖率：**100%**（其余为样本不足时的静态阈值回退）

### 变更事件

| 时间 | 服务 | 类型 | 版本 | 说明 |
| --- | --- | --- | --- | --- |
| 18:00 | inventory-svc | 配置变更 | - | 库存服务限流阈值由 800 QPS 调整为 1200 QPS |
| 05:00 | gateway | 配置变更 | - | 网关健康检查间隔由 5s 调整为 3s |
| 08:30 | order-svc | 扩缩容 | - | 大促前订单服务由 3 实例扩容至 6 实例（实际生效 3 实例） |

### 执行阶段与 AI 调用

| 阶段 | 状态 | 耗时 | 说明 |
| --- | --- | ---: | --- |
| 数据采集 | 成功 | 692ms | file 通道采集 215676 个点 / 150 条时序（688ms） |
| 标准化清洗 | 成功 | 2670ms | 216000 个有效点 / 150 条时序；缺失率 0.00%，置信度 高 |
| 结构化存储 | 成功 | 143ms | 数据集 S3 就绪（216000 个指标点） |
| 动态基线 | 成功 | 1263ms | 计算 150 条基线，动态基线覆盖率 100%（归档 603894 点） |
| 规则巡检 | 成功 | 121ms | 16 条规则命中 45 项原始发现（121ms） |
| 聚合去重 | 成功 | 3ms | 45 条异常（抑制 0 条）聚成 1 个根因簇；多粒度统计 8 个服务 / 5 个集群 |
| 稳定性评分 | 成功 | 0ms | 规则算分 45.0（E 严重）；紧急（P1）异常命中核心链路服务，故障会直接放大到用户侧，封顶 45 |
| AI 智能分析 | 成功 | 7113ms | deepseek/deepseek-chat；1 条根因结论，token 19455，耗时 7105ms |
| 报告输出 | 成功 | 7ms | Markdown 报告 13711 字符，已写入 run-20260925-100000-S3.md |

- AI 分析：deepseek/deepseek-chat；调用 1 次，耗时 7105ms，token 19455，提示词版本 v2
