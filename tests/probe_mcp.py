#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实 MCP 字段探测脚本（需配置 MCD_TOKEN）

用途：官方随时可能调整字段结构。定期重跑本脚本，把原始响应落盘，
比对字段是否漂移，及时更新 references/mcp-tools.md。

安全：只调用 8 个只读工具，绝不触发任何写操作。
     写工具（create-order / auto-bind-coupons / draw-lottery 等）不会出现在本脚本中。

用法：
    export MCD_TOKEN=xxx
    python tests/probe_mcp.py --city 深圳 --place 南山
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from mcp_client import McpError, McdMcpClient      # noqa: E402
from mcp_formats import (parse_campaign, parse_coupons,  # noqa: E402
                         parse_nutrition, parse_time)
from detective import _menu_entries, _rows         # noqa: E402

OUT = os.path.join(HERE, "probe")
os.makedirs(OUT, exist_ok=True)

# 只读白名单：本脚本硬性只允许这些工具
READONLY = {
    "query-nearby-stores", "query-meals", "query-meal-detail",
    "list-nutrition-foods", "calculate-price", "available-coupons",
    "campaign-calendar", "now-time-info",
}


def dump(tag, obj):
    p = os.path.join(OUT, "%s.json" % tag)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
    return os.path.basename(p)


def safe_call(c, tool, args):
    """强制只读：任何不在白名单的工具直接拒绝。"""
    if tool not in READONLY:
        raise McpError("安全拦截：%s 不在只读白名单内，本脚本绝不调用写操作" % tool)
    return c.call_tool(tool, args)


