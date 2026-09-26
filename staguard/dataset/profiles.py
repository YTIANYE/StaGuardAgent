"""模拟数据的「世界设定」：各服务的正常水位、日周期曲线、指标耦合关系。

这里定义的是**因果一致**的合成世界，而不是一堆随机数。之所以较真这一点：
如果 QPS 涨了 CPU 却不动、成功率跌了错误数却不变，那规则引擎和 AI 都能轻易看穿，
整个巡检链路就退化成在拟合噪声，验证不出任何东西。

所以这里的每一条耦合关系，都对应真实系统里的一条物理规律（见 COUPLING_NOTES）。
"""

from __future__ import annotations

import math
from datetime import datetime

from ..models import MetricName

COUPLING_NOTES: dict[str, str] = {
    "success_rate->error_count": "错误数 = QPS x (1 - 成功率) x 60，成功率是驱动量，错误数是被导出量",
    "qps->cpu": "CPU 随 QPS 线性抬升，但保留固定开销（即使零流量也有基础占用）",
    "latency->conn": "延时不降则连接不释放：P99 相对基线每翻一倍，连接池占用增加约 25 个百分点，直至打满",
    "qps->conn": "吞吐上升同样抬高连接占用",
    "cpu/mem->latency": "CPU 或内存进入高压区间后，排队延时非线性放大（雪崩前兆）",
    "business_error_rate": "业务错误率与成功率解耦：HTTP 200 但业务失败，是技术成功率看不到的盲区",
}


class ServiceProfile:
    """一个服务的正常水位。数值单位：QPS=req/s，延时=ms，水位=%。"""

    __slots__ = ("qps", "p50_ms", "cpu", "mem", "conn")

    def __init__(self, qps: float, p50_ms: float, cpu: float, mem: float, conn: float) -> None:
        self.qps = qps
        self.p50_ms = p50_ms
        self.cpu = cpu
        self.mem = mem
        self.conn = conn


SERVICE_PROFILES: dict[str, ServiceProfile] = {
    # 入口网关：高流量、低延时
    "gateway": ServiceProfile(qps=1250, p50_ms=28, cpu=38, mem=55, conn=42),
    "order-svc": ServiceProfile(qps=950, p50_ms=46, cpu=42, mem=60, conn=45),
    "payment-svc": ServiceProfile(qps=720, p50_ms=86, cpu=36, mem=62, conn=48),
    "inventory-svc": ServiceProfile(qps=610, p50_ms=38, cpu=33, mem=58, conn=40),
    "user-svc": ServiceProfile(qps=480, p50_ms=24, cpu=28, mem=52, conn=35),
    # 外部依赖：本身没有「业务成功率」以外的负载波动，但延时基线明显更高
    "bank-channel": ServiceProfile(qps=700, p50_ms=130, cpu=22, mem=45, conn=55),
    "mysql-order": ServiceProfile(qps=1350, p50_ms=14, cpu=45, mem=68, conn=52),
    "redis-session": ServiceProfile(qps=980, p50_ms=6, cpu=18, mem=42, conn=30),
}

DEFAULT_PROFILE = ServiceProfile(qps=500, p50_ms=40, cpu=35, mem=55, conn=40)

# 归一化指标基线
SUCCESS_RATE_BASE = 99.97
BUSINESS_ERROR_RATE_BASE = 0.08

# 各指标的噪声系数（相对标准差）
NOISE_CV: dict[MetricName, float] = {
    MetricName.QPS: 0.035,
    MetricName.SUCCESS_RATE: 0.0002,
    MetricName.BUSINESS_ERROR_RATE: 0.35,
    MetricName.LATENCY_P50: 0.06,
    MetricName.LATENCY_P95: 0.10,
    MetricName.LATENCY_P99: 0.14,
    MetricName.CPU_USAGE: 0.05,
    MetricName.MEM_USAGE: 0.01,
    MetricName.CONN_USAGE: 0.06,
    MetricName.ERROR_COUNT: 0.0,  # 由 QPS 与成功率导出，不再单独加噪
}

# 分位数之间的比例关系（正常态下长尾相对中位的倍数）
P95_RATIO = 2.25
P99_RATIO = 3.35

# 生成器采样范围：决定正常态与故障注入时各类指标的取值区间。
# 它与 normalize 的 VALID_RANGE 语义不同，不能互相替换——后者是「物理上不可能的上界」，
# 要宽得多（防止把真实的高值当脏数据剔除）；这里窄，是为了让生成的数据落在合理水位内。
GENERATION_RANGE: dict[MetricName, tuple[float, float]] = {
    MetricName.QPS: (0.0, 100_000.0),
    MetricName.SUCCESS_RATE: (0.0, 100.0),
    MetricName.ERROR_COUNT: (0.0, 10_000_000.0),
    MetricName.BUSINESS_ERROR_RATE: (0.0, 100.0),
    MetricName.LATENCY_P50: (0.1, 60_000.0),
    MetricName.LATENCY_P95: (0.1, 60_000.0),
    MetricName.LATENCY_P99: (0.1, 60_000.0),
    MetricName.CPU_USAGE: (0.0, 100.0),
    MetricName.MEM_USAGE: (0.0, 100.0),
    MetricName.CONN_USAGE: (0.0, 100.0),
}


def profile_for(service: str) -> ServiceProfile:
    return SERVICE_PROFILES.get(service, DEFAULT_PROFILE)


def daily_factor(ts: datetime) -> float:
    """日周期曲线：双峰（午间大促 + 晚间高峰），凌晨落到谷底。

    这条曲线是「动态阈值」存在的理由——固定阈值在凌晨必然误报、在高峰必然漏报。
    """
    hour = ts.hour + ts.minute / 60.0
    peak_noon = math.exp(-((hour - 11.0) ** 2) / (2 * 2.6**2))
    peak_night = math.exp(-((hour - 20.0) ** 2) / (2 * 2.2**2))
    valley = 0.52
    return valley + (1 - valley) * (0.62 * peak_noon + 0.38 * peak_night)


def weekly_factor(ts: datetime) -> float:
    """周末流量略低。周内波动是 hour-of-week 基线优于「日周期 + 全局中位数」的原因。"""
    return 0.88 if ts.weekday() >= 5 else 1.0


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
