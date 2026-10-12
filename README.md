# 麦门菜单侦探

**同一个巨无霸，深圳南山 25.50元，上海静安 27.00元。**

差价从哪来？为什么有人说这汉堡"料不够"、有人说"值"？

麦当劳官方 MCP 给你 35 个原子能力，返回的是一堆 JSON。**这个 Skill 把其中 7 个串成一条推理流水线，用真实门店数据回答上面那个问题**——不是计算卡路里，不是帮你凑单，而是反推一份餐品的配料结构与工艺复杂度。

```bash
python scripts/detective.py "巨无霸" --city 深圳 --place 南山
```

<details>
<summary><b>👀 点击展开：真实 MCP 实测输出（巨无霸，深圳南山，2026-10-12）</b></summary>

```
==============================================================
 麦门菜单侦探 · 破案报告
==============================================================
标的餐品：巨无霸
  商品编码：1100
  分类：巨无霸 / 牛鱼肉堡
  MCP 模式：MCP 实时
  工具链：query-nearby-stores -> query-meals -> query-meal-detail
          -> list-nutrition-foods -> calculate-price -> available-coupons
          -> campaign-calendar -> now-time-info
--------------------------------------------------------------

1. L1 营养指纹  [高]热量炸弹
   推断：513 kcal，单品热量已超多数人一餐的 1/3 | 钠 961 mg，
         约为每日推荐上限 2000mg 的 48%
   证据：热量 513 kcal / 蛋白 27.0 g / 脂肪 26.0 g / 碳水 42.0 g
        （匹配营养库条目：巨无霸）
   置信：高

2. L2 配料结构  [中]复配调味
   推断：含 巨无霸酱 等复配调味组分 | 支持 5 项配料自定义，门店需
         现场组装，出品一致性风险更高
   证据：配料 5 项：巨无霸酱、吉士、生菜、酸黄瓜、洋葱粒
   置信：高

3. L3 价格锚点  [低]原价款
   推断：现价 25.50 元，无折扣，属利润款 | 单品、100%纯牛肉
   证据：currentPrice=25.5 originalPrice=25.5
   置信：高

4. L4 组合套利  [高]可用券 7 张
   推断：叠加前最优解：中薯条买一送一、王牌小食拼、巧克力味厚松饼
         猪柳蛋套餐（其中 7 张当前可领取） | calculate-price 回传
         25.50 元，与菜单现价一致
   证据：可用券 7 张 / calculate-price=25.50 元
   置信：中

5. L5 门店差异  [低]区域供应分层
   推断：以「麦当劳深圳桂庙路餐厅」为基准（附近 5 家门店，最近 71 米），
         菜单与券按门店维度差异
   证据：query-nearby-stores
   置信：中

6. L6 时间杠杆  [低]服务端时间
   推断：2026-10-12T15:07:19 | 其中今日 14 个：超值9.9元早餐两件套…
   证据：now-time-info + campaign-calendar（活动 26 个）
   置信：中

--------------------------------------------------------------
【结案陈词】
  本餐品命中 2 条高危线索，核心风险：
    - L1 营养指纹：热量炸弹
    - L4 组合套利：可用券 7 张
==============================================================
```

</details>

**读出三件事**：

| 线索 | 结论 | 数据出处 |
|---|---|---|
| L2 配料结构 | 5 项配料 + 支持现场自定义 → 门店需组装，出品一致性不稳定 | `modification.items` |
| L1 营养指纹 | 513 kcal / 钠 961mg → 单餐已达每日钠上限 48% | `list-nutrition-foods` |
| L4 组合套利 | 7 张券可领，叠加后最优解25.50 元 = 当前菜单价，无隐藏费用 | `available-coupons` + `calculate-price` |

每条结论都带 `evidence` 字段写明用了哪个工具的哪个字段，可逐条验证。推理不含随机性，同一输入必得同一输出。

> 以上为真实 MCP 数据实测输出，非模拟。数据源：2026-10-12 深圳南山麦当劳桂庙路餐厅。

---

## 为什么这个项目不太一样

排行榜上多数项目在做「帮你选得更健康」（热量/营养/减脂）或「帮你花得更省」（凑单/预算/优惠精算）。

本项目做的是**配料与工艺的反推破案**——从配料表的长度和顺序、宏量营养素的供能比、价格在菜单里的位置，反推一份餐品的配方成本与工艺复杂度。

举个例子：配料表里出现 8 项，说明它经历多段加工和冷链组装；配料名里带「风味」「酱」「粉」是复配调味，暴露供应链工业化程度；配料表首位代表主料，是成本主体。这些信息官方数据里都有，但没人把它们串起来讲成一个故事。

