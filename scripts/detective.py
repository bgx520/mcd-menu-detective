#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦门菜单侦探 · 推理引擎

从麦当劳 MCP 拉取的真实数据出发，做交叉推理，输出「破案报告」。

MCP 闭环（5 个只读工具，无写操作）：
  1. query-meals        拿门店可售菜单
  2. query-meal-detail  拿单品配料详情
  3. list-nutrition-foods  拿营养成分
  4. calculate-price    算实际价格（验证标价 vs 实付）
  5. query-nearby-stores    拿门店（供应能力差异）

推理维度（6 条线索）：
  L1 营养指纹     热量/蛋白质/脂肪/碳水 反推配方倾向
  L2 配料结构     配料表长度与顺序暴露工艺复杂度
  L3 价格锚点     单位价格反推成本结构
  L4 组合套利     套餐 vs 单点 vs 优惠券 的最优解
  L5 门店差异     同品跨店供应差异反映的区域策略
  L6 时间杠杆     活动日历窗口 vs 当前时点的决策窗口

设计约束：
  - 零第三方依赖，纯标准库。
  - MCP 不可用时降级：用 --offline 走本地样例数据，报告标注 [降级模式]。
  - 结论必须可追溯：每条推断标注它用了哪几个工具的哪几个字段。
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from mcp_client import McpError, McdMcpClient
    HAVE_MCP = True
except ImportError:
    McpError = Exception
    McdMcpClient = None
    HAVE_MCP = False

try:
    from mcp_formats import parse_campaign, parse_coupons, parse_nutrition, parse_time
except ImportError:  # pragma: no cover
    parse_campaign = parse_coupons = parse_nutrition = parse_time = lambda x: []


# ---------- 数据规范化 ----------

def _num(v, default=None):
    """把 MCP 返回的各种数值形态统一成 float。"""
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group()) if m else default


def _first(d, *keys, default=None):
    """按多个候选键名取值，兼容官方字段命名差异。"""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    # 退一步做大小写不敏感匹配
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k.lower() in low and low[k.lower()] not in (None, ""):
            return low[k.lower()]
    return default


def normalize_item(raw, entry=None):
    """把一个餐品归一化成内部结构。缺字段就留 None，绝不编造。

    实测字段（query-meals，mcd-mcp v1.0.0）：
      code 在 dict 的 key 上，不在对象内；
      name / image / currentPrice / originalPrice / discountType /
      canWithOrder / withOrder / tags
    分类与顺序来自 data.categories[].meals[]，通过 entry 传入。
    """
    raw = raw or {}
    entry = entry or {}
    item = {
        "code": entry.get("code") or _first(raw, "code", "productCode", "itemCode"),
        "name": _first(raw, "name", "itemName", "mealName", default="未知餐品"),
        # 实测价格字段名是 currentPrice / originalPrice
        "price": _num(_first(raw, "currentPrice", "price", "salePrice", "amount")),
        "orig_price": _num(_first(raw, "originalPrice", "marketPrice", "listPrice")),
        "desc": _first(raw, "description", "desc", "intro", default=""),
        "category": entry.get("category") or _first(raw, "categoryName", "category",
                                                    default=""),
        "tags": entry.get("tags") or _first(raw, "tags", default=[]) or [],
        "image": _first(raw, "image", "imageUrl", default=""),
        "ingredients": [],
        "nutrition": {},
        "source": {},
    }
    if item["desc"] and not isinstance(item["desc"], str):
        item["desc"] = ""

    # 配料：query-meal-detail 返回 choices 数组，每项含 name/count/modification
    ing = _first(raw, "ingredients", "ingredientList", "materials", "配料", default=None)
    if isinstance(ing, str):
        ing = [s.strip() for s in re.split(r"[,，、;；]", ing) if s.strip()]
    if isinstance(ing, list):
        for x in ing:
            if isinstance(x, dict):
                nm = _first(x, "name", "ingredientName", "mealName", default="")
                amt = _num(_first(x, "amount", "quantity", "count", "weight"))
                if nm:
                    item["ingredients"].append({"name": nm, "amount": amt})
            elif isinstance(x, str) and x.strip():
                item["ingredients"].append({"name": x.strip(), "amount": None})
    item["ingredients"] = [i for i in item["ingredients"] if i.get("name")]

    nut = _first(raw, "nutrition", "nutritionInfo", "nutrient", default=None)
    if isinstance(nut, dict):
        item["nutrition"] = {
            "calories": _num(_first(nut, "calories", "energy", "kcal", "热量")),
            "protein": _num(_first(nut, "protein", "蛋白质")),
            "fat": _num(_first(nut, "fat", "脂肪")),
            "carb": _num(_first(nut, "carbohydrate", "carb", "碳水化合物", "碳水")),
            "sodium": _num(_first(nut, "sodium", "钠")),
        }
    return item


