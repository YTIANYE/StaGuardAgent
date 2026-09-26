# 业务稳定性巡检报告

> 巡检编号 `run-20260925-100000-S5`　|　场景 `S5`　|　数据源 `file`　|　状态 `成功`

## 一、巡检概览

- **巡检窗口**：2026-09-25 09:30~10:00（30分钟），粒度 60s
- **覆盖范围**：8 个服务 / 15 个实例
- **执行耗时**：14.0s

### 稳定性评分

| 评分 | 等级 | 异常分布 |
| --- | --- | --- |
| **83.5 / 100** | B 良好 | 🔴 P1 紧急: 0　🟠 P2 严重: 15　🟡 P3 一般: 0　🔵 P4 轻微: 1 |

### 扣分明细

| 维度 | 权重 | 得分 | 扣分 | 依据 |
| --- | ---: | ---: | ---: | --- |
| 可用性 | 40 | 40 | 0 | 全部服务成功率与业务错误率均在 SLO 之内，错误预算未消耗 |
| 性能 | 25 | 25 | 0 | 本维度未发现异常 |
| 资源 | 20 | 16.8 | 3.2 | 1 条异常、影响 1 个实例（占全量 7%），最严重为 order-svc/order-svc-1 内存使用率（P4 轻微，幅度 0.20） |
| 稳定性 | 15 | 1.7 | 13.3 | 15 条异常、影响 15 个实例（占全量 100%），最严重为 gateway/gateway-0 QPS（P2 严重，幅度 0.84） |

> 评分为规则算分（逐项依据见上表），AI 未调整分数。

## 二、风险总结

本次异常为全链路 8 个服务、15 个实例的 QPS 在 09:41 起同向、同幅度下跌约 74%（gateway 1049→272、order-svc 714→183、payment-svc 574→148、bank-channel 539→140、inventory-svc 507→133、mysql-order 1132→291、user-svc 375→97、redis-session 774→202），各服务跌幅高度一致（73.5%~75.0%），且无任何服务在流量下跌前出现资源或成功率劣化，属典型流量波动（流量消失）而非某个服务故障传导。规则给出的「gateway 沿依赖链向上游传导」的猜想与拓扑方向不符——gateway 是调用方而非被调用方，且各服务跌幅一致说明是共同的外部流量源变化。唯一资源类异常为 order-svc-1 内存 61.33%（P4，幅度 0.20），与流量下跌无因果关联。

- **趋势判断**：整体平稳
  - 依据：各服务 QPS 序列 trend 多为 stable/falling，p50 已稳定在低位（如 gateway-2 p50 302.9、order-svc-0 p50 203.4），说明流量下跌已进入平台期而非持续恶化；成功率与错误率全程在 SLO 内，无资源水位越线扩散迹象。
  - 预测：未来 1 小时若外部流量源未恢复，全链路 QPS 将维持在基线 25%~30% 的低位，业务量损失持续但系统稳定性风险低；需关注 order-svc-1 内存，若按当前斜率（约 +0.06%/min 的观测幅度）外推，1 小时内升至 65% 以内，触发 OOM 概率低。
- **证据不足之处**：
  - 流量下跌的外部触发源（负载均衡、DNS、客户端行为、业务活动结束等）在证据包中不可观测，无法确认是真实业务量下降还是入口链路异常导致请求未到达。
  - 规则给出的 rule_hypothesis 认为「gateway 沿依赖链向上游传导」，但拓扑中 gateway 是调用方（gateway→order-svc/user-svc），且各服务跌幅高度一致（73.5%~75.0%），不支持单点故障传导的解释，故未采纳该猜想。
  - order-svc-1 内存异常（P4）与流量下跌的时间关系无法从证据中确认，二者可能完全独立。
  - 证据包未提供成功率、错误率、延迟等可用性指标的原始序列，无法完全排除在流量下跌同时存在局部错误率抬升。
  - changes 中两条变更（941 分钟前、281 分钟前）均远早于异常起始，不满足 30 分钟窗口，未作为根因，仅作背景。

## 三、异常清单

