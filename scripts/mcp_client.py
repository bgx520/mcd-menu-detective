#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦当劳 MCP 客户端（JSON-RPC 2.0 over Streamable HTTP）

零第三方依赖，仅用 Python 标准库。
协议参考：MCP Streamable HTTP Transport 规范。

安全纪律：
  - Token 只从环境变量 MCD_TOKEN 读取，绝不硬编码、绝不落盘、绝不打印。
  - 所有调用默认只读。涉及下单/领券/兑换等写操作由调用方显式传 confirm=True，
    且本模块不做任何自动确认。
  - 打印时对 Token 做掩码。

环境变量：
  MCD_TOKEN       必填，Token
  MCD_MCP_URL     可选，默认 https://mcp.mcd.cn
"""

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"

# 只读工具白名单：这些工具不产生写操作，可安全自动调用
READONLY_TOOLS = frozenset([
    "available-coupons",
    "query-my-coupons",
    "query-store-coupons",
    "query-nearby-stores",
    "delivery-query-addresses",
    "delivery-query-stores",
    "query-meals",
    "query-meal-detail",
    "list-nutrition-foods",
    "calculate-price",
    "query-order",
    "campaign-calendar",
    "query-my-account",
    "mall-points-products",
    "mall-product-detail",
    "mall-order-list",
    "query-meal-assistance",
    "now-time-info",
])

# 写操作工具：调用前必须由用户确认
WRITE_TOOLS = frozenset([
    "auto-bind-coupons",
    "create-order",
    "mall-create-order",
    "delivery-create-address",
])


class McpError(Exception):
    """MCP 调用异常"""


def _iter_json_candidates(text):
    """在文本里找可解析的 JSON 载荷。

    官方返回的实际形态（2026-10-12 实测）：
      ``每个门店的基本信息按单行展示：\\n- **门店名称**: data[].storeName\\n...
        {"success":true,...,"data":[...]}## 展示结果时，必须完整展示...\\n``

    即 **Markdown 说明在 JSON 之前，且 JSON 之后还会粘连一段说明**。
    因此不能用整段 json.loads，也不能假设 JSON 在开头。
    这里用 JSONDecoder.raw_decode 从每个可能的起点尝试，成功即返回。
    """
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        try:
            obj, _end = dec.raw_decode(text, i)
        except ValueError:
            continue
        if isinstance(obj, (dict, list)) and obj:
            yield obj, i, _end


def split_json_and_doc(text):
    """拆分「JSON 载荷」与「Markdown 说明」。

    返回 ``(payload, doc)``：
      - payload 为解析后的对象（dict 或 list）；找不到则为 None
      - doc 为去掉 JSON 之后剩余的说明文本（无则为空串）

    官方把包装字段放在 data 之外的顶层：
      {"success":true,"code":200,"message":"...","data":[...]}
    这里同时把顶层元信息平铺进返回的 dict 的 _meta 键，便于调用方读取
    success/code/traceId 等，同时保持 data 结构不变。
    """
    if not text:
        return None, ""
    best = None
    for obj, start, end in _iter_json_candidates(text):
        # 取覆盖字符数最多的那个片段，避开说明段里偶然出现的小 JSON
        span = end - start
        if best is None or span > best[1]:
            best = (obj, span, end)
        # 已经吃掉了绝大部分文本，无需继续找
        if span > len(text) * 0.5:
            break

    if best is None:
        return None, text.strip()

    obj, _span, end = best
    doc = (text[:0] + text[end:]).strip()
    if isinstance(obj, dict):
        meta = {k: obj[k] for k in ("success", "code", "message", "datetime", "traceId")
                if k in obj}
        if meta:
            obj = dict(obj)
            obj["_meta"] = meta
    return obj, doc


class McdMcpClient:
    """麦当劳官方 MCP 客户端。

    用法::

        client = McdMcpClient(os.environ["MCD_TOKEN"])
        tools = client.list_tools()
        menu = client.call_tool("query-meals", {...})
    """

    def __init__(self, token=None, url=None, timeout=30):
        self._token = token or os.environ.get("MCD_TOKEN", "")
        if not self._token:
            raise McpError(
                "未找到 MCD_TOKEN。请先在 https://open.mcd.cn/mcp 申请 Token，"
                "然后设置环境变量 MCD_TOKEN。"
            )
        self.url = url or os.environ.get("MCD_MCP_URL", DEFAULT_URL)
        self.timeout = timeout
        self._session_id = None
        self._req_id = 0
        self._server_info = None

    # ---------- 底层传输 ----------

    def _headers(self):
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer %s" % self._token,
            "User-Agent": "mcd-menu-detective/1.0",
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _post(self, payload, notify=False):
        """发一个 JSON-RPC 请求，返回解析后的 result。

        Streamable HTTP 可能返回纯 JSON，也可能返回 SSE 流，两种都要处理。
        """
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, headers=self._headers(), method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:400]
            raise McpError("HTTP %s：%s" % (e.code, detail or e.reason)) from None
        except urllib.error.URLError as e:
            raise McpError("网络不可达：%s" % e.reason) from None
        except OSError as e:
            raise McpError("连接失败：%s" % e) from None

        if notify:
            return None
        return self._parse(body)

    @staticmethod
    def _parse(body):
        """从响应体里取出 JSON-RPC result，同时兼容 SSE 格式。"""
        body = body.strip()
        if not body:
            raise McpError("响应体为空")

        if body.startswith("event:") or body.startswith("data:"):
            # SSE：逐条 data: 拼起来，最后一条通常是 JSON-RPC 响应
            chunks = []
            for line in body.splitlines():
                if line.startswith("data:"):
                    chunks.append(line[5:].strip())
            if not chunks:
                raise McpError("SSE 响应中没有 data 行")
            raw = chunks[-1]
        else:
            raw = body

        try:
            obj = json.loads(raw)
        except ValueError as e:
            raise McpError("响应不是合法 JSON：%s" % e) from None

        if isinstance(obj, list):
            # 批量响应，取第一个含 result/error 的
            for item in obj:
                if "result" in item or "error" in item:
                    obj = item
                    break

        if "error" in obj:
            err = obj["error"]
            raise McpError("MCP 返回错误 [%s] %s" % (err.get("code"), err.get("message")))
        if "result" not in obj:
            raise McpError("响应缺少 result 字段：%s" % json.dumps(obj, ensure_ascii=False)[:300])
        return obj["result"]

    def _next_id(self):
        self._req_id += 1
        return self._req_id

    # ---------- MCP 协议方法 ----------

    def initialize(self):
        """握手，建立会话。返回服务端信息。"""
        result = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcd-menu-detective", "version": "1.0.0"},
            },
        })
        self._server_info = result.get("serverInfo", {})
        # 协议要求的初始化完成通知（fire-and-forget）
        self._post({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }, notify=True)
        return self._server_info

    def _ensure_ready(self):
        if self._server_info is None:
            self.initialize()

    def list_tools(self):
        """tools/list —— 拿全量工具清单。"""
        self._ensure_ready()
        return self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/list",
            "params": {},
        }).get("tools", [])

    def tool_index(self):
        """返回 {工具名: 描述} 便于快速查阅。"""
        return {t.get("name", ""): (t.get("description") or "").strip() for t in self.list_tools()}

    def call_tool(self, name, arguments=None, confirm=False):
        """tools/call —— 调用一个 MCP 工具。

        安全门：写操作工具必须 confirm=True，且调用方负责事先获得用户同意。
        """
        if name in WRITE_TOOLS and not confirm:
            raise McpError(
                "工具 %s 属于写操作（会真实改变账户状态：领券/下单/兑换/新增地址）。"
                "必须先向用户展示将要执行的内容并获得明确同意，然后传 confirm=True。" % name
            )
        if name not in READONLY_TOOLS and name not in WRITE_TOOLS:
            # 不在已知清单里不直接拒绝，但提示可能是新工具
            sys.stderr.write("[warn] 工具 %s 不在本地已知清单中，可能是官方新增工具\n" % name)

        self._ensure_ready()
        result = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })

        # MCP 工具结果通常是 content 数组，取文本部分拼起来
        content = result.get("content", [])
        parts = [c.get("text", "") for c in content if c.get("type") == "text"]
        text = "\n".join(p for p in parts if p)
        if result.get("isError"):
            raise McpError("工具 %s 返回错误：%s" % (name, text[:400]))

        # 官方返回的 text 前置 Markdown 说明、JSON 载荷、尾随说明三段混排，
        # 这里把 payload 与说明分离：说明保留在 _doc 供 Agent 参考。
        payload, doc = split_json_and_doc(text)

        # 实测：HTTP 200 + JSON-RPC 无 error，但 _meta.success=false 且 code=400
        # 表示业务失败（例：缺 beType 参数）。必须转异常，否则会静默拿到空数据。
        if isinstance(payload, dict):
            meta = payload.get("_meta") or {}
            if meta.get("success") is False:
                raise McpError("业务失败 [code=%s] %s（traceId=%s）" % (
                    meta.get("code"), meta.get("message") or "未知错误",
                    meta.get("traceId") or "n/a"))
            if doc:
                payload = dict(payload)
                payload["_doc"] = doc
        return payload if payload is not None else text


# ---------- CLI ----------

def main():
    import argparse

    ap = argparse.ArgumentParser(description="麦当劳 MCP 客户端自检")
    ap.add_argument("--list-tools", action="store_true", help="列出全量工具")
    ap.add_argument("--call", metavar="TOOL", help="调用指定工具")
    ap.add_argument("--args", default="{}", help="工具参数 JSON 字符串")
    ap.add_argument("--confirm", action="store_true", help="写操作需显式确认")
    args = ap.parse_args()

    try:
        client = McdMcpClient()
    except McpError as e:
        print("[FAIL] %s" % e, file=sys.stderr)
        return 2

    try:
        info = client.initialize()
        print("[OK] 连接成功：%s v%s (MCP %s)" % (
            info.get("name", "?"), info.get("version", "?"), PROTOCOL_VERSION))

        if args.list_tools or not args.call:
            tools = client.list_tools()
            print("[OK] 工具总数：%d" % len(tools))
            ro = sum(1 for t in tools if t.get("name") in READONLY_TOOLS)
            wr = sum(1 for t in tools if t.get("name") in WRITE_TOOLS)
            print("     只读 %d / 写操作 %d / 未分类 %d" % (
                ro, wr, len(tools) - ro - wr))
            for t in tools:
                mark = "W" if t.get("name") in WRITE_TOOLS else " "
                desc = (t.get("description") or "").strip().splitlines()
                print("  [%s] %-26s %s" % (mark, t.get("name", ""),
                                            desc[0][:90] if desc else ""))

        if args.call:
            out = client.call_tool(args.call, json.loads(args.args), confirm=args.confirm)
            print(out)

    except McpError as e:
        print("[FAIL] %s" % e, file=sys.stderr)
        return 1
    except ValueError as e:
        print("[FAIL] --args 不是合法 JSON：%s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())