def fields_of(rows, n=60):
    """取一组 dict 记录的字段并集，用于快速发现字段漂移。"""
    if not isinstance(rows, list):
        return []
    out = set()
    for x in rows[:n]:
        if isinstance(x, dict):
            out |= set(x.keys())
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description="麦当劳 MCP 字段探测（只读）")
    ap.add_argument("--city", default="深圳")
    ap.add_argument("--place", default="南山")
    ap.add_argument("--keyword", default="巨无霸", help="用于定位详情/价格的样例餐品")
    ap.add_argument("--be-type", type=int, default=1, choices=[1, 2, 5, 6],
                    help="1 到店自取 / 2 麦乐送 / 5 得来速 / 6 企业团餐")
    args = ap.parse_args()

    try:
        c = McdMcpClient()
    except McpError as e:
        print("[FAIL] %s" % e, file=sys.stderr)
        print("请先设置环境变量 MCD_TOKEN", file=sys.stderr)
        return 2

    try:
        info = c.initialize()
    except McpError as e:
        print("[FAIL] 连接失败：%s" % e, file=sys.stderr)
        return 1
    print("[OK] %s v%s（MCP %s）" % (info.get("name"), info.get("version"),
                                    "2025-06-18"))

    order_type = 2 if args.be_type in (2, 6) else 1

    # 1. 门店
    print("\n=== 1. query-nearby-stores ===")
    try:
        st_payload = safe_call(c, "query-nearby-stores", {
            "searchType": 2, "city": args.city,
            "keyword": args.place, "beType": args.be_type})
    except McpError as e:
        print("[FAIL] %s" % e, file=sys.stderr)
        return 1
    dump("stores", st_payload)
    stores = _rows(st_payload)
    print("  门店数: %d  字段: %s" % (len(stores), fields_of(stores)))
    if not stores:
        print("[FAIL] 未搜到门店，后续无法继续", file=sys.stderr)
        return 1
    s0 = stores[0]
    store_code, be_code = s0.get("storeCode"), s0.get("beCode")
    print("  选定: %s (%s)  beCode=%s" % (
        s0.get("storeName"), store_code, be_code))

    base = {"storeCode": store_code, "orderType": order_type, "beType": args.be_type}
    if be_code:
        base["beCode"] = be_code

    # 2. 菜单
    print("\n=== 2. query-meals ===")
    meals_payload = safe_call(c, "query-meals", base)
    dump("meals", meals_payload)
    entries = _menu_entries(meals_payload)
    print("  餐品条目: %d" % len(entries))
    if entries:
        print("  字段: %s" % sorted({k for e in entries for k in e if not k.startswith("_")}))
        print("  分类: %s" % sorted({e.get("category", "") for e in entries if e.get("category")}))

    target = None
    for e in entries:
        if e.get("name") == args.keyword:
            target = e
            break
    if target is None:
        cand = [e for e in entries if args.keyword in str(e.get("name", ""))]
        single = [e for e in cand
                  if not any(k in str(e.get("name", "")) for k in ("件套", "套餐", "组合"))]
        target = (single or cand or (entries or [None]))[0]
    if target is None:
        print("[FAIL] 菜单为空", file=sys.stderr)
        return 1
    code = target.get("code")
    print("  目标餐品: %s (code=%s)" % (target.get("name"), code))

    # 3. 详情
    print("\n=== 3. query-meal-detail ===")
    det = safe_call(c, "query-meal-detail", dict(base, code=code))
    dump("meal_detail", det)
    body = det.get("data") if isinstance(det, dict) else None
    if isinstance(body, dict):
        print("  字段: %s" % sorted(body.keys()))
        mods = body.get("modification")
        if isinstance(mods, dict):
            vals = [v for g in (mods.get("items") or []) for v in (g.get("values") or [])]
            print("  配料数: %d" % len(vals))
            if vals:
                print("  配料: %s" % "、".join(str(v.get("name")) for v in vals[:10]))
                print("  配料字段: %s" % sorted(vals[0].keys()))

    # 4. 营养库
    print("\n=== 4. list-nutrition-foods ===")
    nut = safe_call(c, "list-nutrition-foods", {})
    dump("nutrition", nut)
    rows = parse_nutrition(nut)
    hit = [r for r in rows if args.keyword in (r.get("name") or "")]
    print("  条目: %d  命中「%s」: %d" % (len(rows), args.keyword, len(hit)))
    for h in hit[:2]:
        print("    %s -> %s kcal / 蛋白 %s / 脂肪 %s / 碳水 %s / 钠 %s" % (
            h["name"], h.get("calories"), h.get("protein"),
            h.get("fat"), h.get("carb"), h.get("sodium")))

    # 5. 价格
    print("\n=== 5. calculate-price ===")
    price = safe_call(c, "calculate-price", dict(
        base, items=[{"productCode": code, "quantity": 1}]))
    dump("price", price)
    pdata = price.get("data") if isinstance(price, dict) else None
    if isinstance(pdata, dict):
        print("  字段: %s" % sorted(pdata.keys()))
        print("  price=%s (单位: 分 -> %.2f 元)  originalPrice=%s" % (
            pdata.get("price"),
            (pdata.get("price") or 0) / 100.0,
            pdata.get("originalPrice")))

    # 6. 券
    print("\n=== 6. available-coupons ===")
    cp = safe_call(c, "available-coupons", {})
    dump("coupons", cp)
    coupons = parse_coupons(cp)
    claim = [x for x in coupons if "可领取" in str(x.get("status") or "")]
    print("  券数: %d（其中可领取 %d）" % (len(coupons), len(claim)))
    for x in coupons[:3]:
        print("    - %s [%s]" % (x.get("title"), x.get("status")))

    # 7. 活动 + 时间
    print("\n=== 7. campaign-calendar + now-time-info ===")
    camp = safe_call(c, "campaign-calendar", {})
    dump("campaign", camp)
    events = parse_campaign(camp)
    today = [e for e in events if e.get("today")]
    print("  活动: %d（今日 %d）" % (len(events), len(today)))
    for e in events[:3]:
        print("    - %s %s" % (e.get("date"), e.get("title")))

    now = safe_call(c, "now-time-info", {})
    dump("now", now)
    print("  服务端时间: %s" % parse_time(now))

    print("\n[OK] 原始响应已落盘: tests/probe/")
    print("     请比对 references/mcp-tools.md 中的字段结构是否漂移。")
    return 0


if __name__ == "__main__":
    sys.exit(main())