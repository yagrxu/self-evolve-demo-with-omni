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
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise OmniError(f"{tool} 返回的不是合法 JSON: {out[:500]}", exit_code=EXIT_TOOL_ERROR) from e


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

    if not (creds.get("can_sign") and creds.get("ready")):
        raise SystemExit(
            f"AWS 凭证未就绪：{json.dumps(creds, ensure_ascii=False, indent=2)}\n"
            f"云端 evaluator 需要凭证。先跑 `aws sts get-caller-identity` 确认。"
        )
    ident = creds.get("caller_identity", {})
    print(f"✓ Omni 可达 | account={ident.get('account')} | source={creds.get('credential_source')}")
