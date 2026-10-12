# MCP 工具清单与实测字段映射

本文档的字段结构全部来自 **2026-10-12 真实 Token 实测**（`mcd-mcp v1.0.0`，深圳南山门店 1420207），不是文档推测。官方 doc 页（`open.mcd.cn/mcp/doc`）是 SPA 壳子且描述不完整，实测发现的协议细节比文档多。

## Server 配置

| 项 | 值 |
|---|---|
| 协议 | MCP Streamable HTTP + JSON-RPC 2.0 |
| 端点 | `https://mcp.mcd.cn` |
| 认证 | `Authorization: Bearer <MCD_TOKEN>` |
| 协议版本 | `2025-06-18` |
| 工具总数 | **35**（实测 `tools/list`） |
| 限流 | 600 次/分钟 |
| 区域 | 中国大陆（不含港澳台） |
| 用途限制 | 仅限非商业用途 |

Token 申请：https://open.mcd.cn/mcp → 手机号验证登录 → 控制台 → 激活。

---

## ⚠️ 三个实测发现的协议陷阱

### 陷阱 1：返回文本是「Markdown + JSON + Markdown」三段混排

**不是**纯 JSON。实测 `query-nearby-stores` 的 `content[0].text` 形态：

```
每个门店的基本信息按单行展示：
- **门店名称**: data[].storeName
...                                        ← 说明段在 JSON 之前
{"success":true,"code":200,...,"data":[...]}## 展示结果时，每个门店的 storeCode 必须完整展示
                                         ← JSON 之后还粘了一段说明
```

**后果**：`json.loads(整个 text)` 必然失败。
**解法**：`mcp_client.split_json_and_doc()` 用 `JSONDecoder.raw_decode` 从每个 `{` 位置试探，取覆盖字符数最大的片段。

### 陷阱 2：`data` 字段有四种不同类型

| 工具 | `data` 实际类型 |
|---|---|
| `query-nearby-stores` | 数组 `[{storeCode, storeName, ...}]` |
| `query-meals` | **dict** `{categories: [...], meals: {code: {...}}}` |
| `query-meal-detail` | dict `{code, name, modification: {...}}` |
| `list-nutrition-foods` | **字符串**（紧凑表格） |
| `available-coupons` | **字符串**（Markdown） |
| `campaign-calendar` | **字符串**（Markdown） |
| `calculate-price` | dict `{price: 2550, productList: [...]}` |

**解法**：`mcp_formats.py` 统一适配，推理引擎不必关心差异。

### 陷阱 3：业务错误藏在 HTTP 200 里

缺 `beType` 参数时，HTTP 200、JSON-RPC 无 error，但响应是：

```json
{"success":false,"code":400,"message":"beType1-到店取餐，2-麦乐送到家，5-得来速(DT)，6-企业团餐"}
```

**后果**：不检查 `success` 就会静默拿到空数据，表现为「查不到餐品」而非「参数错了」。
**解法**：`mcp_client.call_tool()` 检测 `_meta.success === false` 时抛 `McpError`。

---

## 本 Skill 使用的工具（8 个，只读）

调用顺序即推理流水线顺序。

### 1. `query-nearby-stores` — 门店检索

**入参**（实测 `searchType` 必传）：

| 字段 | 值 |
|---|---|
| `searchType` | `1` 收藏餐厅 / `2` 按位置搜索（**必传**） |
| `city` | `searchType=2` 时必填 |
| `keyword` | 位置关键词，`searchType=2` 时必填 |
| `beType` | `1` 到店自取 / `5` 得来速 / `2` 麦乐送 / `6` 企业团餐 |

**返回 `data[]` 实测字段**：

```json
{
  "storeCode": "1420207",
  "storeName": "麦当劳深圳桂庙路餐厅",
  "address": "南山大道1175号新绿岛大厦1层B号",
  "distance": 71,
  "businessStatus": true,
  "businessStartTime": "00:00",
  "businessEndTime": "23:59",
  "longitude": 0.0,
  "latitude": 0.0,
  "reservation": true,
  "reservationTimeOptions": [{"date":"2026-10-12","today":true,"reservationOptionText":"..."}]
}
```

> `beType=1`（到店自取）时返回的门店**无 beCode**；`beType=5`（得来速）才有 beCode。

### 2. `query-meals` — 菜单 ⚠️ 双层结构

