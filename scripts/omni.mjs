#!/usr/bin/env node
/**
 * omni.mjs — 从命令行 / CI 调用 CloudWatch Omni 的 MCP 工具。
 *
 * 为什么需要它
 * ------------
 * Omni 的 MCP server 不是独立进程，而是跑在 Kiro 扩展进程里的一个本机 TCP 服务：
 * 扩展把端口写到 `<workspace>/.omni/mcp-port`，官方的 `mcp-proxy.js` 只是
 * stdio ⇄ TCP 的适配器，握手时先发一条 `workspace/verify`。
 *
 * 这个脚本复现了同样的协议，但做成了一次性的 request/response CLI，
 * 于是流量生成、trace 导出、A/B replay 这些脚本可以在纯 shell 里调 Omni 工具，
 * 不必依赖 IDE 里的 agent 会话。
 *
 * 用法
 * ----
 *   node scripts/omni.mjs list
 *   node scripts/omni.mjs call <tool> '<json-args>'
 *   node scripts/omni.mjs call manage_datasets '{"action":"list","dataSource":"local"}'
 *
 * 环境变量
 * --------
 *   OMNI_WORKSPACE  workspace 绝对路径（默认：本仓库根目录）
 *   OMNI_PORT       覆盖端口（默认：读 <workspace>/.omni/mcp-port）
 *   OMNI_TIMEOUT_MS 默认 180000
 *
 * 退出码：0 成功 · 1 协议/工具错误 · 2 用法错误 · 3 连不上/超时
 */
import net from 'node:net';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const WORKSPACE = process.env.OMNI_WORKSPACE || REPO_ROOT;
const TIMEOUT_MS = Number(process.env.OMNI_TIMEOUT_MS || 180_000);

function resolvePort() {
  if (process.env.OMNI_PORT) return Number(process.env.OMNI_PORT);
  const portFile = path.join(WORKSPACE, '.omni', 'mcp-port');
  let raw;
  try {
    raw = fs.readFileSync(portFile, 'utf8').trim();
  } catch {
    die(3,
      `找不到 ${portFile}\n` +
      `Omni 扩展没有为这个 workspace 运行。请在 Kiro 里打开：\n  ${WORKSPACE}\n` +
      `（Omni MCP 一次只服务一个 workspace — 见 docs/BUILD-LOG.md 决策点 R1）`);
  }
  const port = Number(raw);
  if (!Number.isFinite(port) || port <= 0 || port >= 65536) {
    die(3, `${portFile} 内容不是合法端口: ${JSON.stringify(raw)}`);
  }
  return port;
}

function die(code, msg) {
  process.stderr.write(msg.endsWith('\n') ? msg : msg + '\n');
  process.exit(code);
}

/** 打开一个已完成 verify + initialize 的连接，然后发一条请求，返回结果。 */
function rpc(method, params) {
  return new Promise((resolve, reject) => {
    const port = resolvePort();
    const sock = net.createConnection({ port, host: '127.0.0.1' });
    const timer = setTimeout(() => {
      sock.destroy();
      reject({ code: 3, message: `${TIMEOUT_MS}ms 超时（tool=${params?.name ?? method}）` });
    }, TIMEOUT_MS);

    const send = (obj) => sock.write(JSON.stringify(obj) + '\n');
    let buf = '';

    sock.on('connect', () =>
      send({ jsonrpc: '2.0', id: 'verify', method: 'workspace/verify', params: { workspace: WORKSPACE } }));

    sock.on('data', (chunk) => {
      buf += chunk;
      const lines = buf.split('\n');
      buf = lines.pop() ?? '';
      for (const line of lines) {
        if (!line.trim()) continue;
        let msg;
        try { msg = JSON.parse(line); } catch { continue; }

        if (msg.id === 'verify') {
          if (msg.error) {
            clearTimeout(timer); sock.destroy();
            return reject({ code: 3, message: `workspace/verify 失败: ${msg.error.message}` });
          }
          send({
            jsonrpc: '2.0', id: 'init', method: 'initialize',
            params: {
              protocolVersion: '2024-11-05', capabilities: {},
              clientInfo: { name: 'selfevolve-demo-omni-cli', version: '1.0.0' },
            },
          });
        } else if (msg.id === 'init') {
          send({ jsonrpc: '2.0', method: 'notifications/initialized' });
          send({ jsonrpc: '2.0', id: 'req', method, params });
        } else if (msg.id === 'req') {
          clearTimeout(timer); sock.destroy();
          return msg.error ? reject({ code: 1, message: JSON.stringify(msg.error) }) : resolve(msg.result);
        }
      }
    });

    sock.on('error', (err) => {
      clearTimeout(timer);
      reject({ code: 3, message: `连接 127.0.0.1:${port} 失败: ${err.message}（Kiro 里的 Omni 扩展还在跑吗？）` });
    });
  });
}

/**
 * MCP 工具的返回值是 content 数组；Omni 的工具把 payload 放在
 * content[0].text 里的一段 JSON 字符串。这里剥掉外层信封。
 */
function unwrap(result) {
  const text = result?.content?.[0]?.text;
  if (typeof text !== 'string') return result;
  try { return JSON.parse(text); } catch { return { text }; }
}

export async function callTool(name, args = {}) {
  return unwrap(await rpc('tools/call', { name, arguments: args }));
}

export async function listTools() {
  const r = await rpc('tools/list', {});
  return r.tools.map((t) => ({ name: t.name, description: t.description }));
}

// ── CLI ────────────────────────────────────────────────────────────────────
const isMain = process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (isMain) {
  const [cmd, toolName, argsJson] = process.argv.slice(2);
  try {
    if (cmd === 'list') {
      const tools = await listTools();
      for (const t of tools) console.log(`${t.name}\n    ${(t.description || '').split('\n')[0].slice(0, 160)}`);
    } else if (cmd === 'call') {
      if (!toolName) die(2, '用法: omni.mjs call <tool> \'<json-args>\'');
      let args = {};
      if (argsJson) {
        try { args = JSON.parse(argsJson); }
        catch (e) { die(2, `第二个参数不是合法 JSON: ${e.message}`); }
      }
      console.log(JSON.stringify(await callTool(toolName, args), null, 2));
    } else {
      die(2, ' 用法:\n  omni.mjs list\n  omni.mjs call <tool> \'<json-args>\'');
    }
  } catch (e) {
    die(e.code ?? 1, `omni.mjs: ${e.message ?? e}`);
  }
}