def normalize_nutrition(raw_list):
    """归一化营养库，返回 {名称关键词: nutrition dict}。

    输入来自 mcp_formats.parse_nutrition，已把紧凑表格解析成 dict 列表。
    """
    idx = {}
    for rec in raw_list or []:
        if not isinstance(rec, dict):
            continue
        nm = rec.get("name")
        if not nm:
            continue
        item = {
            "calories": rec.get("calories"),
            "protein": rec.get("protein"),
            "fat": rec.get("fat"),
            "carb": rec.get("carb"),
            "sodium": rec.get("sodium"),
            "energy_kj": rec.get("energy_kj"),
            "calcium": rec.get("calcium"),
        }
        idx[nm] = item
        for kw in re.split(r"[\s(（]", nm):
            if len(kw) >= 2:
                idx.setdefault(kw, item)
    return idx


# ---------- 推理线索 ----------

def clue_L1_nutrition(item, nut_idx):
    """营养指纹：从宏量营养素比例反推配方倾向。"""
    nut = dict(item.get("nutrition") or {})
    if not nut.get("calories"):
        # 按名称匹配营养库。菜单名常带套餐后缀（如「巨无霸三件套」），
        # 故先整名匹配，再逐步剥离「三件套/套餐/组合」等后缀，最后做包含匹配。
        cands = [item["name"]]
        stripped = re.sub(r"(三件套|二件套|两件套|套餐|组合|超值|加购|大份|中份|小份)$",
                          "", item["name"])
        if stripped != item["name"]:
            cands.append(stripped)
        for cand in cands:
            if cand and cand in nut_idx:
                for k in ("calories", "protein", "fat", "carb", "sodium"):
                    v = nut_idx[cand].get(k)
                    if v is not None:
                        nut[k] = v
                nut["_matched"] = cand
                break
        else:
            # 退一步：营养库名称包含餐品名
            for k, rec in nut_idx.items():
                if k and k in item["name"]:
                    for kk in ("calories", "protein", "fat", "carb", "sodium"):
                        if rec.get(kk) is not None:
                            nut.setdefault(kk, rec.get(kk))
                    if nut.get("calories"):
                        nut["_matched"] = k
                    break
    if not nut.get("calories"):
        return None

    c, p, f, cb = nut.get("calories") or 0, nut.get("protein") or 0, \
        nut.get("fat") or 0, nut.get("carb") or 0
    total = p * 4 + f * 9 + cb * 4
    if total <= 0:
        # 无宏量数据时只报热量
        return {
            "clue": "L1 营养指纹",
            "verdict": "热量 %d kcal（无宏量明细）" % c,
            "detail": "营养库仅提供热量，缺少蛋白/脂肪/碳水，无法做供能比分析",
            "severity": "中" if c >= 500 else "低",
            "evidence": "energyKcal=%d" % c,
            "confidence": "低",
        }

    p_pct, f_pct, c_pct = p * 4 / total * 100, f * 9 / total * 100, cb * 4 / total * 100

    findings = []
    if f_pct >= 55:
        findings.append(("高脂型", "脂肪供能比 %.0f%%，油炸工艺是热量主来源" % f_pct, "高"))
    elif c_pct >= 60:
        findings.append(("高碳型", "碳水供能比 %.0f%%，主料以淀粉/糖浆为主" % c_pct, "高"))
    elif p_pct >= 35:
        findings.append(("高蛋白型", "蛋白供能比 %.0f%%，肉饼/禽肉含量可观" % p_pct, "中"))

    if c >= 500:
        findings.append(("热量炸弹", "%d kcal，单品热量已超多数人一餐的 1/3" % c, "高"))
    elif c >= 300:
        findings.append(("热量偏高", "%d kcal" % c, "中"))
    elif c <= 150:
        findings.append(("轻食档", "%d kcal，属于低热量区间" % c, "低"))

    if nut.get("sodium") and nut["sodium"] >= 800:
        findings.append(("高钠警报", "钠 %d mg，约为每日推荐上限 2000mg 的 %.0f%%" % (
            nut["sodium"], nut["sodium"] / 2000 * 100), "高"))

    if not findings:
        findings.append(("均衡型", "%d kcal，宏量配比无明显偏向" % c, "低"))

    matched = nut.get("_matched")
    return {
        "clue": "L1 营养指纹",
        "verdict": findings[0][0],
        "detail": " | ".join(f[1] for f in findings),
        "severity": findings[0][2],
        "evidence": "热量 %g kcal / 蛋白 %s g / 脂肪 %s g / 碳水 %s g（匹配营养库条目：%s）" % (
            c, nut.get("protein"), nut.get("fat"), nut.get("carb"), matched or "餐品自带"),
        "confidence": "高" if matched else "中",
    }


