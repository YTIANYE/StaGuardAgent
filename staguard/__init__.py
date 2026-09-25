"""StaGuardAgent：AI 驱动的业务稳定性自动化巡检 Agent。

分层：

    collector   数据接入（本地结构化文件 / Mock 监控 HTTP 接口）
    normalize   标准化清洗（单位统一、时间对齐、缺失与越界处理）
    store       结构化存储（SQLAlchemy Core，SQLite / PostgreSQL 可切换）
    rules       多维度巡检规则引擎（业务 / 性能 / 资源 / 链路）+ 动态基线
    detect      异常聚合、去重、分级、拓扑传播聚类
    ai          AI 智能分析（证据包 -> LLM 根因归因 -> schema 校验 -> 降级）
    report      巡检报告输出（控制台 / Markdown / JSON）+ 历史对比
    notify      告警推送（钉钉 / 企业微信，模拟）
"""

__version__ = "0.1.0"