| 级别 | 服务 / 实例 | 指标 | 实测 | 基线 | 偏离 | 持续 | 起始 | 级别理由 |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 🟠 P2 严重 | gateway / gateway-0 | QPS | 261.63 | 1023.76 | -74.47% | 16分钟 | 09:42 | QPS 261.6 较基线 1023.8 下跌 74.47% |
| 🟠 P2 严重 | gateway / gateway-1 | QPS | 241.82 | 944.94 | -74.12% | 16分钟 | 09:42 | QPS 241.8 较基线 944.9 下跌 74.12% |
| 🟠 P2 严重 | gateway / gateway-2 | QPS | 272.18 | 1049.65 | -74.00% | 17分钟 | 09:41 | QPS 272.2 较基线 1049.6 下跌 74.00% |
| 🟠 P2 严重 | order-svc / order-svc-0 | QPS | 183.36 | 714.73 | -74.31% | 20分钟 | 09:40 | QPS 183.4 较基线 714.7 下跌 74.31% |
| 🟠 P2 严重 | order-svc / order-svc-1 | QPS | 199.82 | 756.44 | -73.48% | 20分钟 | 09:40 | QPS 199.8 较基线 756.4 下跌 73.48% |
| 🟠 P2 严重 | order-svc / order-svc-2 | QPS | 204.41 | 792.89 | -73.91% | 20分钟 | 09:40 | QPS 204.4 较基线 792.9 下跌 73.91% |
| 🟠 P2 严重 | payment-svc / payment-svc-0 | QPS | 148.15 | 574.17 | -74.08% | 20分钟 | 09:40 | QPS 148.2 较基线 574.2 下跌 74.08% |
| 🟠 P2 严重 | payment-svc / payment-svc-1 | QPS | 154.41 | 588.75 | -73.61% | 20分钟 | 09:40 | QPS 154.4 较基线 588.7 下跌 73.61% |
| 🟠 P2 严重 | bank-channel / bank-channel-0 | QPS | 139.89 | 539.46 | -74.00% | 20分钟 | 09:40 | QPS 139.9 较基线 539.5 下跌 74.00% |
| 🟠 P2 严重 | inventory-svc / inventory-svc-0 | QPS | 133.31 | 507.88 | -73.53% | 20分钟 | 09:40 | QPS 133.3 较基线 507.9 下跌 73.53% |
| 🟠 P2 严重 | inventory-svc / inventory-svc-1 | QPS | 127.00 | 503.58 | -75.01% | 20分钟 | 09:40 | QPS 127.0 较基线 503.6 下跌 75.01% |
| 🟠 P2 严重 | mysql-order / mysql-order-0 | QPS | 290.88 | 1132.59 | -73.82% | 20分钟 | 09:40 | QPS 290.9 较基线 1132.6 下跌 73.82% |
| 🟠 P2 严重 | user-svc / user-svc-0 | QPS | 96.92 | 375.62 | -74.19% | 20分钟 | 09:40 | QPS 96.9 较基线 375.6 下跌 74.19% |
| 🟠 P2 严重 | user-svc / user-svc-1 | QPS | 95.09 | 375.89 | -74.78% | 20分钟 | 09:40 | QPS 95.1 较基线 375.9 下跌 74.78% |
| 🟠 P2 严重 | redis-session / redis-session-0 | QPS | 201.66 | 774.17 | -73.91% | 20分钟 | 09:40 | QPS 201.7 较基线 774.2 下跌 73.91% |
| 🔵 P4 轻微 | order-svc / order-svc-1 | 内存使用率 | 61.33 | 59.37 | +3.25% | 1分钟 | 09:38 | 内存使用率 61.33% 显著偏离基线 59.37% |

## 四、多粒度巡检统计

> 统计对象为**未抑制异常**；影响面 = 该维度下出现异常的实例数 / 实例总数。本次覆盖 8 个服务（其中 8 个有异常）、5 个集群。

### 服务维度

| 服务 | 集群 | 异常数 | 最严重 | 受影响实例 | 影响面 | 主要指标 | 命中规则 |
| --- | --- | ---: | --- | --- | ---: | --- | --- |
| order-svc | 交易集群 | 4 | 🟠 P2 严重 | order-svc-0、order-svc-1、order-svc-2（3 / 3） | 100% | QPS | BIZ-04、RES-02 |
| gateway | 交易集群 | 3 | 🟠 P2 严重 | gateway-0、gateway-1、gateway-2（3 / 3） | 100% | QPS | BIZ-04 |
| inventory-svc | 供应链集群 | 2 | 🟠 P2 严重 | inventory-svc-0、inventory-svc-1（2 / 2） | 100% | QPS | BIZ-04 |
| payment-svc | 支付集群 | 2 | 🟠 P2 严重 | payment-svc-0、payment-svc-1（2 / 2） | 100% | QPS | BIZ-04 |
| user-svc | 用户集群 | 2 | 🟠 P2 严重 | user-svc-0、user-svc-1（2 / 2） | 100% | QPS | BIZ-04 |
| bank-channel | 外部依赖集群 | 1 | 🟠 P2 严重 | bank-channel-0（1 / 1） | 100% | QPS | BIZ-04 |
| mysql-order | 外部依赖集群 | 1 | 🟠 P2 严重 | mysql-order-0（1 / 1） | 100% | QPS | BIZ-04 |
| redis-session | 外部依赖集群 | 1 | 🟠 P2 严重 | redis-session-0（1 / 1） | 100% | QPS | BIZ-04 |