def clue_L2_structure(item):
    """配料结构：配料表长度与顺序暴露工艺复杂度。

    实测数据源：query-meal-detail 的 modification.items[].values[]
    每项含 name / price（加价）/ selectedQuantity。
    """
    ings = item.get("ingredients") or []
    if not ings:
        return None

    n = len(ings)
    names = [i["name"] for i in ings if i.get("name")]
    findings = []

    if n <= 3:
        findings.append(("极简配方", "仅 %d 种配料，工艺门槛低，出品稳定性高" % n, "中"))
    elif n >= 8:
        findings.append(("复杂配方", "%d 种配料，涉及多段加工与冷链组装" % n, "高"))

    # 复配成分检测：括号里带"风味""酱""粉"通常是复配
    compound = [x for x in names if re.search(r"(风味|酱|粉|浆|包浆|糖浆|调味)", x)]
    if compound:
        findings.append(("复配调味", "含 %s 等复配调味组分" % "、".join(compound[:3]), "中"))

    # 可定制 = 出品依赖人工组装
    if item.get("support_modify"):
        findings.append(("可特调", "支持 %d 项配料自定义，门店需现场组装，出品一致性风险更高" % n, "中"))

    # 加价项 = 成本浮动空间
    priced = [i for i in ings if (i.get("amount") or 0) > 0]
    if priced:
        findings.append(("加价项 %d 个" % len(priced),
                        "含 %s，加价说明该项在标准配方外可单点升级" % "、".join(
                            i["name"] for i in priced[:3]), "低"))

    if names:
        findings.append(("主料判定", "配料表首位为「%s」，代表风味与成本的主体" % names[0], "低"))

    if not findings:
        findings.append(("常规配方", "%d 种配料，无显著特征" % n, "低"))

    return {
        "clue": "L2 配料结构",
        "verdict": findings[0][0],
        "detail": " | ".join(f[1] for f in findings),
        "severity": findings[0][2],
        "evidence": "配料 %d 项：%s" % (n, "、".join(names[:10]) + ("…" if n > 10 else "")),
        "confidence": "高" if names else "低",
    }


