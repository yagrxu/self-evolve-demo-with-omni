"""从 Python 调用 Omni MCP 工具的薄封装。

实现方式：转调 `scripts/omni.mjs`（Node）。
为什么不在 Python 里重写一遍 MCP 协议 —— 只有一个理由：**唯一真相来源**。
握手细节（`workspace/verify` 的确切消息形状、content 信封的剥法、端口文件位置）
只在一个地方实现，Omni 扩展升级时也只有一个地方要改。

用法：
    from omni_client import omni, OmniError
    creds = omni("check_credentials")
    ds = omni("manage_datasets", {"action": "list", "dataSource": "local"})
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "scripts" / "omni.mjs"

# omni.mjs 的退出码约定
EXIT_TOOL_ERROR = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3


class OmniError(RuntimeError):
    """Omni 工具调用失败。``unreachable=True`` 表示扩展没在为本 workspace 运行。"""

    def __init__(self, message: str, *, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code
        self.unreachable = exit_code == EXIT_UNREACHABLE


def omni(tool: str, args: dict | None = None, *, timeout_s: int = 600) -> dict | list:
    """调一个 Omni MCP 工具，返回已剥掉 content 信封的 payload。"""
    proc = subprocess.run(  # noqa: S603
        ["node", str(BRIDGE), "call", tool, json.dumps(args or {}, ensure_ascii=False)],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        cwd=str(REPO),
    )
    if proc.returncode != 0:
        raise OmniError(
            f"{tool} 失败 (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}",
            exit_code=proc.returncode,
        )
    out = proc.stdout.strip()
    if not out:
        return {}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as e:
        raise OmniError(f"{tool} 返回的不是合法 JSON: {out[:500]}", exit_code=EXIT_TOOL_ERROR) from e

    # Omni 的工具会用 exit 0 + `{"error": "..."}` 表示业务失败 —— 不是协议错误，
    # 所以 returncode 检查抓不到它。必须在这里显式抬成异常。
    #
    # 实测（BUILD-LOG 2.1）：`create_evaluator` 因占位符不合法而失败时正是这个形态，
    # 而调用方把它当成功、把 `None` 当 evaluator id 继续跑，直到几十行后才以一个
    # 完全无关的 KeyError 崩掉。静默失败在这条链路上代价极高：evaluator 没建成却
    # 继续评估，会产出一份看起来正常、实际缺了一个维度的基线。
    # `len(payload) <= 2` 这个判据太窄 —— 实测（BUILD-LOG 5.4）
    # `invoke_agent` 的失败信封是三个键：{"error", "message", "sessionId"}，
    # 于是被当成成功返回，一路带到门禁才以「0 个配对样本」暴露。
    # 改为：只要有 error 键、且没有任何成功载荷的迹象，就抬成异常。
    if isinstance(payload, dict) and payload.get("error") and not any(
            k in payload for k in ("content", "results", "traces", "datasets",
                                   "evaluators", "examples", "evaluatorId")):
        detail = payload.get("message") or payload["error"]
        raise OmniError(f"{tool} 返回业务错误: {detail}", exit_code=EXIT_TOOL_ERROR)
    return payload


def require_omni() -> None:
    """在脚本开头调用：确认 Omni 可达，否则给出可操作的提示后退出。"""
    try:
        creds = omni("check_credentials")
    except OmniError as e:
        if e.unreachable:
            raise SystemExit(
                f"{e}\n\n"
                f"需要在 Kiro 里打开本项目目录，Omni 扩展才会为它启动 MCP server：\n"
                f"  {REPO}\n"
                f"（Omni MCP 一次只服务一个 workspace —— 见 docs/BUILD-LOG.md 决策点 R1）"
            ) from e
        raise SystemExit(str(e)) from e

    # 只有 can_sign 是硬门 —— 它为假说明凭证链根本签不出请求，任何工具都不用试了。
    if not creds.get("can_sign"):
        raise SystemExit(
            f"AWS 凭证未就绪：{json.dumps(creds, ensure_ascii=False, indent=2)}\n"
            f"云端 evaluator 需要凭证。先跑 `aws sts get-caller-identity` 确认。"
        )

    ident = creds.get("caller_identity", {})
    print(f"✓ Omni 可达 | account={ident.get('account')} | source={creds.get('credential_source')}")

    # `ready` 不能当硬门（见 docs/BUILD-LOG.md 决策点 R8）。
    # 它反映的是 Omni 后端 authorization check 的结果，`auth_status:"transient"` 时
    # 只表示那一次 check 5xx/超时了 —— 而本地工具（datasets local / test agent invoke /
    # 本地 evaluator）实测照常可用。把它当硬门会让 Phase 2 因为一个假信号直接 exit。
    if not creds.get("ready"):
        print(
            f"⚠ check_credentials ready=false (auth_status={creds.get('auth_status')})"
            f" —— 本地工具不受影响，继续。\n"
            f"  云端 trace 查询（Phase 3/4）需要扩展宿主进程有 AWS_REGION，"
            f"若报 'Cloud endpoint not configured'，从终端重启 Kiro：\n"
            f"    AWS_REGION=us-west-2 open -na Kiro --args {REPO}"
        )
