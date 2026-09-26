# 业务稳定性巡检报告

> 巡检编号 `run-20260925-100000-S3`　|　场景 `S3`　|　数据源 `file`　|　状态 `成功`

## 一、巡检概览

- **巡检窗口**：2026-09-25 09:30~10:00（30分钟），粒度 60s
- **覆盖范围**：8 个服务 / 15 个实例
- **执行耗时**：14.2s

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

本次异常是典型的全链路流量突增：8 个服务的 QPS 在 09:46 前后同向、同幅度（约 2.6~3.4 倍）上涨，随后 order-svc、payment-svc、bank-channel、mysql-order、inventory-svc 等下游服务的 CPU 与连接池水位被推至 99% 打满。规则给出的 45 分（E 级）主要来自资源维度扣分，但可用性与性能维度均未越线，说明当前尚未造成业务损失，属于「容量被流量吃满」而非「服务故障」。根因机制是流量波动，最先扛不住的是 order-svc（CPU 99% 持续 21 分钟）与 bank-channel（连接池 99%）。

- **趋势判断**：整体平稳
  - 依据：所有异常指标的 series.trend 均为 stable，QPS 在观测窗口内维持高位但未继续攀升，CPU/连接池水位已在高位平台期（order-svc CPU p50=99、bank-channel conn p50=97.7），未出现进一步恶化。可用性与性能维度扣分为 0，说明尚未发生业务损失。
  - 预测：若流量维持当前水平，order-svc CPU 将持续 99% 打满，bank-channel/mysql-order 连接池 99% 打满状态下新请求被拒绝的概率随时间线性上升；预计 30~60 分钟内可能出现成功率跌破 SLO 与 P95 延时飙升（参考历史同类事件 run-20260925-100000-S1 的传导路径）。若流量自然回落，各服务水位将在 10~15 分钟内恢复至基线。
- **证据不足之处**：
  - 流量突增的外部触发源未知（是否为营销活动、客户端重试风暴、负载均衡策略变更等），证据包中无相关记录，无法确认。
  - CHG-20260925-0062（order-svc 扩容 3→6 实例，实际生效 3 实例）发生在异常起始前 76 分钟，超出 30 分钟窗口，不作为根因；但其「实际生效 3 实例」可能是 order-svc CPU 最先打满的放大因素，需进一步确认扩容未生效的原因。
  - 无法区分 order-svc CPU 99% 是「自身处理能力不足」还是「其下游 payment-svc/bank-channel 响应变慢导致请求堆积」——证据包中 payment-svc/bank-channel 的延时指标未在异常列表中，无法判断。
  - 规则 rule_hypothesis 认为「order-svc QPS 异常沿依赖链向上游传导」，但证据显示 gateway、user-svc、redis-session 等与 order-svc 无直接依赖关系的服务 QPS 也同幅度上涨，说明这是全链路同向流量变化而非 order-svc 单点传导，本结论与规则猜想不一致。
  - bank-channel 作为拓扑最下游（无更下游依赖），其连接池打满无法区分是自身能力不足还是其外部银行侧依赖异常，证据包中无 bank-channel 的下游指标。

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
- **AI 根因**：**流量波动** — 全链路 8 个服务的 QPS 在 09:46 前后同向、同幅度上涨至基线的 2.6~3.4 倍（gateway 2.65x、order-svc 3.24x、payment-svc 3.27x、bank-channel 3.22x、inventory-svc 3.26x、mysql-order 3.36x、user-svc 2.59x、redis-session 2.69x），流量是触发机制；order-svc 因处于交易主链路且实例数不足（扩容未生效）最先被打满 CPU 99%。（置信度 82%，**历史复发**）
- **AI 认定的根因服务**：`order-svc`
- **影响面**：全链路 15 个实例（100%）受影响。order-svc 三实例 CPU 均达 99%（最长 21 分钟），bank-channel 与 mysql-order 连接池打满至 99%，payment-svc、inventory-svc、gateway 的 CPU/连接池水位同步升高。当前成功率与业务错误率仍在 SLO 内，但连接池打满意味着新请求将被拒绝，若流量继续或持续，将很快出现成功率下跌与 P95 延时飙升（参考历史 run-20260925-100000-S1 的传导路径）。
- **建议责任方**：交易平台组（order-svc/gateway）、支付组（payment-svc/bank-channel）、DBA（mysql-order）、供应链组（inventory-svc）

<details><summary>证据引用（22 条）</summary>