def clue_L3_price(item):
    """价格锚点：单位价格反推成本结构。

    实测语义（query-meals，mcd-mcp v1.0.0）：
      currentPrice  = 当前售价（已含活动折扣，单位元）
      originalPrice = 原价（单位元）
    因此 originalPrice > currentPrice 表示「在折」，差额即优惠力度。
    """
    price = item.get("price")
    if not price:
        return None

    orig = item.get("orig_price")
    findings = []
    if orig and orig > price:
        disc = (orig - price) / orig * 100
        findings.append(("折价品", "原价 %.2f 元现价 %.2f 元，优惠 %.0f%%，属活动引流款" % (
            orig, price, disc), "高" if disc >= 30 else "中"))
    elif orig and orig < price:
        findings.append(("加价品", "原价 %.2f 元现价 %.2f 元，逆价组合，疑为套餐加价" % (
            orig, price), "中"))
    else:
        findings.append(("原价款", "现价 %.2f 元，无折扣，属利润款" % price, "低"))

    if price <= 15:
        findings.append(("引流价带", "%.2f 元，属低价带，承担引流而非利润" % price, "中"))
    elif price >= 45:
        findings.append(("高利润带", "%.2f 元，属高价带，定价锚点上移" % price, "高"))

    nut = item.get("nutrition") or {}
    cals = nut.get("calories")
    if cals and cals > 0:
        findings.append(("热量单价", "每 100 kcal 售价 %.2f 元" % (price / cals * 100), "低"))

    tags = item.get("tags") or []
    if tags:
        findings.append(("官方标签", "、".join(str(x) for x in tags[:3]), "低"))

    return {
        "clue": "L3 价格锚点",
        "verdict": findings[0][0],
        "detail": " | ".join(f[1] for f in findings),
        "severity": findings[0][2],
        "evidence": "currentPrice=%g originalPrice=%s" % (price, orig),
        "confidence": "高",
    }


def clue_L4_arbitrage(item, coupons, price_check):
    """组合套利：单品 vs 套餐 vs 券的最优解。

    实测：available-coupons 返回 Markdown，已由 mcp_formats 解析为
    [{title, status, image}]；calculate-price 返回单位为「分」。
    """
    findings = []
    price = item.get("price") or 0

    # 可用券（只查不领）
    if coupons:
        titles = [c.get("title") for c in coupons
                  if isinstance(c, dict) and c.get("title")]
        claimable = [c for c in coupons if isinstance(c, dict)
                     and c.get("status") and "可领取" in str(c.get("status"))]
        if titles:
            findings.append((
                "可用券 %d 张" % len(coupons),
                "叠加前最优解：%s%s" % (
                    "、".join(titles[:3]),
                    ("（其中 %d 张当前可领取）" % len(claimable)) if claimable else ""),
                "高"))

    # 实付校验（price_check 单位已换算为元）
    # 实测语义：calculate-price 在不传券时返回原价（productOriginalPrice/price），
    # 而菜单 currentPrice 已含活动折扣。因此「不一致」是正常的，
    # 正确解读是：currentPrice 比 calculate-price 便宜，差额即当前活动优惠。
    if price_check is not None:
        if price and abs(price_check - price) < 0.01:
            findings.append((
                "实付校验",
                "calculate-price 回传 %.2f 元，与菜单现价一致" % price_check,
                "低"))
        elif price and price_check > price:
            gap = price_check - price
            pct = gap / price_check * 100
            findings.append((
                "活动优惠 %.0f%%" % pct,
                "菜单现价 %.2f 元较原价 %.2f 元直降 %.2f 元（%.0f%%），"
                "本单已自动应用活动折扣" % (price, price_check, gap, pct),
                "高" if pct >= 30 else "中"))
        else:
            findings.append((
                "实付校验",
                "calculate-price %.2f 元 vs 菜单现价 %.2f 元，需核对取餐方式差异" % (
                    price_check, price or 0.0),
                "中"))

    if not findings:
        return None

    return {
        "clue": "L4 组合套利",
        "verdict": findings[0][0],
        "detail": " | ".join(f[1] for f in findings),
        "severity": findings[0][2],
        "evidence": "可用券 %d 张 / calculate-price=%s" % (
            len(coupons or []), ("%.2f 元" % price_check) if price_check is not None else "N/A"),
        "confidence": "中",
    }


def clue_L5_store_supply(store_note):
    """门店差异：同品跨店供应差反映的区域策略。"""
    if not store_note:
        return None
    return {
        "clue": "L5 门店差异",
        "verdict": "区域供应分层",
        "detail": store_note,
        "severity": "低",
        "evidence": "query-nearby-stores",
        "confidence": "中",
    }