### 集群维度

| 集群 | 服务数 | 异常服务 | 异常数 | 最严重 | 受影响实例 | 影响面 | 根因簇 |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| 交易集群 | 2 | 2 | 7 | 🟠 P2 严重 | 6 / 6 | 100% | 1 |
| 外部依赖集群 | 3 | 3 | 3 | 🟠 P2 严重 | 3 / 3 | 100% | 0 |
| 支付集群 | 1 | 1 | 2 | 🟠 P2 严重 | 2 / 2 | 100% | 0 |
| 供应链集群 | 1 | 1 | 2 | 🟠 P2 严重 | 2 / 2 | 100% | 0 |
| 用户集群 | 1 | 1 | 2 | 🟠 P2 严重 | 2 / 2 | 100% | 0 |

## 五、根因分析

> 本次 16 条未抑制异常全部归入 1 个根因簇，逐簇给出根因。

### CLUS-01　gateway　[P2 严重]

- **信号规模**：16 条异常，涉及 8 个服务（bank-channel、gateway、inventory-svc、mysql-order、order-svc、payment-svc、redis-session、user-svc）
- **传播路径**：`gateway → order-svc → payment-svc → bank-channel`　（数据流方向，故障影响由此向上游扩散）
- **规则假设**：gateway 的 QPS 异常沿依赖链向上游传导，已影响 7 个上游服务（bank-channel、inventory-svc、mysql-order、order-svc、payment-svc、redis-session、user-svc），建议优先排查 gateway
- **AI 根因**：**流量波动** — 全链路 8 个服务的 QPS 在 09:41 起同向、同幅度下跌约 74%，跌幅高度一致（73.5%~75.0%），无任一服务在流量下跌前出现资源水位越线或成功率劣化，说明是入口侧整体流量消失（外部流量源变化），而非某个服务故障向下游/上游传导。（置信度 82%，**历史复发**）
- **AI 认定的根因服务**：`gateway`
- **影响面**：全链路 8 个服务、15 个实例的请求量跌至基线约 26%，交易主链路（gateway→order-svc→payment-svc→bank-channel）与旁路（user-svc→redis-session、inventory-svc→mysql-order）同步受影响；由于成功率与错误率仍在 SLO 内，本次为「业务量骤降」型影响，而非可用性故障，但若为上游流量源异常（如 LB/DNS/客户端），实际业务成交可能已受损。
- **建议责任方**：交易平台组（gateway 归属）+ 网络/入口平台协同排查流量源

<details><summary>证据引用（15 条）</summary>

- `BIZ-04:gateway/gateway-2/qps`
- `BIZ-04:gateway/gateway-0/qps`
- `BIZ-04:gateway/gateway-1/qps`
- `BIZ-04:order-svc/order-svc-0/qps`
- `BIZ-04:order-svc/order-svc-1/qps`
- `BIZ-04:order-svc/order-svc-2/qps`
- `BIZ-04:payment-svc/payment-svc-0/qps`
- `BIZ-04:payment-svc/payment-svc-1/qps`
- `BIZ-04:bank-channel/bank-channel-0/qps`
- `BIZ-04:inventory-svc/inventory-svc-0/qps`
- `BIZ-04:inventory-svc/inventory-svc-1/qps`
- `BIZ-04:mysql-order/mysql-order-0/qps`
- …其余 3 条同类证据（见 JSON 报告）

</details>

## 六、修复建议

### 立即止血（本次窗口内）

- **CLUS-01 · 流量波动**
  - 立即确认入口流量来源：核对网关上游 LB/VIP/DNS 解析与客户端侧调用量，确认是外部流量真实下降还是入口链路（LB、DNS、CDN）异常导致请求未到达 gateway。
- **CLUS-01 · 资源瓶颈**
  - 对 order-svc-1 增加堆内存与 GC 指标采集（老年代使用率、Full GC 次数），确认 61% 是稳态波动还是缓慢上涨趋势。

### 短期加固（本周内）