- `BIZ-03:order-svc/order-svc-1/qps`
- `BIZ-03:order-svc/order-svc-0/qps`
- `BIZ-03:order-svc/order-svc-2/qps`
- `BIZ-03:payment-svc/payment-svc-1/qps`
- `BIZ-03:payment-svc/payment-svc-0/qps`
- `BIZ-03:bank-channel/bank-channel-0/qps`
- `BIZ-03:inventory-svc/inventory-svc-1/qps`
- `BIZ-03:inventory-svc/inventory-svc-0/qps`
- `BIZ-03:mysql-order/mysql-order-0/qps`
- `BIZ-03:gateway/gateway-0/qps`
- `BIZ-03:gateway/gateway-1/qps`
- `BIZ-03:gateway/gateway-2/qps`
- …其余 10 条同类证据（见 JSON 报告）

</details>

## 六、修复建议

### 立即止血（本次窗口内）

- **CLUS-01 · 流量波动**
  - 立即：确认 order-svc 大促扩容（CHG-20260925-0062）为何「实际生效 3 实例」而非 6 实例，修复扩容生效链路并补齐至 6 实例，缓解 CPU 99% 压力。

### 短期加固（本周内）

- [CLUS-01] 立即：对 bank-channel、mysql-order 连接池使用率 99% 的实例做限流/排队保护，避免新请求被直接拒绝导致成功率下跌。
- [CLUS-01] 短期：核对 inventory-svc 限流阈值（CHG-20260923-0047 已由 800 调至 1200 QPS）与当前实际 QPS 1641 的关系，确认限流是否已触发或即将触发。
- [CLUS-01] 短期：为 order-svc、payment-svc、bank-channel 配置基于 QPS 的自动扩缩容（HPA），阈值设为基线 1.5 倍，避免每次流量脉冲都靠人工扩容。
- [CLUS-01] 短期：在 gateway 层增加全链路流量整形/削峰，防止突发流量直接穿透到 order-svc 与 bank-channel。

### 长期治理

- 为交易主链路（gateway → order-svc → payment-svc → bank-channel）建立基于 QPS 的自动扩缩容策略，扩容阈值设为基线 1.5 倍，缩容冷却期不低于 10 分钟，避免流量脉冲打满。
- 对 bank-channel、mysql-order 等连接池敏感的下游依赖，评估连接池上限是否与上游峰值 QPS 匹配，必要时提升上限或引入请求排队+快速失败机制。
- 在 gateway 层部署全链路流量整形与熔断，对超过基线 2 倍的流量做削峰或降级，保护下游 order-svc 与 bank-channel。
- 修复扩容生效链路：CHG-20260925-0062 声明扩容至 6 实例但实际生效 3 实例，需排查编排/调度层为何未生效，并加入扩容生效校验告警。
- 针对历史同类事件（run-20260925-100000-S1/S2）建立流量突增场景的预案演练，明确各服务的降级顺序与限流阈值。

## 七、稳定性趋势

**对比对象**：`run-20260925-100000-S2`（2026-09-26T17:36+08:00）

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
| 数据采集 | 成功 | 647ms | file 通道采集 215676 个点 / 150 条时序（643ms） |
| 标准化清洗 | 成功 | 2630ms | 216000 个有效点 / 150 条时序；缺失率 0.00%，置信度 高 |
| 结构化存储 | 成功 | 145ms | 数据集 S3 就绪（216000 个指标点） |
| 动态基线 | 成功 | 950ms | 计算 150 条基线，动态基线覆盖率 100%（归档 603894 点） |
| 规则巡检 | 成功 | 115ms | 16 条规则命中 45 项原始发现（115ms） |
| 聚合去重 | 成功 | 3ms | 45 条异常（抑制 0 条）聚成 1 个根因簇；多粒度统计 8 个服务 / 5 个集群 |
| 稳定性评分 | 成功 | 0ms | 规则算分 45.0（E 严重）；紧急（P1）异常命中核心链路服务，故障会直接放大到用户侧，封顶 45 |
| AI 智能分析 | 成功 | 9695ms | deepseek/deepseek-chat；1 条根因结论，token 20282，耗时 9678ms |
| 报告输出 | 成功 | 13ms | Markdown 报告 14277 字符，已写入 run-20260925-100000-S3.md |

- AI 分析：deepseek/deepseek-chat；调用 1 次，耗时 9678ms，token 20282，提示词版本 v1