**所有结论都带 `evidence` 字段**，写明用了哪个工具的哪个字段，可以逐条验证。推理逻辑不含随机性，同一输入必得同一输出。

### 工程上的三个硬要求

这个项目在实现上守住了三条线，都是踩过坑之后定的：

| 要求 | 做法 |
|---|---|
| **零第三方依赖** | 全部用 Python 标准库（`urllib`），MCP 协议手写实现，`git clone` 完就能跑 |
| **写操作物理隔离** | 7 个写工具（领券/下单/兑换/新增地址…）在代码层硬拦截，主流程零写操作，探针脚本带只读白名单 |
| **零凭据落盘** | Token 只读内存，7 类凭据模式全仓扫描，入库前和推送前各扫一轮 |

前 12 处协议错误全部来自实测——官方文档页是 SPA 壳子抓不到工具清单，社区文档是二手。所有字段假设都验证过，记录在 [`references/mcp-tools.md`](references/mcp-tools.md)。

---

## 六条推理线索

| 线 | 名称 | 回答什么问题 |
|---|---|---|
| L1 | 营养指纹 | 供能比揭示什么工艺主导了这道菜？ |
| L2 | 配料结构 | 配料表暴露了多少工艺复杂度？ |
| L3 | 价格锚点 | 它在菜单里是引流款还是利润款？ |
| L4 | 组合套利 | 单点 / 套餐 / 券，哪个最划算？ |
| L5 | 门店差异 | 区域供应策略有什么分层？ |
| L6 | 时间杠杆 | 当前时点的决策窗口还有多长？ |

每条线索的判定阈值见 [`references/inference-rules.md`](references/inference-rules.md)。

---

## 安装

**要求**：Python 3.8+，**零第三方依赖**（纯标准库）。

```bash
git clone <your-repo-url>
cd mcd-menu-detective
```

### 1. 申请 MCP Token

访问 <https://open.mcd.cn/mcp> → 手机号验证登录 → 右上角控制台 → 点激活 → 复制 Token。

### 2. 配置 Token

**方式一：环境变量（推荐，脚本优先读这个）**

```bash
# Windows PowerShell
$env:MCD_TOKEN = "你的Token"

# macOS / Linux
export MCD_TOKEN="你的Token"
```

**方式二：MCP 客户端配置**

复制 `mcp-config.example.json` 中的配置到你的 MCP 客户端，把 `YOUR_MCP_TOKEN` 换成真实 Token。

> Token 只从环境变量或本地配置读取，**绝不硬编码、绝不落盘到仓库、绝不打印**。

### 3. 验证安装

```bash
# 连通性自检（会列出官方全量工具）
python scripts/mcp_client.py --list-tools

# 完整自测 + 脱敏扫描
python tests/selfcheck.py
```

预期看到 `[OK] 连接成功` 和工具清单。

### 4. 开始使用

```bash
# 最简：自动检索门店 + 破案
python scripts/detective.py "巨无霸" --city 深圳 --place 南山

# 指定餐品
python scripts/detective.py "麦辣鸡腿堡" --city 深圳

# 指定门店（已知 storeCode 时最准）
python scripts/detective.py "巨无霸" --store 1420207

# 麦乐送渠道
python scripts/detective.py "巨无霸" --be-type delivery

# 结构化输出，便于二次加工
python scripts/detective.py "巨无霸" --json

# 无 Token 时体验完整链路（内置样例数据）
python scripts/detective.py "巨无霸" --offline
```

### 参数说明

| 参数 | 默认 | 说明 |
|---|---|---|
| 关键词 | 巨无霸 | 餐品名。精确同名优先，单品优先于套餐 |
| `--city` | 深圳 | 城市，配合 `--place` 定位门店 |
| `--place` | 城市名 | 位置关键词，如「南山」「腾讯大厦」 |
| `--store` | 自动检索 | 门店编码 storeCode |
| `--be-type` | pickup | `pickup` 到店自取 / `drive` 得来速 / `delivery` 麦乐送 |
| `--json` | 关 | 输出结构化 JSON |
| `--offline` | 关 | 强制离线降级，不联网 |

### 在 WorkBuddy 中使用

把本目录作为 Skill 安装，然后在对话中说「用麦门菜单侦探分析一下巨无霸」。

### 在 Claude Desktop 中使用

把 `mcp-config.example.json` 内容合并进 `claude_desktop_config.json` 的 `mcpServers`，替换 Token 占位符，重启客户端。

---

## 使用示例

### 例 1：想知道某套餐的性价比