**入参**：`storeCode`（**必填**）、`orderType`（`1` 到店 / `2` 外送）、`beType`（**必填**，缺失返回 code=400）

**返回 `data` 是两级结构**（实测 15 个分类 / 120 个餐品）：

```json
{
  "categories": [
    { "name": "人气热卖", "meals": [ {"code":"9900016076","tags":["心形薯饼同款比心"]} ] }
  ],
  "meals": {
    "1100": {
      "name": "巨无霸",
      "image": "https://menu-img.mcd.cn/...",
      "currentPrice": "25.5",
      "originalPrice": "25.5",
      "canWithOrder": false,
      "discountType": "...",
      "withOrder": [...]
    }
  }
}
```

**关键点**：
- **餐品编码不在对象内**，它是 `meals` 字典的 key
- **`currentPrice` 是当前售价（已含活动折扣）**，`originalPrice` 是原价 —— 这是字符串，需转数值
- **分类与顺序来自 `categories`**，全字段来自 `meals`
- 同一分类的 `name` 可能含换行（如 `巨无霸\n牛鱼肉堡`）

### 3. `query-meal-detail` — 配料详情

**入参**：`storeCode`、`orderType`、`beType`、`code`

**返回 `data` 实测结构** —— 配料藏在 `modification` 里：

```json
{
  "code": "1100",
  "name": "巨无霸",
  "image": "https://...",
  "rounds": [],
  "supportModify": true,
  "modification": {
    "items": [{
      "maxValues": 5, "minValues": 0,
      "values": [
        {"code":"100116","name":"巨无霸酱","price":0,
         "maxQuantity":1,"minQuantity":0,"selectedQuantity":1,
         "selectedKey":"0-1","unselectedKey":"0-0"},
        {"code":"100141","name":"吉士","price":0, ...},
        {"code":"100168","name":"生菜","price":0, ...},
        {"code":"100202","name":"酸黄瓜","price":0, ...},
        {"code":"100212","name":"洋葱粒","price":0, ...}
      ]
    }]
  }
}
```

**关键点**：
- 配料 = `modification.items[].values[]`，**不是** `ingredients` 字段
- `price` 是**加价**（0 表示标准配方含），非克重
- `selectedQuantity > 0` 表示当前默认选中
- `supportModify=true` 表示支持门店现场特调

### 4. `list-nutrition-foods` — 营养库（紧凑表格格式）

**返回 `data` 是字符串**：

```
[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  猪柳麦满分,null,1288,308,16,16,24,781,213
  中薯条,null,1210,289,4,12,38,165,18
  巨无霸,null,2146,513,27,26,42,961,171
```

**关键点**：
- 首行 `[160]` 是条目数
- `{...}` 是表头，**列序按表头动态解析，不写死**
- `energyKcal` 才是千卡，`energyKj` 是千焦
- `nutritionDescription` 全是 `null`
- 实测 160 条，其中 156 条热量 > 0
- 解法：`mcp_formats.parse_nutrition()`

### 5. `calculate-price` — 价格计算

**入参**：`storeCode`、`orderType`、`beType`、`items: [{"productCode": "...", "quantity": 1}]`

⚠️ 字段名是 **`productCode` / `quantity`**，不是 `code` / `count`。

**返回 `data` 实测**：

```json
{
  "productOriginalPrice": 2550,
  "productPrice": 2550,
  "originalPrice": 2550,
  "discount": 0,
  "price": 2550,
  "productList": [{"productCode":"1100","productName":"巨无霸","quantity":1,
                   "originalSubtotal":2550,"subtotal":2550}],
  "takeWayList": [{"code":"eat-in","title":"堂食"},
                  {"code":"locker-in","title":"外带"}]
}
```

**关键点**：
- **所有价格单位是「分」** —— `price: 2550` = 25.50 元，必须除以 100
- **不传券时返回原价**，而菜单 `currentPrice` 已含活动折扣
  → 因此 `price` > `currentPrice` 的差额，正是当前活动优惠力度
- `discount` 字段为 0 时不代表没优惠，只是本次未叠加券

### 6. `available-coupons` — 可用券（Markdown 格式）

**返回 `data` 是 Markdown 字符串**：

```
### 麦麦省优惠券列表：
- 优惠券标题：中薯条买一送一 \
  状态：可领取 \
  优惠券图片：<img src="https://img.mcd.cn/..." ...>
- 优惠券标题：王牌小食拼 \
  状态：可领取 \
  ...
```

