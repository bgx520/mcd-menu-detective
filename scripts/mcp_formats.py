"""MCP 返回格式适配层（实测于 2026-10-12，mcd-mcp v1.0.0）

麦当劳 MCP 各工具返回格式**不统一**，实测共四种：

1. 标准 JSON         query-nearby-stores / query-meals / query-meal-detail / calculate-price
   {"success":true,"code":200,"data":[...]}  或  data 为 dict

2. 紧凑表格字符串   list-nutrition-foods
   "[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
    \n  猪柳麦满分,null,1288,308,16,16,24,781,213\n  中薯条,null,1210,289,4,12,38,165,18"

3. Markdown 文本     available-coupons / campaign-calendar
   "### 麦麦省优惠券列表：\n- 优惠券标题：中薯条买一送一 \n  状态：可领取 ..."

4. 时间字符串       now-time-info
   data 为 "2026-10-12 15:03:45" 或含 datetime 的 dict

本模块把这四种统一成Python 结构，让推理引擎不必关心差异。
零依赖，只用标准库。
"""

import json
import re

STD = {"json", "re"}


def _as_obj(payload):
    """取出业务数据体，兼容 dict 包装与裸值。"""
    if isinstance(payload, dict):
        if "data" in payload:
            return payload["data"]
        if "_meta" in payload and len(payload) == 1:
            return None
        return payload
    return payload


def parse_nutrition(payload):
    """解析 list-nutrition-foods 的紧凑表格格式。

    实测返回：``{"success":true,...,"data":"[160]{productName,...}:\\n  猪柳麦满分,null,1288,..."}``
    data 本身是**字符串**，需先剥出 data 再按表格解析。

    返回 ``[{name, calories, protein, fat, carb, sodium, ...}, ...]``
    字段名按表头动态映射，不写死列序。
    """
    # 关键：先取出 data。直接对整个 payload json.dumps 会把外层
    # success/code/traceId 也拼进字符串，导致正则只匹配到极少行。
    body = _as_obj(payload)
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False)
    text = body if isinstance(body, str) else ""
    if not text:
        return []

    out = []
    # 形如 [160]{a,b,c}:\n  row...
    m = re.search(r"\]\s*\{([^}]*)\}\s*:?\s*(.*)", text, re.S)
    if not m:
        # 退化成「每行逗号分隔」，首行做表头
        head = None
        for line in text.splitlines():
            line = line.strip().rstrip(":")
            if not line or "," not in line:
                continue
            cols = [x.strip() for x in line.split(",")]
            if head is None:
                head = cols
                continue
            if len(cols) == len(head):
                out.append(dict(zip(head, cols)))
        return [_norm_nutrition_row(r) for r in out]

    header = [h.strip() for h in m.group(1).split(",")]
    for line in m.group(2).splitlines():
        line = line.strip().rstrip(",")
        if not line or "," not in line:
            continue
        cols = [x.strip() for x in line.split(",")]
        if len(cols) < 2:
            continue
        while len(cols) < len(header):
            cols.append("")
        out.append(dict(zip(header, cols[:len(header)])))
    return [_norm_nutrition_row(r) for r in out]


def _norm_nutrition_row(row):
    """把表头名映射到推理引擎用的规范键，并做数值转换。"""
    def g(*keys):
        for k in keys:
            for rk, rv in row.items():
                if rk.lower() == k.lower():
                    return rv
        return None

    def num(v):
        if v in (None, "", "null", "None"):
            return None
        m = re.search(r"-?\d+(?:\.\d+)?", str(v))
        return float(m.group()) if m else None

    return {
        "name": (g("productName", "name") or "").strip(),
        "desc": (g("nutritionDescription", "description") or "").strip() or None,
        "energy_kj": num(g("energyKj")),
        "calories": num(g("energyKcal", "calories", "热量")),
        "protein": num(g("protein", "蛋白质")),
        "fat": num(g("fat", "脂肪")),
        "carb": num(g("carbohydrate", "carb", "碳水化合物")),
        "sodium": num(g("sodium", "钠")),
        "calcium": num(g("calcium", "钙")),
    }


def parse_coupons(text):
    """解析 available-coupons 的 Markdown。

    实测格式：``{"data":"### 麦麦省优惠券列表：\\n- 优惠券标题：中薯条买一送一 \\\\n  状态：可领取 ..."}``
    返回 [{title, status, image}]。
    """
    body = _as_obj(text)
    if not isinstance(body, str):
        rows = body
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
        return []

    items = []
    cur = {}
    for raw in body.splitlines():
        line = raw.strip()
        if re.match(r"^#{1,4}\s*麦麦省优惠券列表", line) or line.startswith("###"):
            continue
        mt = re.search(r"优惠券标题[：:]\s*(.+?)\s*$", line)
        if mt:
            if cur.get("title"):
                items.append(cur)
            # 去除 Markdown 硬换行残留的尾部反斜杠
            title = re.sub(r"\s*\\\s*$", "", mt.group(1)).strip()
            cur = {"title": title, "status": None, "image": None}
            continue
        ms = re.search(r"状态[：:]\s*(.+?)\s*$", line)
        if ms and cur:
            cur["status"] = re.sub(r"\s*\\\s*$", "", ms.group(1)).strip()
            continue
        mi = re.search(r'<img\s+src="([^"]+)"', line)
        if mi and cur:
            cur["image"] = mi.group(1)
    if cur.get("title"):
        items.append(cur)
    return items


def parse_campaign(text):
    """解析 campaign-calendar 的 Markdown。

    实测格式：``{"data":"### 当前时间：2026-10-12 15:03:45\\n### 活动列表：\\n#### 2026年10月12日 今日\\n-   **活动标题**：..."}``
    返回 [{date, today, title, content}]。
    """
    body = _as_obj(text)
    if not isinstance(body, str):
        rows = body
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
        return []

    now = None
    mn = re.search(r"当前时间[：:]\s*(.+?)\s*$", body, re.M)
    if mn:
        now = re.sub(r"\s*\\\s*$", "", mn.group(1)).strip()

    items = []
    date, today = "", False
    for raw in body.splitlines():
        line = raw.strip()
        md = re.match(r"^#{2,4}\s*(\d{4}年\d{1,2}月\d{1,2}日)\s*(.*)$", line)
        if md:
            date = md.group(1)
            today = "今日" in (md.group(2) or "")
            continue
        mt = re.search(r"\*\*活动标题\*\*[：:]\s*(.+?)\s*$", line)
        if mt:
            items.append({"date": date, "today": today,
                          "title": re.sub(r"\s*\\\s*$", "", mt.group(1)).strip(),
                          "content": ""})
            continue
        mc = re.search(r"\*\*活动内容介绍\*\*[：:]\s*(.*)$", line)
        if mc and items:
            items[-1]["content"] = mc.group(1).strip()
    if now:
        for it in items:
            it.setdefault("server_time", now)
    return items


def parse_time(payload):
    """解析 now-time-info，返回时间字符串。"""
    d = _as_obj(payload)
    if isinstance(d, dict):
        for k in ("datetime", "dateTime", "time", "currentTime"):
            if d.get(k):
                return str(d[k]).strip()
        return None
    if isinstance(d, str):
        return d.strip()
    return None