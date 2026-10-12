#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地自测 + 脱敏检查（零依赖）

用法：
  python tests/selfcheck.py            # 跑全部检查
  python tests/selfcheck.py --report   # 附带打印报告节选
"""
import ast
import io
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")

PASS, FAIL = [], []


def ok(name, detail=""):
    PASS.append(name)
    print("  [PASS] %s%s" % (name, ("  " + detail) if detail else ""))


def bad(name, detail=""):
    FAIL.append(name)
    print("  [FAIL] %s%s" % (name, ("  " + detail) if detail else ""))


def check(name, cond, detail=""):
    ok(name, detail) if cond else bad(name, detail)
    return cond


def main():
    print("=" * 62)
    print(" 麦门菜单侦探 · 本地自测")
    print("=" * 62)

    # 1. 语法
    print("\n[1] 语法检查")
    for fn in sorted(os.listdir(SCRIPTS)):
        if not fn.endswith(".py"):
            continue
        p = os.path.join(SCRIPTS, fn)
        try:
            ast.parse(open(p, encoding="utf-8").read(), filename=p)
            ok("语法 %s" % fn)
        except SyntaxError as e:
            bad("语法 %s" % fn, str(e))

    # 2. 零依赖
    print("\n[2] 零第三方依赖")
    STD = {"json", "os", "re", "sys", "argparse", "datetime", "urllib", "urllib.request",
           "urllib.error", "io", "collections", "subprocess", "tempfile", "math"}
    LOCAL = {"mcp_client", "detective", "mcp_formats"}
    for fn in sorted(os.listdir(SCRIPTS)):
        if not fn.endswith(".py"):
            continue
        src = open(os.path.join(SCRIPTS, fn), encoding="utf-8").read()
        mods = set()
        for m in re.finditer(r"^\s*(?:from|import)\s+([a-zA-Z_][\w.]*)", src, re.M):
            mods.add(m.group(1).split(".")[0])
        external = {m for m in mods if m not in STD and m not in LOCAL}
        check("依赖 %s" % fn, not external, ("外部依赖: %s" % sorted(external)) if external else "")

    # 3. 离线降级自测
    print("\n[3] 离线降级自测")
    env = dict(os.environ)
    env.pop("MCD_TOKEN", None)
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "detective.py"), "巨无霸", "--offline"],
                       capture_output=True, text=True, env=env, encoding="utf-8")
    check("离线退出码 0", r.returncode == 0, r.stderr[:100])
    check("离线报告含降级标注", "降级模式" in r.stdout)
    check("离线报告含 L1 营养指纹", "L1 营养指纹" in r.stdout)
    check("离线报告含 L2 配料结构", "L2 配料结构" in r.stdout)
    check("离线报告含免责声明", "不构成营养或医疗建议" in r.stdout)
    check("离线报告含降级告警", "降级告警" in r.stdout)

    # 4. JSON 输出
    print("\n[4] 结构化输出")
    r2 = subprocess.run([sys.executable, os.path.join(SCRIPTS, "detective.py"),
                         "巨无霸", "--offline", "--json"],
                        capture_output=True, text=True, env=env, encoding="utf-8")
    try:
        obj = json.loads(r2.stdout)
        check("JSON 可解析", True)
        check("JSON 含 item", "item" in obj)
        check("JSON 含 clues 数组", isinstance(obj.get("clues"), list) and len(obj["clues"]) > 0)
        check("JSON 每条线索有 evidence", all("evidence" in c for c in obj["clues"] if c))
    except ValueError as e:
        check("JSON 可解析", False, str(e))

    # 5. 无 Token 时的报错友好性
    print("\n[5] 无 Token 行为")
    r3 = subprocess.run([sys.executable, os.path.join(SCRIPTS, "mcp_client.py"), "--list-tools"],
                        capture_output=True, text=True, env=env, encoding="utf-8")
    check("无 Token 退出码非 0", r3.returncode != 0)
    check("无 Token 提示含申请地址", "open.mcd.cn/mcp" in (r3.stdout + r3.stderr))

    # 6. 写操作确认闸门
    print("\n[6] 安全闸门")
    sys.path.insert(0, SCRIPTS)
    try:
        from mcp_client import McpError, McdMcpClient, READONLY_TOOLS, WRITE_TOOLS
        check("WRITE_TOOLS 非空", len(WRITE_TOOLS) > 0, "含 %d 个" % len(WRITE_TOOLS))
        check("READONLY_TOOLS 非空", len(READONLY_TOOLS) > 0, "含 %d 个" % len(READONLY_TOOLS))
        check("读写集合无交集", not (READONLY_TOOLS & WRITE_TOOLS),
              "交集=%s" % (READONLY_TOOLS & WRITE_TOOLS))
        # 确认闸门：没有 confirm=True 时必须拒绝写操作
        cli = McdMcpClient(token="dummy-token-for-probe")
        rejected = False
        try:
            cli.call_tool("create-order", {}, confirm=False)
        except McpError as e:
            rejected = "必须先向用户展示" in str(e)
        check("写操作默认被拦截", rejected)
    except Exception as e:
        bad("安全闸门导入", str(e))


    # 6.5 格式适配层（用实测样本回归，四种格式各一例）
    print("\n[6.5] 返回格式适配层")
    try:
        from mcp_formats import (parse_campaign, parse_coupons,
                                 parse_nutrition, parse_time)
        # 样本 1：紧凑表格（营养库）
        nut = parse_nutrition({"success": True, "data":
            "[2]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,"
            "carbohydrate,sodium,calcium}:\n  巨无霸,null,2146,513,27,26,42,961,171\n"
            "  中薯条,null,1210,289,4,12,38,165,18"})
        check("营养库解析条数=2", len(nut) == 2, "得%d" % len(nut))
        check("营养库字段完整", bool(nut) and nut[0].get("calories") == 513.0
              and nut[0].get("sodium") == 961.0,
              str(nut[0]) if nut else "空")
        check("营养库条目名正确", bool(nut) and nut[0]["name"] == "巨无霸")
        # 样本 2：Markdown 券
        cps = parse_coupons({"data":
            "### 麦麦省优惠券列表：\n- 优惠券标题：中薯条买一送一 \\\n"
            "  状态：可领取 \\\n- 优惠券标题：霸王餐 \\\n  状态：已领取 \\"})
        check("券解析条数=2", len(cps) == 2, "得%d" % len(cps))
        check("券标题去反斜杠", bool(cps) and not str(cps[0].get("title", "")).endswith("\\"),
              repr(cps[0].get("title")) if cps else "空")
        check("券状态字段", bool(cps) and cps[0].get("status") == "可领取")
        # 样本 3：Markdown 活动
        evs = parse_campaign({"data":
            "### 当前时间：2026-10-12 15:03:45\n### 活动列表：\n"
            "#### 2026年10月12日 今日\n-   **活动标题**：超值9.9元早餐\n"
            "    **活动内容介绍**：早八的快乐\n"})
        check("活动解析条数=1", len(evs) == 1, "得%d" % len(evs))
        check("活动日期与今日标记", bool(evs) and evs[0].get("today") is True)
        check("活动标题内容", bool(evs) and "早餐" in evs[0].get("title", ""))
        # 样本 4：时间
        t = parse_time({"data": "2026-10-12T15:07:19.211"})
        check("时间解析", t == "2026-10-12T15:07:19.211", repr(t))
    except ImportError as e:
        bad("格式适配层导入", str(e))

    # 6.6 JSON/doc 分离（实测：Markdown + JSON + Markdown 三段混排）
    print("\n[6.6] 文本分段解析")
    try:
        from mcp_client import split_json_and_doc
        mixed = ("每个门店的基本信息按单行展示：\n- **门店名称**: data[].storeName\n"
                 '{"success":true,"code":200,"data":[{"storeCode":"1420207"}]}'
                 "## 展示结果时，必须完整展示 storeCode\n")
        payload, doc = split_json_and_doc(mixed)
        check("三段混排可解析", isinstance(payload, dict) and payload.get("code") == 200,
              str(payload)[:80] if payload else "None")
        check("data 正确提取", isinstance(payload, dict)
              and payload.get("data") == [{"storeCode": "1420207"}])
        check("尾部说明被剥离", isinstance(payload, dict) and "展示结果时" not in json.dumps(payload, ensure_ascii=False))
        only_text = split_json_and_doc("### 没有JSON的纯文本")
        check("纯文本返回 None", only_text[0] is None)
    except ImportError as e:
        bad("分段解析导入", str(e))

    # 7. 脱敏扫描
    print("\n[7] 脱敏扫描")
    PATTERNS = [
        (r"sk-" + r"[A-Za-z0-9]{16,}", "疑似 OpenAI style key"),
        (r"ghp_" + r"[A-Za-z0-9]{20,}", "GitHub token"),
        (r"github_pa" + r"t_[A-Za-z0-9_]{20,}", "GitHub fine-grained token"),
        (r"AKIA[0-9A-Z]{16}", "AWS key"),
        (r"1[3-9]\d{9}", "疑似手机号"),
        (r"[\w.\-+]+@[\w\-]+\.[\w\.]+", "疑似邮箱"),
        (r"Bearer\s+[A-Za-z0-9\-_\.]{20,}", "疑似 Bearer token"),
    ]
    # 允许的假值（测试夹具），出现即放过
    ALLOW = {"dummy-token-for-probe", "YOUR_MCP_TOKEN", "your_token_here"}

    hits = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__"}]
        for fn in filenames:
            if not fn.endswith((".py", ".md", ".json")):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, ROOT)
            try:
                txt = open(p, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            for pat, desc in PATTERNS:
                for m in re.finditer(pat, txt):
                    frag = m.group(0)
                    if any(a in frag for a in ALLOW):
                        continue
                    # 邮箱域名白名单：示例/官方文档域名
                    if desc == "疑似邮箱" and re.search(
                            r"@(example\.|mcd\.cn|github\.com|mcdonalds\.com\.cn|tencent\.com)", frag):
                        continue
                    hits.append("%s :: %s :: %s" % (rel, desc, frag[:40]))

    check("无凭据泄露", not hits, ("命中 %d 处:\n    %s" % (len(hits), "\n    ".join(hits[:8])))
          if hits else "扫描 %d 类模式" % len(PATTERNS))

    # 8. 必需交付文件
    print("\n[8] 交付文件完整性")
    REQUIRED = [
        "README.md", "CONTEST_DECLARATION.md", "MCP_INTEGRATION.md",
        "mcp-config.example.json", "workbuddy.md",
        "SKILL.md", "scripts/mcp_client.py", "scripts/mcp_formats.py",
        "scripts/detective.py",
    ]
    for rel in REQUIRED:
        p = os.path.join(ROOT, rel)
        exists = os.path.isfile(p)
        size = os.path.getsize(p) if exists else 0
        check("存在 %s" % rel, exists and size > 0,
              "%d bytes" % size if exists else "缺失")

    # 9. CONTEST_DECLARATION 完整性
    print("\n[9] 参赛声明一致性")
    p = os.path.join(ROOT, "CONTEST_DECLARATION.md")
    if os.path.isfile(p):
        ours = open(p, encoding="utf-8").read()
        try:
            import urllib.request
            url = ("https://raw.githubusercontent.com/M-China/"
                   "mcd-developer-innovation-challenge/main/CONTEST_DECLARATION.md")
            req = urllib.request.Request(url, headers={"User-Agent": "selfcheck"})
            official = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
            norm = lambda s: re.sub(r"\s+", "", s)
            check("与官方声明逐字一致", norm(ours) == norm(official),
                  "" if norm(ours) == norm(official) else "内容与官方版本不一致")
        except Exception as e:
            print("  [SKIP] 无法拉取官方声明做比对：%s" % str(e)[:80])

    # 汇总
    print("\n" + "=" * 62)
    print(" 通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print(" 失败项：")
        for f in FAIL:
            print("   - %s" % f)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())