```bash
python scripts/detective.py "巨无霸四件套" --city 深圳
```

看 L3 价格锚点：系统会自动对比 `currentPrice` 与 `originalPrice`，算出活动折扣力度。巨无霸单品原价 25.5 元无折扣，但四件套原价 58.5 元现价 32 元——**套餐的折扣才是真正的优惠来源**。

### 例 2：横向对比多款餐品

```bash
python scripts/detective.py "巨无霸" --json > a.json
python scripts/detective.py "麦辣鸡腿堡" --json > b.json
python scripts/detective.py "中薯条" --json > c.json
```

JSON 里的 `clues` 数组可直接做程序化对比，比如按「每 100 kcal 售价」排序找性价比最高的。

### 例 3：看活动期该不该等

L6 时间杠杆会拉取服务端时间和在档活动。实测某门店当月 26 个活动、其中 14 个是今日活动。活动要结束时决策窗口就会关闭。

---

## 目标用户

| 用户 | 场景 |
|---|---|
| 关注饮食的普通用户 | 想搞清一份快餐的真实构成，而不是看营销话术 |
| 食品/餐饮从业者 | 研究连锁快餐的配方设计与定价策略 |
| 营养师、健康管理师 | 需要快速的多产品营养横向对比 |
| 对供应链好奇的开发者 | 观察一个 8 配料 SKU 背后的工业化程度 |
| MCP/Agent 开发者 | 参考一个零依赖的 Streamable HTTP 客户端实现 |

---

## 项目结构

```
mcd-menu-detective/
├── SKILL.md                      Skill 定义与工作流
├── README.md                     本文件
├── CONTEST_DECLARATION.md        参赛声明（官方原文）
├── MCP_INTEGRATION.md            MCP 集成与业务价值
├── mcp-config.example.json       脱敏配置示例
├── workbuddy.md                  开发对话时间线
├── scripts/
│   ├── mcp_client.py             MCP 客户端 + 安全闸门
│   ├── mcp_formats.py            四种返回格式适配层
│   └── detective.py              推理引擎
├── references/
│   ├── inference-rules.md        六线推理规则全文
│   └── mcp-tools.md              工具清单与实测字段映射
└── tests/
    ├── selfcheck.py              本地自测 + 脱敏扫描
    └── probe_mcp.py              真实 MCP 字段探测（需 Token）
```

---

## 安全设计

| 约束 | 实现 |
|---|---|
| Token 零落盘 | 只从 `MCD_TOKEN` 环境变量读，不写文件、不打印、不提交 |
| 默认只读 | 主流程只调用 8 个只读工具 |
| 写操作硬闸 | `create-order` 等 4 个写工具缺 `confirm=True` 时，**在发起网络请求前**抛错 |
| 降级必标注 | MCP 不可用时报告头部显示「降级模式」，末尾列降级告警 |
| 不编造数据 | MCP 未返回的字段留 `None`，报告显示 `N/A` |
| 结论可追溯 | 每条线索带 `evidence` 字段 |
| 免责声明 | 报告固定输出「非官方结论、不构成营养或医疗建议」 |

```python
client.call_tool("create-order", {...})                 # 抛错，零网络请求
client.call_tool("create-order", {...}, confirm=True)   # 用户确认后才执行
```

---

## 扩展

新增一条推理线索只需两步：

1. 在 `detective.py` 写 `clue_LN_xxx()` 函数，返回含 `clue`/`verdict`/`detail`/`severity`/`evidence`/`confidence` 的 dict，返回 `None` 表示数据不足
2. 在 `investigate()` 的 `clues` 列表里加上它

`render_report` 无需改动。

---

## 参与竞赛

本项目是**麦当劳程序员创意开发大赛**参赛作品。

- 仓库：https://github.com/bgx520/mcd-menu-detective
- 榜单：https://github.com/M-China/mcd-developer-innovation-challenge/blob/main/RANKING.md

如果这个项目对你有用，欢迎点一颗 Star ⭐ —— 每颗 Star 都是对独立开发者的直接鼓励，也是竞赛排名的依据。

**也欢迎提Issue**：字段假设错误、线索判断偏了、想加新的推理维度，都欢迎拍砖。Issue 比 Star 更值钱。

---

## 免责声明

本项目为麦当劳程序员创意开发大赛参赛作品，由参赛者独立开发，**非麦当劳官方产品**。

项目输出仅供参考，**不构成医疗、营养或其他专业建议**。餐品信息、价格及供应状态以麦当劳官方渠道的实时结果为准。本项目不将麦当劳与其他品牌作对比。

---

## 许可证

MIT