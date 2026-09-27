# 业务稳定性巡检报告

> 巡检编号 `run-20260925-100000-S5`　|　场景 `S5`　|　数据源 `file`　|　状态 `成功`

## 一、巡检概览

- **巡检窗口**：2026-09-25 09:30~10:00（30分钟），粒度 60s
- **覆盖范围**：8 个服务 / 15 个实例
- **执行耗时**：12.3s

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

本次异常表现为全链路 8 个服务、15 个实例的 QPS 同向同幅度下跌约 74%，跌幅高度一致（-73.5%~-75.0%），且各服务成功率、错误率均在 SLO 之内，属于典型的入口流量整体消失而非某个服务故障传导。唯一资源类异常是 order-svc-1 内存使用率 61.33%（P4 轻微，仅 1 分钟），不足以解释全链路流量塌陷。规则给出的「gateway 沿依赖链向上游传导」的猜想与拓扑方向相反，实际是入口流量下降后逐级向下游传导。

- **趋势判断**：趋于好转
  - 依据：各实例 series 显示 QPS 在窗口内已从最低点（如 gateway-2 min 272、last 802；order-svc-0 last 199 但 p50 203）趋于稳定，gateway 三个实例 last 值（802/791/717）明显高于 min 值，说明流量在窗口后段已有回升迹象；且成功率与错误率全程未越线，无资源水位持续恶化。
  - 预测：若外部流量源恢复，QPS 预计在 1 小时内回到基线 1000 req/s 附近；当前无资源水位逼近上限（order-svc-1 内存 61%，距 OOM 尚远），未来 1 小时无 OOM 或级联故障风险。需关注的是流量恢复瞬间的突增冲击，建议确认 gateway 与 order-svc 的连接池/线程池上限能否承接从 260 到 1000+ 的阶跃回升。
- **证据不足之处**：
  - 证据包中没有任何成功率、错误率、延时或连接池水位的异常证据，无法判断流量下跌期间是否存在被掩盖的局部错误。
  - 无法从证据确认流量消失的外部触发源（负载均衡切换、DNS 变更、客户端行为、上游业务方停量等），证据包中两条变更（inventory-svc 限流阈值调整 941 分钟前、gateway 健康检查间隔调整 281 分钟前）均远早于异常起始，不构成 change_induced 根因。
  - 规则给出的 propagation_path（gateway→order-svc→payment-svc→bank-channel）方向与拓扑调用方向相反：拓扑中 gateway 是 order-svc 的上游，QPS 下跌应是从 gateway 向下游传导，规则 hypothesis 的「向上游传导」表述与事实不符，本结论未采纳。
  - order-svc-1 内存使用率 61.33%（P4）仅持续 1 分钟、幅度 0.20，且发生在流量下跌期间，更可能是流量变化下的次要波动而非根因，但无法完全排除其对 order-svc 的轻微影响。
  - bank-channel、mysql-order、redis-session 在拓扑中已是最下游，其 QPS 下跌可由上游流量减少完全解释，无证据表明它们自身发生故障。

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
- **AI 根因**：**流量波动** — 入口网关 QPS 在 09:41 起由约 1000 req/s 骤降至约 260 req/s（-74%），该跌幅沿 gateway→order-svc→payment-svc/bank-channel、inventory-svc→mysql-order、user-svc→redis-session 全链路同向同幅度传导，属于整体流量消失而非单点故障。（置信度 90%）
- **AI 认定的根因服务**：`gateway`
- **影响面**：全链路 8 个服务、15 个实例的请求量下降约 74%，持续约 17-20 分钟；由于成功率与业务错误率仍在 SLO 内，本次未造成错误预算消耗，但订单、支付、库存、用户会话等业务吞吐量大幅缩水，若持续将直接影响交易量。
- **建议责任方**：交易平台组（gateway 归属方），需联合网络/接入层团队排查外部流量源

