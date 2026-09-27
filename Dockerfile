# StaGuardAgent 生产镜像
# ---------------------------------------------------------------------------
# 三条约束，都是被现实问题逼出来的：
#   1. 非 root 运行 —— 容器逃逸的代价太高，能不给 root 就不给；
#   2. 依赖与代码分层复制 —— 改一行业务代码不该触发重新安装依赖（构建从分钟级降到秒级）；
#   3. HEALTHCHECK 用 /healthz 而不是 curl 进程存活 —— 进程活着但服务假死是最常见的情况。

FROM python:3.12-slim AS runtime

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    STAGUARD_DB_URL=sqlite:////app/data/staguard.db \
    STAGUARD_LOG_FORMAT=json

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖：这一层只在 pyproject.toml 变化时失效
COPY pyproject.toml README.md ./
RUN pip install --upgrade pip && pip install . || true

# 再拷代码并重装（带上真实包内容）
COPY config ./config
COPY staguard ./staguard
COPY data/changes.yaml ./data/changes.yaml
# 数据集在镜像内按固定种子生成，而不是从构建上下文拷进来：
#   - data/ 是 gitignore 的，从零 clone 的仓库根本没有它，靠 COPY 的话
#     镜像里就没有数据集，容器起来只能报「无法评估」；
#   - 它是数据不是代码，不该从开发者本机的产物里烤进镜像。
# gen-data 约 15s，换来的是「clone 下来直接 docker compose up 就能跑」。
RUN pip install . \
    && python -m staguard gen-data \
    && useradd --create-home --uid 10001 staguard \
    && mkdir -p /app/reports /app/logs /app/data/metrics \
    && chown -R staguard:staguard /app

USER staguard

EXPOSE 8080

# 探针用 API 的 /healthz：只看进程在不在没有意义，
# 「进程活着但依赖全挂」才是最需要被编排系统发现的故障。
#
# 这条探针只对默认命令（serve）成立：同一个镜像还跑 mock-monitor（8090）与
# schedule（不监听任何端口）。前者在 docker-compose.yml 里覆盖成探 8090/health，
# 后者显式 disable —— 否则它会一直显示 unhealthy，把真正的异常淹没掉。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# tini 负责回收僵尸进程并正确转发 SIGTERM，
# 否则定时调度进程收不到优雅停止信号，会被 K8s 强杀。
ENTRYPOINT ["/usr/bin/tini", "--", "staguard"]
CMD ["serve"]