def clue_L6_timing(now_info, campaign):
    """时间杠杆：活动窗口 vs 当前时点的决策窗口。

    实测：campaign-calendar 经 mcp_formats 解析为
    [{date, today, title, content}]。
    """
    findings = []
    if now_info:
        findings.append(("服务端时间", str(now_info), "低"))

    if campaign:
        titles = [c.get("title") for c in campaign
                  if isinstance(c, dict) and c.get("title")]
        today_n = sum(1 for c in campaign if isinstance(c, dict) and c.get("today"))
        if titles:
            findings.append((
                "在档活动 %d 个" % len(campaign),
                "其中今日 %d 个：%s" % (today_n, "、".join(titles[:3])),
                "高" if today_n else "中"))

    if not findings:
        return None
    return {
        "clue": "L6 时间杠杆",
        "verdict": findings[0][0],
        "detail": " | ".join(f[1] for f in findings),
        "severity": findings[0][2],
        "evidence": "now-time-info + campaign-calendar（活动 %d 个）" % len(campaign or []),
        "confidence": "中",
    }


# ---------- 报告渲染 ----------

SEV_ICON = {"高": "[高]", "中": "[中]", "低": "[低]"}


def render_report(target, item, clues, meta):
    """渲染成纯文本破案报告（终端友好，也可直接贴给用户）。"""
    L = []
    L.append("=" * 62)
    L.append(" 麦门菜单侦探 · 破案报告")
    L.append("=" * 62)
    L.append("标的餐品：%s" % target)
    L.append("  商品编码：%s" % (item.get("code") or "N/A"))
    L.append("  分类：%s" % (item.get("category") or "N/A"))
    L.append("  MCP 模式：%s" % meta.get("mode"))
    L.append("  工具链：%s" % " -> ".join(meta.get("toolchain", [])))
    L.append("-" * 62)

    valid = [c for c in clues if c]
    if not valid:
        L.append("")
        L.append("线索不足：MCP 返回数据缺少可推理字段。")
        L.append("=" * 62)
        return "\n".join(L)

    for i, c in enumerate(valid, 1):
        L.append("")
        L.append("%d. %s  %s%s" % (i, c["clue"], SEV_ICON.get(c["severity"], ""), c["verdict"]))
        L.append("   推断：%s" % c["detail"])
        L.append("   证据：%s" % c["evidence"])
        L.append("   置信：%s" % c["confidence"])

    # 结论
    L.append("")
    L.append("-" * 62)
    hi = [c for c in valid if c["severity"] == "高"]
    L.append("【结案陈词】")
    if hi:
        L.append("  本餐品命中 %d 条高危线索，核心风险：" % len(hi))
        for c in hi:
            L.append("    - %s：%s" % (c["clue"], c["verdict"]))
    else:
        L.append("  未命中高危线索，属常规餐品。")
    L.append("")
    L.append("  免责：本报告由 MCP 公开数据推理生成，非麦当劳官方结论，")
    L.append("        不构成营养或医疗建议。价格与供应以官方实时结果为准。")
    L.append("=" * 62)
    return "\n".join(L)


# ---------- MCP 取数 ----------

def _rows(obj, *keys):
    """从 MCP 返回对象里提取数组。

    实测协议：{"success":true,"code":200,"data":...}
    data 可能是数组、可能是 {"list":[...]}、也可能是 {"categories":[...],"meals":{...}}。
    这里统一降层到「数组」。
    """
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        d = obj.get("data", obj)
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            for k in keys:
                if isinstance(d.get(k), list):
                    return d[k]
            for v in d.values():
                if isinstance(v, list):
                    return v
    return []


