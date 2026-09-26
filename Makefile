# StaGuardAgent 开发与运行入口
# ---------------------------------------------------------------------------
# 所有常用操作都收敛到 make，评审拿到代码后不需要读文档猜命令。

PY := .venv/bin/python
PIP := .venv/bin/pip
SCENARIO ?= S1
SOURCE ?=

.PHONY: help venv install gen-data run run-all runs eval sample test test-fast lint fmt serve monitor schedule docker-build docker-up clean

help: ## 显示可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## 创建虚拟环境
	python3 -m venv .venv

install: venv ## 安装依赖（含开发依赖）
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

gen-data: ## 生成模拟数据集（14 天归档入库 + 7 个场景文件）
	$(PY) -m staguard gen-data

run: ## 单次巡检：make run SCENARIO=S1 SOURCE=http
	$(PY) -m staguard run --scenario $(SCENARIO) $(if $(SOURCE),--source $(SOURCE),)

run-all: ## 依次巡检全部场景（用于批量巡检与回归）
	@for s in S0 S1 S2 S3 S4 S5 S6; do \
		echo "=== $$s ==="; \
		$(PY) -m staguard run --scenario $$s --log-level WARNING >/dev/null; \
	done
	@echo "全部场景巡检完成，报告见 reports/"

runs: ## 查看历史巡检记录
	$(PY) -m staguard runs

eval: ## 跑归因评测集，输出根因命中率 / 级别命中率 / 误报率
	COLUMNS=200 $(PY) -m staguard eval

sample: ## 导出报告样例到 docs/samples（文件名按 AI 模式区分，便于对比）
	COLUMNS=200 $(PY) -m staguard sample

test: ## 全量测试（含端到端，首次约 30s）
	$(PY) -m pytest tests/ -q

test-fast: ## 只跑单元测试（毫秒级，改规则时用）
	$(PY) -m pytest tests/ -q -m "not slow"

lint: ## 静态检查
	$(PY) -m ruff check staguard tests

fmt: ## 自动修复可修复的问题
	$(PY) -m ruff check --fix staguard tests

monitor: ## 启动模拟监控数据接口（HTTP 采集通道的对端）
	$(PY) -m staguard mock-monitor --port 8090

serve: ## 启动巡检 API（含健康探针）
	$(PY) -m staguard serve

schedule: ## 常驻定时巡检（每 30 分钟）
	$(PY) -m staguard schedule --interval 30

demo: gen-data test ## 一键跑通全流程：生成数据 + 跑测试 + 巡检 + 归因评测
	$(PY) -m staguard run --scenario S0 --log-level WARNING >/dev/null
	$(PY) -m staguard run --scenario S1 --log-level WARNING >/dev/null
	COLUMNS=200 $(PY) -m staguard eval
	@echo "已完成：reports/S0（正常态）与 reports/S1（依赖故障）可直接打开查看"

docker-build: ## 构建镜像
	docker build -t staguard:local .

docker-up: ## 本地起全套（数据源 + API + 定时巡检）
	docker compose up -d --build

clean: ## 清理生成物
	rm -rf data/staguard.db data/metrics reports logs .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
