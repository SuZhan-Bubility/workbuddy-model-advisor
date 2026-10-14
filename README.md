# workbuddy-model-advisor

> 给 **WorkBuddy / CodeBuddy** 用的「开工前选模型」技能 —— 按任务类型推荐最合适的大模型与思考强度，出一张双角度推荐卡，然后**停下来等你切好模型再动手**。

**关键词**：WorkBuddy 技能 · skill · 模型推荐 · 大模型选择 · 思考强度 · 性价比 · 积分费率 · 推荐卡 · CodeBuddy

> **适用范围：WorkBuddy / CodeBuddy（腾讯 AI Agent 工作台）。**
> 费率数据来自该客户端在本机留下的配置缓存，交互依赖它的工具与界面。
> 核心算法（打分 + 渲染）与平台无关，移植到其它平台只需替换数据源、
> 并按目标平台改写工具名——详见下方「移植到其它平台」。

## 它解决什么问题

AI 工作台里往往挂着几十个可选模型，费率和能力差几十倍。选错了：

- 便宜模型干重活 → 结果不行，返工更贵
- 贵模型干轻活 → 白烧积分

这个 skill 让 Agent 在开工前先给出一张**推荐卡**，把"选哪个模型"变成一个明确的检查点——你看完、切好模型，它再动手。

## 推荐卡长什么样

卡片左右两栏，回答两个不同的问题：

| 角度 | 回答的问题 | 成本口径 |
|---|---|---|
| **做到最好** | 不管花多少钱，哪个能做到最优？ | 按原价算 |
| **性价比最高** | 够用又最划算，用哪个？ | 按折扣价算 |

每个推荐带：费率、上下文长度、性价比星级、**建议的思考强度**、一句话理由。

## 怎么用

### 1. 安装

把整个目录放到 skills 目录下：

| 平台 | 落点 | 支持程度 |
|---|---|---|
| WorkBuddy / CodeBuddy | `~/.workbuddy/skills/workbuddy-model-advisor/` | 原生完整支持 |
| 其它支持 Agent Skills 的工具 | 对应 skills 目录 | 见下方「移植到其它平台」 |

### 移植到其它平台

这个 skill 分三层，通用性不同：

| 层次 | 通用性 | 移植时的动作 |
|---|---|---|
| `scripts/` 打分与渲染逻辑 | ✅ 完全通用 | 直接用（纯 Python 标准库，零依赖） |
| 数据源 | ❌ WorkBuddy 专属 | 用 `MODEL_ADVISOR_CONFIG` 环境变量指向你自己的模型清单 JSON |
| 交互方式 | ❌ WorkBuddy 专属 | 改写 SKILL.md 里的工具名（`present_files`）与「界面切模型」表述 |

替换数据源的 JSON 格式（脚本读取 `models` 数组）：

```json
{
  "models": [
    {
      "id": "your-model-id",
      "name": "Your Model Name",
      "credits": "x1.00 credits",
      "maxInputTokens": 128000,
      "supportsImages": true,
      "supportsToolCall": true,
      "reasoning": { "effort": "medium" }
    }
  ]
}
```

设好环境变量即可：

```bash
export MODEL_ADVISOR_CONFIG=/path/to/your-models.json
python scripts/recommend.py --list
```

### 2. 触发

在任务提示词里加一句 **「推荐大模型」**，例如：

```
帮我批量抓取这批论文，推荐大模型
```

Agent 会：生成推荐卡 → 打开给你看 → **停下来等你切模型** → 你回一句「继续」才开工。

> 触发是**手动的**。它不会自动弹卡，也不会挂载到其它 skill 上。

### 3. 也可以单独跑

```bash
python scripts/recommend.py --list                    # 列出本机全部可用模型与费率
python scripts/recommend.py --task-type crape         # 爬取类推荐
python scripts/recommend.py --task-type research      # 研究类推荐
python scripts/recommend.py --task-type survey        # 调研类推荐
python scripts/render_card.py --task-type crape --out card.html   # 生成 HTML 推荐卡
```

三种任务类型的定位：

- `crape` — 批量抓取/爬取（量大、重复、成本敏感）
- `research` — 系统性深度研究（量少、要深、长链条论证）
- `survey` — 调研/综述（多源整合、要读图表、结构化输出）

## 数据来源与适用条件（重要）

**实时费率来自 WorkBuddy 桌面客户端的本机缓存配置。**

| 情况 | 行为 |
|---|---|
| 装了 WorkBuddy 客户端 | 自动读取模型清单与费率，随官方调价自动更新 |
| 没装 | 回退到内置兜底清单（`FALLBACK_MODELS`），仍能给出推荐，但费率需人工核对 |
| 想用自己的数据 | 设环境变量 `MODEL_ADVISOR_CONFIG` 指向一个 `{"models": [...]}` 格式的 JSON 文件 |

官方没有逐模型的费率页面，也没有费率 API。本项目的做法是读取客户端在本机留下的
配置文件（由服务端下发），因此费率能跟上官方调整，但**不保证与实际扣费完全一致**。

## 免责声明

- 本项目**非官方**，与腾讯 / WorkBuddy / CodeBuddy 无任何关联。
- 费率数据来自本机客户端缓存，**仅供参考**，实际计费以官方页面为准。
- 模型能力评分（`scripts/recommend.py` 中的 `ABILITY` 字典）是**主观估计**，非权威榜单。
- 使用前请自行确认是否符合你所使用服务的条款与当地法律法规。

## 工作原理

### 双角度打分

```
best  角度：能力权重高，成本按【原价】算 → 不计成本求最优
value 角度：成本权重高，成本按【折扣价】算 → 错峰后的真实划算度
```

权重表见 `scripts/recommend.py` 的 `TASK_PROFILES`。

### 折扣档识别

脚本会扫描客户端留下的一批历史快照（每份带时间戳），对每个模型汇总出现过的
所有费率数值：

**同名模型出现 ≥2 个不同数值 → 判定存在折扣**，取最大值为原价、最小值为折扣价。

这样即使官方调价或做活动，卡片上展示的"原价 → 折扣"两档也会自动跟上。

### 思考强度

配置里有每个模型的**默认思考强度**（low / medium / high），但**没有**"各档对应
多少费率"的表格。脚本用经验系数把它折算进"有效成本"：

```
minimal 1.0 / low 1.1 / medium 1.25 / high 1.6
```

这让"默认开高档"的模型在性价比角度上被适当压低。系数是估计值，不是精确倍率。

## 目录结构

```
workbuddy-model-advisor/
├── SKILL.md                      # 技能主文件（Agent 读的说明书）
├── README.md                     # 本文件
├── LICENSE
├── references/
│   └── model-profiles.md         # 模型能力档案（写推荐理由时参考）
└── scripts/
    ├── recommend.py              # 读配置 → 双角度打分 → 输出 JSON
    └── render_card.py            # 渲染自包含 HTML 推荐卡
```

## 自定义

| 想改什么 | 改哪里 |
|---|---|
| 任务类型与权重 | `scripts/recommend.py` → `TASK_PROFILES` |
| 模型能力评分 | `scripts/recommend.py` → `ABILITY` |
| 兜底模型清单 | `scripts/recommend.py` → `FALLBACK_MODELS` |
| 思考强度折算系数 | `scripts/recommend.py` → `EFFORT_COST_FACTOR` |
| 卡片配色 | `scripts/render_card.py` → `THEME` |
| 触发词 | `SKILL.md` 的 `description` 与「触发时机」节 |

## 依赖

- Python 3.8+（仅用标准库，无需 pip 安装任何包）

## License

MIT