<details><summary>证据引用（16 条）</summary>

- `BIZ-04:gateway/gateway-0/qps`
- `BIZ-04:gateway/gateway-1/qps`
- `BIZ-04:gateway/gateway-2/qps`
- `BIZ-04:order-svc/order-svc-0/qps`
- `BIZ-04:order-svc/order-svc-1/qps`
- `BIZ-04:order-svc/order-svc-2/qps`
- `BIZ-04:payment-svc/payment-svc-0/qps`
- `BIZ-04:payment-svc/payment-svc-1/qps`
- `BIZ-04:bank-channel/bank-channel-0/qps`
- `BIZ-04:inventory-svc/inventory-svc-0/qps`
- `BIZ-04:inventory-svc/inventory-svc-1/qps`
- `BIZ-04:mysql-order/mysql-order-0/qps`
- …其余 4 条同类证据（见 JSON 报告）

</details>

## 六、修复建议

### 立即止血（本次窗口内）

- **CLUS-01 · 流量波动**
  - 立即确认 09:41 前后入口侧（LB/DNS/CDN/上游调用方）是否存在流量调度变化或客户端侧中断，定位流量消失的源头。

### 短期加固（本周内）

- [CLUS-01] 核对 gateway 各实例的接入连接数、TLS 握手数与 4xx/5xx 分布，确认是「请求没进来」还是「请求被拒」，以区分外部流量中断与网关自身限流。
- [CLUS-01] 在 gateway 上按 1 分钟粒度回放 09:30-10:00 的 QPS 曲线，确认下跌是阶跃式还是渐进式，阶跃式更支持外部流量源切换。
- [CLUS-01] 短期：为 gateway 增加入口 QPS 相对基线的分钟级告警（跌幅 >50% 持续 2 分钟），缩短此类全链路流量塌陷的发现时间。

### 长期治理

- 在 gateway 入口建立「QPS 相对历史同时段基线跌幅 >50% 且持续 ≥2 分钟」的告警，覆盖全链路流量塌陷这一当前监控盲区。
- 为全链路核心服务补充流量-资源联动看板，区分「流量驱动的水位变化」与「自身劣化的水位变化」，避免下次把流量结果误判为资源根因。
- 梳理 gateway 上游的流量来源与调度策略（LB 权重、DNS TTL、客户端重试），建立入口流量异常的快速定位手册。
- 对 order-svc 等有状态服务设置内存水位分级告警（如 75%/85%），当前 61% 虽安全，但需明确 OOM 前的干预阈值。

## 七、稳定性趋势

**对比对象**：`run-20260925-100000-S4`（2026-09-27T03:17+08:00）

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
| 数据采集 | 成功 | 599ms | file 通道采集 215676 个点 / 150 条时序（595ms） |
| 标准化清洗 | 成功 | 2654ms | 216000 个有效点 / 150 条时序；缺失率 0.00%，置信度 高 |
| 结构化存储 | 成功 | 139ms | 数据集 S5 就绪（216000 个指标点） |
| 动态基线 | 成功 | 1284ms | 计算 150 条基线，动态基线覆盖率 100%（归档 603894 点） |
| 规则巡检 | 成功 | 116ms | 16 条规则命中 16 项原始发现（116ms） |
| 聚合去重 | 成功 | 3ms | 16 条异常（抑制 0 条）聚成 1 个根因簇；多粒度统计 8 个服务 / 5 个集群 |
| 稳定性评分 | 成功 | 0ms | 规则算分 83.5（B 良好） |
| AI 智能分析 | 成功 | 7493ms | deepseek/deepseek-chat；1 条根因结论，token 10381，耗时 7486ms |
| 报告输出 | 成功 | 5ms | Markdown 报告 9391 字符，已写入 run-20260925-100000-S5.md |

- AI 分析：deepseek/deepseek-chat；调用 1 次，耗时 7486ms，token 10381，提示词版本 v2