def _menu_entries(payload):
    """把 query-meals 的返回摊平成 [{code, category, tags}] 列表。

    实测结构（2026-10-12 真实门店 1420207，120 个餐品）：
      data.categories[] = {"name": "人气热卖", "meals": [{"code": "...", "tags": [...]}]}
      data.meals        = {"<code>": {"name","image","currentPrice","originalPrice",...}}
    即 categories 提供分类与顺序，meals 是 code -> 全字段 的字典。
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return []
    meals = data.get("meals")
    if not isinstance(meals, dict):
        return []
    cats = data.get("categories")
    entries = []
    if isinstance(cats, list):
        for cat in cats:
            if not isinstance(cat, dict):
                continue
            for m in (cat.get("meals") or []):
                if isinstance(m, dict) and m.get("code"):
                    entries.append({"code": m["code"],
                                    "category": cat.get("name") or "",
                                    "tags": m.get("tags") or []})
    if not entries:
        entries = [{"code": k, "category": "", "tags": []} for k in meals.keys()]
    # 合并 meals 字典的全字段
    for e in entries:
        full = meals.get(e["code"])
        e.update(full if isinstance(full, dict) else {})
        e["_order"] = len(entries)
    return entries


# beType 取值（实测官方错误提示：1-到店取餐, 2-麦乐送到家, 5-得来速(DT), 6-企业团餐）
BE_NUM = {"pickup": 1, "delivery": 2, "drive": 5, "group": 6}


def gather(client, keyword, store_code=None, be_code=None, city=None,
           place=None, be_type="pickup"):
    """MCP 闭环取数。任一环节失败则降级，不整体崩。

    实测协议要点（2026-10-12 以真实 Token 验证，mcd-mcp v1.0.0）：
      - 所有业务工具的 storeCode 为必填，必须先经 query-nearby-stores 取得
      - 业务工具必须传 beType（数字），文档只提 orderType 但实测缺失会返回 code=400
      - orderType：1=到店，2=外送；得来速(beType=5) 仍属到店，需带 beCode
      - 价格字段单位是「分」，展示前需除以 100
      - 返回文本是「Markdown说明 + JSON + 尾随说明」三段混排，客户端已处理
    """
    data = {"mode": "MCP 实时", "toolchain": [], "warns": []}
    tk = data["toolchain"]

    # 0. 门店（所有后续工具的必填前置）
    if not store_code:
        be_num = BE_NUM.get(be_type, 1)
        try:
            args = {"searchType": 2, "city": city or "深圳",
                    "keyword": place or city or "市中心", "beType": be_num}
            tk.append("query-nearby-stores")
            arr = _rows(client.call_tool("query-nearby-stores", args))
            data["stores"] = [
                {"code": x.get("storeCode"), "name": x.get("storeName"),
                 "address": x.get("address"), "distance": x.get("distance")}
                for x in arr if isinstance(x, dict)]
            if not arr:
                data["warns"].append("未搜到门店，后续工具需要 storeCode，已中止 MCP 取数")
                return data
            st = arr[0]
            store_code = st.get("storeCode")
            be_code = st.get("beCode")
            data["store_note"] = "以「%s」为基准（附近 %d 家门店，最近 %s 米），菜单与券按门店维度差异" % (
                st.get("storeName"), len(arr), st.get("distance"))
        except McpError as e:
            data["warns"].append("query-nearby-stores 失败：%s" % str(e)[:100])
            return data

    be_num = BE_NUM.get(be_type, 1)
    order_type = 2 if be_num in (2, 6) else 1

    def base_args():
        """所有门店级工具的统一入参。"""
        a = {"storeCode": store_code, "orderType": order_type, "beType": be_num}
        if be_code:
            a["beCode"] = be_code
        return a

    # 1. 菜单（实测为 categories + meals 两级结构）
    items = []
    try:
        tk.append("query-meals")
        payload = client.call_tool("query-meals", base_args())
        entries = _menu_entries(payload)
        for e in entries:
            raw = {k: v for k, v in e.items() if not k.startswith("_")}
            it = normalize_item(raw, entry=e)
            if it:
                items.append(it)
    except McpError as e:
        data["warns"].append("query-meals 失败：%s" % str(e)[:100])

    if not items:
        data["warns"].append("菜单为空或解析失败，无法继续")
        return data

    # 2. 定位目标（实测：菜单含大量套餐变体，须优先精确名匹配）
    target = None
    if keyword:
        # 优先级 1：完全同名
        for it in items:
            if it["name"] == keyword:
                target = it
                break
        # 优先级 2：单品优先于套餐（套餐名通常带「N件套/套餐/组合」）
        if target is None:
            cand = [it for it in items if keyword in it["name"]]
            single = [it for it in cand
                      if not re.search(r"(件套|套餐|组合|超值|双人|多人)", it["name"])]
            target = (single or cand or [None])[0]
        if target is None:
            target = items[0]
            data["warns"].append("未匹配到「%s」，改用菜单首项「%s」" % (keyword, target["name"]))
    else:
        target = items[0]
    data["item"] = target
    data["menu_size"] = len(items)
    data["candidates"] = [it["name"] for it in items
                          if keyword and keyword in it["name"]][:8]

    # 3. 配料详情（实测：配料在 modification.items[].values[]）
    if target.get("code"):
        try:
            tk.append("query-meal-detail")
            a = base_args()
            a["code"] = target["code"]
            det = client.call_tool("query-meal-detail", a)
            body = det.get("data") if isinstance(det, dict) else None
            if isinstance(body, dict):
                target["desc"] = target["desc"] or _first(body, "description", "desc",
                                                         default="")
                target["support_modify"] = bool(body.get("supportModify"))
                # 实测：配料 = modification.items[].values[]，每项含 name/price/selectedQuantity
                mods = body.get("modification")
                if isinstance(mods, dict):
                    ings = []
                    for grp in (mods.get("items") or []):
                        for v in (grp.get("values") or []):
                            if not isinstance(v, dict):
                                continue
                            nm = v.get("name")
                            if not nm:
                                continue
                            ings.append({
                                "name": nm,
                                "amount": _num(v.get("price")),
                                "selected": bool(v.get("selectedQuantity")),
                                "code": v.get("code"),
                            })
                    if ings:
                        target["ingredients"] = ings
                elif isinstance(mods, list):
                    ings = []
                    for grp in mods:
                        for v in (grp.get("values") or []):
                            if isinstance(v, dict) and v.get("name"):
                                ings.append({"name": v["name"],
                                             "amount": _num(v.get("price")),
                                             "selected": bool(v.get("selectedQuantity")),
                                             "code": v.get("code")})
                    if ings:
                        target["ingredients"] = ings
        except McpError as e:
            data["warns"].append("query-meal-detail 失败：%s" % str(e)[:100])

    # 4. 营养库（实测是紧凑表格字符串，须经 mcp_formats 解析）
    nut_idx = {}
    try:
        tk.append("list-nutrition-foods")
        nut_idx = normalize_nutrition(parse_nutrition(client.call_tool("list-nutrition-foods", {})))
    except McpError as e:
        data["warns"].append("list-nutrition-foods 失败：%s" % str(e)[:100])
    data["nut_idx"] = nut_idx

    # 5. 价格校验（实测：价格单位是分）
    price_check = None
    if target.get("code"):
        try:
            tk.append("calculate-price")
            a = base_args()
            a["items"] = [{"productCode": target["code"], "quantity": 1}]
            d = client.call_tool("calculate-price", a)
            raw = _num(_first(d, "price", "totalPrice", "amount", "payAmount",
                              "originalPrice")
                       if isinstance(d, dict) else None)
            if raw is None and isinstance(d, dict):
                inner = d.get("data")
                if isinstance(inner, dict):
                    raw = _num(_first(inner, "price", "totalPrice", "amount", "payAmount"))
            if raw is not None:
                # 单位为分时换算成元（>1000 视为分）
                price_check = raw / 100.0 if raw > 1000 else float(raw)
        except McpError as e:
            data["warns"].append("calculate-price 失败：%s" % str(e)[:80])
    data["price_check"] = price_check

    # 6. 可用券（只读查询，不领；实测返回 Markdown）
    coupons = []
    try:
        tk.append("available-coupons")
        coupons = parse_coupons(client.call_tool("available-coupons", {}))
    except McpError as e:
        data["warns"].append("available-coupons 失败：%s" % str(e)[:100])
    data["coupons"] = coupons

    # 7. 活动日历 + 服务端时间（实测均返回 Markdown）
    campaign = []
    try:
        tk.append("campaign-calendar")
        campaign = parse_campaign(client.call_tool("campaign-calendar", {}))
    except McpError:
        pass
    now_info = None
    try:
        tk.append("now-time-info")
        now_info = parse_time(client.call_tool("now-time-info", {}))
    except McpError:
        now_info = None

    data["campaign"] = campaign
    data["now_info"] = now_info or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return data


# ---------- 离线降级 ----------

OFFLINE_SAMPLE = [{
    "code": "OFFLINE-DEMO-001",
    "name": "巨无霸（离线示例）",
    "price": 22.0,
    "originalPrice": 24.0,
    "categoryName": "汉堡",
    "description": "离线样例数据，仅用于无 Token 时验证推理链路",
    "ingredients": [
        {"name": "牛肉饼"}, {"name": "面包胚"}, {"name": "生菜"},
        {"name": "番茄酱"}, {"name": "芥末酱"}, {"name": "洋葱"},
        {"name": "酸黄瓜"}, {"name": "奶酪片"},
    ],
    "nutrition": {"calories": 550, "protein": 25, "fat": 30, "carb": 45, "sodium": 980},
}]


def run_offline(keyword):
    item = normalize_item(OFFLINE_SAMPLE[0])
    data = {
        "mode": "降级模式（离线样例，MCP 未连接）",
        "toolchain": ["offline-sample"],
        "warns": ["未配置 MCD_TOKEN 或 MCP 不可达，全部结论基于内置样例，不可用于实际决策"],
        "nut_idx": {}, "coupons": [], "price_check": None,
        "store_note": None, "campaign": None,
        "now_info": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return item, data


# ---------- 主流程 ----------

def investigate(keyword, store_code=None, be_code=None, offline=False,
                be_type="pickup", city=None, place=None):
    """完整侦探流程。返回报告文本。

    实测协议：门店必填。store_code 缺省时自动用 city/place 检索第一家店。
    """
    if offline or not HAVE_MCP or not os.environ.get("MCD_TOKEN"):
        item, data = run_offline(keyword)
    else:
        try:
            client = McdMcpClient()
            data = gather(client, keyword, store_code=store_code, be_code=be_code,
                          city=city, place=place, be_type=be_type)
            item = data.get("item")
            if item is None:
                raise McpError("未能从菜单取到任何餐品")
        except McpError as e:
            item, data = run_offline(keyword)
            data["warns"].append("MCP 连接失败已降级：%s" % str(e)[:100])

    nut_idx = data.pop("nut_idx", {}) or {}
    price_check = data.pop("price_check", None)
    coupons = data.pop("coupons", [])
    store_note = data.pop("store_note", None)
    campaign = data.pop("campaign", None)
    now_info = data.get("now_info")

    clues = [
        clue_L1_nutrition(item, nut_idx),
        clue_L2_structure(item),
        clue_L3_price(item),
        clue_L4_arbitrage(item, coupons, price_check),
        clue_L5_store_supply(store_note),
        clue_L6_timing(now_info, campaign),
    ]

    meta = {"mode": data["mode"], "toolchain": data["toolchain"]}
    report = render_report(item["name"], item, clues, meta)

    if data.get("warns"):
        report += "\n\n【降级告警】\n" + "\n".join("  ! " + w for w in data["warns"])

    return {"item": item, "clues": clues, "meta": meta, "report": report,
            "warnings": data.get("warns", [])}


def main():
    ap = argparse.ArgumentParser(description="麦门菜单侦探 · 推理引擎")
    ap.add_argument("keyword", nargs="?", default="巨无霸", help="要破案的餐品关键词")
    ap.add_argument("--store", help="门店编码 storeCode（省略则按城市自动检索）")
    ap.add_argument("--be-code", help="业务编码 beCode（得来速/麦乐送必填）")
    ap.add_argument("--city", default="深圳", help="城市，配合 --store 省略时使用")
    ap.add_argument("--place", help="位置关键词，如「腾讯大厦」")
    ap.add_argument("--be-type", default="pickup",
                    choices=["pickup", "drive", "delivery"],
                    help="取餐类型：pickup 到店自取 / drive 得来速 / delivery 麦乐送")
    ap.add_argument("--offline", action="store_true", help="强制离线降级模式")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    args = ap.parse_args()

    result = investigate(args.keyword, store_code=args.store, be_code=args.be_code,
                         offline=args.offline, be_type=args.be_type,
                         city=args.city, place=args.place)

    if args.json:
        print(json.dumps({
            "item": result["item"],
            "clues": result["clues"],
            "meta": result["meta"],
            "warnings": result["warnings"],
        }, ensure_ascii=False, indent=2))
    else:
        print(result["report"])
    return 0


if __name__ == "__main__":
    sys.exit(main())