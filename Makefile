# AgentCore Self-Evolution Demo
#
# Region 与 profile 一律显式给出，不依赖 shell 环境 ——
# 本机 profile 默认 region 是 ap-southeast-1，而 Omni Space 与所有本 demo 的
# AgentCore 资源都在 us-west-2（见 docs/BUILD-LOG.md 决策点 R4 及修正 R4'）。

SHELL      := /bin/bash
PY         := .venv/bin/python
REGION     := us-west-2
export AWS_REGION := $(REGION)
export AWS_DEFAULT_REGION := $(REGION)

.PHONY: help venv datasets check omni-check serve baseline bundle deploy deploy-v2 \
        traffic-prod traffic-verify export-bad ab synth diff destroy clean

help:  ## 显示所有目标
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

# ── 环境 ──────────────────────────────────────────────────────────────────

venv:  ## 建 venv 并装依赖
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) -r agent/requirements.txt

omni-check:  ## 自检 Omni MCP 是否为本目录运行（R1）
	@node scripts/omni.mjs call check_credentials '{}' \
	  || { echo ""; echo "→ 请在 Kiro 里打开本项目目录后重试（见 README 前置条件 1）"; exit 1; }

# ── Phase 1 ───────────────────────────────────────────────────────────────

datasets:  ## 生成三份 dataset（oracle 由 tools.py 纯函数渲染）
	$(PY) scripts/build_datasets.py

check:  ## 只校验 dataset，不写盘
	$(PY) scripts/build_datasets.py --check

serve:  ## 本地起 agent（带 opentelemetry-instrument —— 否则 trace 上没有 prompt 版本）
	cd agent && ../.venv/bin/opentelemetry-instrument ../$(PY) agent.py

# ── Phase 2 ───────────────────────────────────────────────────────────────

baseline: omni-check  ## 本地基线：注册 dataset + 建 evaluator + 跑 15 条 + 9 维打分
	$(PY) scripts/local_baseline.py

# ── Phase 3 / 6 ───────────────────────────────────────────────────────────

bundle:  ## 为 Direct Code Deploy 打包 agent（vendored 依赖）
	scripts/build_agent_bundle.sh

synth: bundle  ## cdk synth（会先打包，因为 asset 指向 agent/.build）
	cd cdk && npx cdk synth

diff: bundle  ## cdk diff
	cd cdk && npx cdk diff

deploy: bundle  ## 部署 v1（baseline）
	cd cdk && AGENT_ENDPOINT_NAME=v1 AGENT_PROMPT_VARIANT=v1-baseline \
	  npx cdk deploy --all --require-approval never

deploy-v2: bundle  ## 部署 v2（Phase 5 选出的 winner）
	cd cdk && AGENT_ENDPOINT_NAME=v2 AGENT_PROMPT_VARIANT=$${AGENT_PROMPT_VARIANT:-v2-winner} \
	  AGENT_TOOL_TIER=$${AGENT_TOOL_TIER:-basic} \
	  npx cdk deploy --all --require-approval never

traffic-prod:  ## Phase 3：用 prod_sim 打云上流量
	$(PY) scripts/traffic.py --dataset prod_sim --endpoint v1

traffic-verify:  ## Phase 6：用 held-out 的 verify 打云上流量
	$(PY) scripts/traffic.py --dataset verify --endpoint v2

# ── Phase 4 / 5 ───────────────────────────────────────────────────────────

export-bad: omni-check  ## Phase 4：从云上导出低分/失败 trace 到本地
	$(PY) scripts/export_bad_traces.py

ab: omni-check  ## Phase 5：三条优化轴 paired A/B + 门禁判定
	$(PY) scripts/ab_replay.py

# ── 清理 ──────────────────────────────────────────────────────────────────

destroy:  ## 删除本 Demo 的 CDK stack（只删 selfevolve-demo-*，不碰账号里其他 demo）
	@echo "将删除 selfevolve-demo-eval 与 selfevolve-demo-agent（region $(REGION)）。"
	@echo "账号里的 cat_demo_strands / cat_demo_langgraph 不受影响。"
	@read -p "确认删除？输入 yes 继续：" ans; [ "$$ans" = "yes" ] || { echo "已取消"; exit 1; }
	cd cdk && npx cdk destroy selfevolve-demo-eval selfevolve-demo-agent --force

clean:  ## 清理本地构建产物（不动 .omni/ 和 reports/）
	rm -rf agent/.build cdk/cdk.out
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
