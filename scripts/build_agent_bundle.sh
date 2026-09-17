#!/usr/bin/env bash
# 为 AgentCore Direct Code Deploy 打包 agent。
#
# 托管 Python 运行时**不会**替你 pip install —— 依赖必须已经躺在 zip 里
# （AgentCore 团队的 feature launch 培训原话："zip up their dependencies
#  corresponding to that specific Python runtime version"）。
# 所以这里用 pip --target 把 wheel 摊平到 agent/.build/，CDK 再把这个目录变成
# S3 asset（见 cdk/lib/agent-stack.ts）。
#
# 平台风险（docs/BUILD-LOG.md 决策点 R2）
# --------------------------------------
# AgentCore 托管代码运行时跑在哪个 CPU 架构，文档没有明确说明，而带 C 扩展的包
# （pydantic-core / cryptography / grpcio）的 wheel 是分架构的。装错架构会在
# runtime 启动时报 "No module named ..." 或 ELF 不兼容。
# 因此架构做成可切换的，Phase 3 用最小 runtime 实测后再定，实测结论会写回 BUILD-LOG。
#
# 用法：
#   scripts/build_agent_bundle.sh                       # 默认 aarch64
#   AGENT_BUNDLE_PLATFORM=manylinux2014_x86_64 scripts/build_agent_bundle.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${REPO_ROOT}/agent"
BUILD="${SRC}/.build"

PLATFORM="${AGENT_BUNDLE_PLATFORM:-manylinux2014_aarch64}"
PY_VERSION="${AGENT_BUNDLE_PYTHON:-3.12}"

echo "==> 清理 ${BUILD}"
rm -rf "${BUILD}"
mkdir -p "${BUILD}"

echo "==> 复制 agent 源码与 fixture"
# 注意：只复制运行时真正需要的东西。不要 cp -r 整个 agent/，
# 那会把 .build/ 自己和 __pycache__ 递归包进去。
cp "${SRC}"/*.py "${BUILD}/"
cp "${SRC}/requirements.txt" "${BUILD}/"
cp "${SRC}/prompts.json" "${BUILD}/"
cp -R "${SRC}/fixtures" "${BUILD}/fixtures"

echo "==> 安装依赖 (platform=${PLATFORM}, python=${PY_VERSION})"
# --only-binary=:all: 是必须的：配合 --platform 时 pip 不能从源码构建
# （交叉编译到别的架构没有意义），只能取预编译 wheel。
# 如果某个包在目标平台没有 wheel，这里会直接失败 —— 这是好事，
# 比部署到云上才发现 import 报错强得多。
python3 -m pip install \
  --quiet \
  --target "${BUILD}" \
  --platform "${PLATFORM}" \
  --python-version "${PY_VERSION}" \
  --implementation cp \
  --only-binary=:all: \
  --upgrade \
  -r "${SRC}/requirements.txt"

echo "==> 精简包体"
# 这些目录只影响体积，不影响运行；zip 越小上传和冷启动越快。
find "${BUILD}" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "${BUILD}" -type d -name "*.dist-info" -prune -exec rm -rf {} + 2>/dev/null || true
find "${BUILD}" -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true

echo "==> 冒烟检查"
for f in agent.py tools.py prompt_loader.py prompts.json fixtures/orders.json fixtures/policies.json; do
  [[ -e "${BUILD}/${f}" ]] || { echo "缺少 ${f}"; exit 1; }
done
# 关键依赖必须真的落到包里，而不是"pip 说成功了"
for m in strands bedrock_agentcore opentelemetry boto3; do
  [[ -d "${BUILD}/${m}" ]] || { echo "依赖 ${m} 没有出现在 ${BUILD}/"; exit 1; }
done
# entryPoint 的第一个元素是可执行文件名，它必须真的存在于包里
[[ -f "${BUILD}/bin/opentelemetry-instrument" ]] \
  || echo "  ⚠️  bin/opentelemetry-instrument 不在包内 —— 若 runtime 启动失败，" \
          "改用 python -m opentelemetry.instrumentation.auto_instrumentation 作为 entryPoint"

SIZE=$(du -sh "${BUILD}" | cut -f1)
COUNT=$(find "${BUILD}" -type f | wc -l | tr -d ' ')
echo "==> 完成：${BUILD}  (${SIZE}, ${COUNT} 个文件, platform=${PLATFORM})"