- [CLUS-01] 对比同时段业务订单量、支付笔数与 QPS 曲线，判断是「真实业务量下降」还是「流量被拦截/未接入」，若为后者立即切流或修复入口。
- [CLUS-01] 检查 gateway 各实例的接入连接数、TLS 握手失败数、5xx/4xx 分布，排除网关自身拒绝连接导致的流量统计下降。
- [CLUS-01] 若确认为外部流量源变化，与业务/运营确认是否有活动结束、投放停止或上游系统停调，并同步容量侧避免误判为故障扩容。
- [CLUS-01] 若后续 1 小时内内存持续单调上升，提前对该实例做滚动重启或扩容，避免触发 OOMKill。

### 长期治理

- 为 gateway 增加「入口流量骤降」专项告警（如下跌 >50% 持续 2 分钟），并与业务订单量做联动校验，避免把流量消失误判为下游故障。
- 建立全链路 QPS 同向变化的一致性检测规则：当 ≥5 个服务跌幅落在同一区间（±3%）时，直接归因为入口流量波动，减少逐服务排查成本。
- 梳理入口链路（LB/DNS/CDN/客户端）的可观测性盲区，补充入口侧请求到达量、连接建立失败率等指标，使「流量消失」可被定位到具体环节。
- 对 order-svc 各实例补齐内存/GC 细粒度监控与 OOMKill 预警，避免 P4 级内存波动在流量恢复后叠加放大。

## 七、稳定性趋势

**对比对象**：`run-20260925-100000-S4`（2026-09-26T17:37+08:00）

- 评分变化：**+23.5 分**（较上次上升）
- 稳定性评分较上次上升 23.5 分；新增异常 16 项、已恢复 10 项、持续未解决 0 项。

**新增异常（16）**

- [P2 严重] bank-channel/bank-channel-0 QPS
- [P2 严重] gateway/gateway-0 QPS
- [P2 严重] gateway/gateway-1 QPS
- [P2 严重] gateway/gateway-2 QPS
- [P2 严重] inventory-svc/inventory-svc-0 QPS
- [P2 严重] inventory-svc/inventory-svc-1 QPS
- [P2 严重] mysql-order/mysql-order-0 QPS
- [P2 严重] order-svc/order-svc-0 QPS
- [P2 严重] order-svc/order-svc-1 QPS
- [P2 严重] order-svc/order-svc-2 QPS
- [P2 严重] payment-svc/payment-svc-0 QPS
- [P2 严重] payment-svc/payment-svc-1 QPS

**已恢复（10）**

- gateway/gateway-0 success_rate
- gateway/gateway-1 success_rate
- gateway/gateway-2 success_rate
- mysql-order/mysql-order-0 success_rate
- user-svc/user-svc-0 success_rate
- user-svc/user-svc-1 success_rate
- user-svc/user-svc-0 business_error_rate
- user-svc/user-svc-1 business_error_rate
- gateway/user-svc success_rate
- user-svc/user-svc success_rate

### 评分历史

`▃█▁▃▃▄▆`　最近 7 次：45 → 96 → 20 → 45 → 45 → 60 → 84

## 八、附录

### 数据质量

- 数据质量良好，未发现缺失或越界
- 有效样本 216000 / 应有 216000（缺失率 0.00%，插值占比 0.15%）
- 结论置信度：**高**

### 规则命中统计

| 规则 | 命中 |
| --- | ---: |
| BIZ-04 | 15 |
| RES-02 | 1 |

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
| 数据采集 | 成功 | 742ms | file 通道采集 215676 个点 / 150 条时序（738ms） |
| 标准化清洗 | 成功 | 2681ms | 216000 个有效点 / 150 条时序；缺失率 0.00%，置信度 高 |
| 结构化存储 | 成功 | 142ms | 数据集 S5 就绪（216000 个指标点） |
| 动态基线 | 成功 | 1285ms | 计算 150 条基线，动态基线覆盖率 100%（归档 603894 点） |
| 规则巡检 | 成功 | 111ms | 16 条规则命中 16 项原始发现（111ms） |
| 聚合去重 | 成功 | 2ms | 16 条异常（抑制 0 条）聚成 1 个根因簇；多粒度统计 8 个服务 / 5 个集群 |
| 稳定性评分 | 成功 | 0ms | 规则算分 83.5（B 良好） |
| AI 智能分析 | 成功 | 9063ms | deepseek/deepseek-chat；2 条根因结论，token 11027，耗时 9059ms |
| 报告输出 | 成功 | 6ms | Markdown 报告 9532 字符，已写入 run-20260925-100000-S5.md |

- AI 分析：deepseek/deepseek-chat；调用 1 次，耗时 9059ms，token 11027，提示词版本 v1