**关键点**：
- 标题行尾的 ` \` 是 Markdown 硬换行残留，需清理
- **本 Skill 只查不领** —— `auto-bind-coupons` 是写操作，受闸门保护

### 7. `campaign-calendar` — 活动日历（Markdown 格式）

**返回 `data` 是 Markdown 字符串**：

```
### 当前时间：2026-10-12 15:03:45
### 活动列表：
#### 2026年10月12日 今日
-   **活动标题**：超值9.9元早餐两件套陪你开工啦
    **活动内容介绍**：早八的快乐，一堡一咖已就位
    **活动图片介绍**：<img src="..." ...>
```

实测某门店：**当月 26 个活动，其中 14 个是今日活动**。

### 8. `now-time-info` — 服务端时间

返回 ISO 8601 字符串，如 `2026-10-12T15:07:19.211`。用服务端时间而非本地时间，避免客户端时钟偏差影响活动窗口判断。

---

## 未使用的 27 个工具

引擎在 `mcp_client.py` 中已按读写分类。

### 只读未用（`READONLY_TOOLS`）

`query-order`（查订单）、`order-list`（历史订单）、`query-my-coupons`（我的券）、`query-store-coupons`（门店可用券）、`delivery-query-addresses`（地址列表）、`delivery-query-stores`（可配送门店）、`query-my-account`（积分账户）、`mall-points-products`（积分商品）、`mall-product-detail`（商品详情）、`mall-order-list` / `mall-order-detail`（商城订单）、`query-meal-assistance`（团餐助餐）、`query-promotions`（团餐满减）、`query-lottery-info`（抽奖信息）、`query-my-prizes`（我的奖品）、`query-survey-coupon`（满意度答卷）、`query-party-city` / `query-party-store` / `query-party-store-date` / `query-party-store-session`（派对四级链路）

### 写操作（`WRITE_TOOLS`，须确认制）

| 工具 | 影响 | 闸门 |
|---|---|---|
| `auto-bind-coupons` | 真实领取券到账户 | 必须 `confirm=True` |
| `create-order` | 真实创建订单 | 必须 `confirm=True` |
| `mall-create-order` | 真实用积分兑换 | 必须 `confirm=True` |
| `delivery-create-address` | 新增收货地址 | 必须 `confirm=True` |
| `cancel-order` | 取消订单 | 必须 `confirm=True` |
| `party-order-create` | 创建派对订单 | 必须 `confirm=True` |
| `draw-lottery` | **消耗积分抽奖** | 必须 `confirm=True` |

代码层已实现闸门：调用这七个工具时若未传 `confirm=True`，`mcp_client.py` 直接抛 `McpError`，**不做任何网络请求**。

```python
client.call_tool("create-order", {...})                 # 抛错，零网络请求
client.call_tool("create-order", {...}, confirm=True)   # 用户确认后才执行
```

> `draw-lottery` 尤其危险——官方描述明确写「服务端原子完成次数或积分扣减」，误调会直接消耗积分。

---

## 字段名兼容策略

官方跨工具的字段命名不统一。`detective.py` 的 `_first()` 做多键 + 大小写不敏感回退：

```python
_first(raw, "calories", "energyKcal", "kcal", "热量")
_first(raw, "currentPrice", "price", "salePrice", "amount")
```

**边界**：只对语义明确的同义字段做兼容映射，绝不猜测未知含义的字段。猜错会让报告出现看似合理的假结论，这比缺一条线索危险得多。

---

## 自定义工具扩展

`tools/list` 会返回官方全量工具。`mcp_client.tool_index()` 可拿到 `{工具名: 描述}`。

若官方新增工具，`detective.py` 会向 stderr 输出告警但不阻断执行：

```
[warn] 工具 XXX 不在本地已知清单中，可能是官方新增工具
```

确认语义后可自行加入 `READONLY_TOOLS` 或 `WRITE_TOOLS`。**加入 `WRITE_TOOLS` 前必须先判断是否有副作用。**

## 协议变更自检

官方随时可能调整字段。建议定期重跑：

```bash
python tests/probe_mcp.py
```

它会依次调用 8 个只读工具并把原始响应落盘到 `tests/probe/`，用于比对字段是否漂移。**该脚本只调用只读工具，绝不触发写操作。**