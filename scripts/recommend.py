#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workbuddy-model-advisor 核心脚本
从本机 WorkBuddy 配置实时读取可用模型清单与计费，按任务类型打分排序，
**给出两个角度的推荐**：
  1) best    —— 做到最好的模型 + 思考强度
  2) value   —— 性价比最高的模型 + 思考强度
输出 JSON。

用法:
    python recommend.py --task-type crape
    python recommend.py --task-type auto --query "帮我爬这批论文"
    python recommend.py --list
    python recommend.py --task-type research --json
"""
import argparse
import json
import os
import re
import sys

# ---- 数据源 ----
# 本脚本的模型清单与费率来自「本机配置文件」，按以下顺序查找：
#   1) 环境变量 MODEL_ADVISOR_CONFIG 指向的 JSON 文件（格式：{"models": [...]}）
#   2) WorkBuddy 客户端的本机产品配置缓存（默认路径，跨平台）
# 若两者都读不到，脚本回退到内置的 FALLBACK_MODELS 兜底清单（见下方），仍可给出推荐。
_WB_CACHE = os.path.join(os.path.expanduser("~"), ".workbuddy", "cache")

CONFIG_CANDIDATES = [
    os.environ.get("MODEL_ADVISOR_CONFIG", ""),
    os.path.join(_WB_CACHE, "acc-product-config-v3.json"),
]
CONFIG_CANDIDATES = [p for p in CONFIG_CANDIDATES if p]

# 历史费率快照目录（客户端每几小时落一份；服务端调价/折扣变更后随之更新）
# 用于识别同一模型的「原价 / 折扣价」两档。不存在时不影响主流程。
SPILL_DIR = os.path.join(_WB_CACHE, "conversation-product-spill")

# ---- 时段规则（来源：官方活动描述 + 本机快照交叉验证）----
# 高峰（原价）: 周一至周五 09:00-12:00 与 14:00-18:00
# 非高峰（折扣）: 其余所有时间（含工作日午休/晚间/夜间，以及周末全天）
# 说明：该规则来自活动文案，不是本机配置字段；本机配置只提供"当前生效的费率数值"，
#      两者互补 —— 时段规则用来"预判何时便宜"，配置数值用来"确认现在多少钱"。
PEAK_WEEKDAY_RANGES = [(9, 12), (14, 18)]


def is_peak_now(now=None):
    """判断当前是否处于高峰（原价）时段。"""
    import datetime
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:  # 周六/周日全天非高峰
        return False
    for a, b in PEAK_WEEKDAY_RANGES:
        if a <= now.hour < b:
            return True
    return False


def load_snapshots(max_files=400):
    """读取本机全部产品配置快照，返回 [(mtime, {name: credits_raw})]，按时间升序。"""
    import glob
    snapshots = []
    if not os.path.isdir(SPILL_DIR):
        return snapshots
    files = sorted(glob.glob(os.path.join(SPILL_DIR, "*.json")),
                   key=os.path.getmtime)[-max_files:]
    for p in files:
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            m = {}
            for x in d.get("models", []):
                n = x.get("name")
                if n:
                    m.setdefault(n, (x.get("credits") or "").strip())
            snapshots.append((os.path.getmtime(p), m))
        except Exception:
            continue
    return snapshots


def detect_fee_plans(snapshots):
    """
    从历史快照中聚类出"费率方案"，并找出每个方案最后一次出现的时间。
    返回 {plan_key: {"models": {...}, "last_seen": ts, "first_seen": ts, "samples": n}}
    方案 key 用若干代表模型的费率拼接而成，足够区分不同方案。
    """
    import datetime
    key_models = ("Deepseek-V4-Pro", "GLM-5.2", "Kimi-K3", "Deepseek-V4-Flash")
    plans = {}
    for ts, m in snapshots:
        key = "|".join(str(m.get(n, "-")) for n in key_models)
        if key not in plans:
            plans[key] = {"models": m, "first_seen": ts, "last_seen": ts, "samples": 0}
        plans[key]["last_seen"] = ts
        plans[key]["samples"] += 1
        plans[key]["models"] = m  # 保留最近一次
    return plans


def current_plan_info(snapshots):
    """
    取"最近一次快照"作为当前生效费率，并报告其时间戳；
    同时给出该快照相对其它方案的定位（用于判断现在是否处于折扣档）。
    """
    import datetime
    if not snapshots:
        return None
    ts, m = snapshots[-1]
    return {
        "captured_at": datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
        "captured_ts": ts,
        "models": m,
    }

EXCLUDE_KEYWORDS = (
    "kling", "image", "completion", "codewise", "hunyuan-3b", "hunyuan-7b",
    "nes", "jump", "rewrite", "taiji", "default-1.1", "default-1.2",
    "hunyuan-image", "spark",
)

# ---- 任务类型定义 ----
# best 权重 = 能力优先；value 权重 = 能力÷成本优先
TASK_PROFILES = {
    "crape": {
        "name": "批量爬取（论文/文章）",
        "features": "量大 · 重复 · 单篇不长 · 成本敏感",
        "best_weights":  {"ability": 0.40, "context": 0.35, "cost": 0.25},
        "value_weights": {"ability": 0.28, "context": 0.27, "cost": 0.45},
        "prefer_visual": False,
    },
    "research": {
        "name": "系统级研究（深度推理）",
        "features": "量少 · 要深 · 跨文献串因果 · 长链条论证",
        "best_weights":  {"ability": 0.65, "context": 0.25, "cost": 0.10},
        "value_weights": {"ability": 0.42, "context": 0.23, "cost": 0.35},
        "prefer_visual": False,
    },
    "survey": {
        "name": "产业链调研（读图+结构化）",
        "features": "多源整合 · 要读图表截图 · 要出结构化结论",
        "best_weights":  {"ability": 0.45, "visual": 0.25, "context": 0.20, "cost": 0.10},
        "value_weights": {"ability": 0.32, "visual": 0.23, "context": 0.20, "cost": 0.25},
        "prefer_visual": True,
    },
    "ppt": {
        "name": "PPT / 演示文稿生成",
        "features": "长文压成大纲 · 要读图排版 · 要结构化输出",
        "best_weights":  {"ability": 0.50, "visual": 0.22, "context": 0.20, "cost": 0.08},
        "value_weights": {"ability": 0.38, "visual": 0.20, "context": 0.18, "cost": 0.24},
        "prefer_visual": True,
    },
    "video": {
        "name": "视频生成（脚本/分镜/剪辑）",
        "features": "创意脚本 · 分镜描述 · 多模态理解 · 依赖工具调用",
        "best_weights":  {"ability": 0.48, "visual": 0.24, "context": 0.16, "cost": 0.12},
        "value_weights": {"ability": 0.36, "visual": 0.22, "context": 0.15, "cost": 0.27},
        "prefer_visual": True,
    },
    "code": {
        "name": "写代码 / 调试 / 重构",
        "features": "代码能力优先 · 要读长上下文 · 常需多轮改",
        "best_weights":  {"ability": 0.62, "context": 0.28, "cost": 0.10},
        "value_weights": {"ability": 0.45, "context": 0.25, "cost": 0.30},
        "prefer_visual": False,
        # 代码场景单独校准：代码专精型号上浮，通用旗舰维持原档
        "ability_override": {
            "kimi-k2.7": 0.94, "kimi-k3-1": 0.92, "deepseek-v4-pro": 0.90,
            "glm-5.2": 0.89, "minimax-m3": 0.82, "deepseek-v4-flash": 0.74,
        },
    },
    "writing": {
        "name": "写报告 / 文档 / 文案",
        "features": "中文表达 · 结构清晰 · 长短可控 · 偶要读资料",
        "best_weights":  {"ability": 0.55, "context": 0.30, "cost": 0.15},
        "value_weights": {"ability": 0.42, "context": 0.26, "cost": 0.32},
        "prefer_visual": False,
    },
    "general": {
        "name": "通用任务（未识别类型）",
        "features": "无明确倾向 · 日常问答与轻量处理",
        "best_weights":  {"ability": 0.50, "context": 0.30, "cost": 0.20},
        "value_weights": {"ability": 0.38, "context": 0.27, "cost": 0.35},
        "prefer_visual": False,
    },
}

# 手工能力档（结合榜单印象 + 本机型号定位），0~1
ABILITY = {
    "kimi-k3-1": 0.95, "glm-5.2": 0.93, "glm-5.1": 0.85, "glm-5.0": 0.80,
    "deepseek-v4-pro": 0.88, "deepseek-v4-flash": 0.72, "minimax-m3": 0.82,
    "minimax-m2.5": 0.65, "minimax-m2.7": 0.68, "glm-5v-turbo": 0.78,
    "glm-5.0-turbo": 0.76, "kimi-k2.7": 0.80, "kimi-k2.6": 0.72,
    "kimi-k2-thinking": 0.75, "deepseek-v3-2-volc": 0.62, "glm-4.7": 0.60,
    "glm-4.6": 0.55, "glm-4.6v": 0.58, "hy3": 0.58,
    "hunyuan-2.0-thinking": 0.55, "hunyuan-chat": 0.45,
    "default": 0.80, "auto": 0.60, "hy3-preview": 0.58,
}

EFFORT_RANK = {"minimal": 0, "low": 1, "medium": 2, "high": 3}
EFFORT_LABEL_CN = {"minimal": "极低", "low": "低", "medium": "中", "high": "高"}

# 任务对"思考强度"的需求 —— 强度由任务类型决定，这是设计而非缺陷
EFFORT_DEMAND = {
    "crape": "low",       # 批量逐条处理，不需要长时间思考
    "research": "high",   # 深度推理，长链条论证
    "survey": "medium",   # 多源整合，中等推理
    "ppt": "medium",      # 结构化整理
    "video": "medium",    # 创意脚本与分镜
    "code": "high",       # 推理深度直接影响代码正确率
    "writing": "medium",  # 写作表达
    "general": "medium",  # 通用任务
}

# 每个任务类型"为什么是这个强度"的一句话解释（写进卡片）
EFFORT_REASON = {
    "crape": "批量逐条处理，单条不需要长时间思考，开高了只是多烧积分",
    "research": "深度推理任务，长链条论证，强度直接影响结论质量",
    "survey": "多源整合类任务，中等推理足够，复杂图表时可临时调高",
    "ppt": "把长文压成结构，中等推理足够，重点在排版与取舍",
    "video": "创意脚本与分镜，中等推理足够，重点在画面描述",
    "code": "代码任务容错低，推理越深越少返工",
    "writing": "写作类任务，中等推理足够，重点在表达与结构",
    "general": "通用任务，中等档通常是最稳妥的平衡点",
}


def load_models():
    cfg_path = None
    for p in CONFIG_CANDIDATES:
        if os.path.exists(p):
            cfg_path = p
            break

    if cfg_path:
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("models", [])

            # 从历史快照里提取"折扣档"费率：取所有快照中同名字段出现过的不同数值，
            # 数值最小的视为折扣价（非高峰价），最大的视为原价（高峰价）。
            snapshots = load_snapshots()
            plan = current_plan_info(snapshots)
            fee_alt = build_fee_alt(snapshots)

            models = []
            for m in raw:
                mid = m.get("id", "")
                if not mid or any(k in mid.lower() for k in EXCLUDE_KEYWORDS):
                    continue
                name = m.get("name", mid)
                r = m.get("reasoning") or {}
                alt = fee_alt.get(name, {})
                models.append({
                    "id": mid,
                    "name": name,
                    # credits_raw 是配置里的**原文**，永远原样展示，杜绝转述失真
                    "credits_raw": (m.get("credits") or "").strip(),
                    # credits 是解析出的**倍率数值**（以 Default 基准）；无标记为 None
                    "credits": parse_credits(m.get("credits", "")),
                    # 历史快照里出现过的其它费率（用于呈现"白天价/夜间价"）
                    "credits_peak": alt.get("peak"),
                    "credits_offpeak": alt.get("offpeak"),
                    "ctx": m.get("maxInputTokens") or 0,
                    "img": bool(m.get("supportsImages")),
                    "reason": bool(m.get("supportsReasoning")),
                    "tool": bool(m.get("supportsToolCall")),
                    "default_effort": r.get("effort", "medium"),
                })
            if models:
                return models, cfg_path, plan, fee_alt
        except Exception as e:
            sys.stderr.write(f"[warn] 读取配置失败: {e}\n")

    return FALLBACK_MODELS, "(内置兜底清单)", None, {}


def build_fee_alt(snapshots):
    """
    对每个模型，汇总历史快照里出现过的全部费率数值，返回
    {name: {"peak": 高价, "offpeak": 低价, "values": [...]}}。
    只有出现 ≥2 个不同数值时才认为该模型存在折扣。
    """
    seen = {}
    for _, m in snapshots:
        for n, c in m.items():
            v = parse_credits(c)
            if v is None:
                continue
            seen.setdefault(n, set()).add(v)
    out = {}
    for n, vs in seen.items():
        vals = sorted(vs)
        if len(vals) >= 2:
            out[n] = {"peak": vals[-1], "offpeak": vals[0], "values": vals}
    return out


def parse_credits(s):
    if not s:
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(s))
    return float(m.group(1)) if m else None


FALLBACK_MODELS = [
    {"id": "deepseek-v4-flash", "name": "DeepSeek-V4-Flash", "credits": 0.06, "credits_raw": "x0.06", "ctx": 1000000, "img": True, "reason": True, "tool": True, "default_effort": "high"},
    {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro", "credits": 0.16, "credits_raw": "x0.16", "ctx": 1000000, "img": True, "reason": True, "tool": True, "default_effort": "high"},
    {"id": "minimax-m3", "name": "MiniMax-M3", "credits": 0.25, "credits_raw": "x0.25", "ctx": 512000, "img": True, "reason": True, "tool": True, "default_effort": "medium"},
    {"id": "glm-5.2", "name": "GLM-5.2", "credits": 0.79, "credits_raw": "x0.79", "ctx": 1000000, "img": True, "reason": True, "tool": True, "default_effort": "medium"},
    {"id": "kimi-k3-1", "name": "Kimi-K3", "credits": 1.62, "credits_raw": "x1.62", "ctx": 1000000, "img": True, "reason": True, "tool": True, "default_effort": "medium"},
    {"id": "glm-5v-turbo", "name": "GLM-5v-Turbo", "credits": 0.60, "credits_raw": "视觉档", "ctx": 200000, "img": True, "reason": True, "tool": True, "default_effort": "medium"},
    {"id": "hy3", "name": "Hy3", "credits": 0.0, "credits_raw": "x0.00", "ctx": 192000, "img": True, "reason": True, "tool": True, "default_effort": "high"},
]


def norm(values, reverse=False):
    vals = [v for v in values if v is not None]
    if not vals:
        return {}
    lo, hi = min(vals), max(vals)
    out = {}
    for v in set(values):
        if v is None:
            continue
        elif hi == lo:
            out[v] = 1.0
        else:
            r = (v - lo) / (hi - lo)
            out[v] = (1 - r) if reverse else r
    return out


# 思考强度对实际 token 消耗的经验系数（配置无倍率表，此为相对估计，非精确值）
EFFORT_COST_FACTOR = {"minimal": 1.0, "low": 1.1, "medium": 1.25, "high": 1.6}


def score_models(models, task_type, angle, use_offpeak=False):
    """angle = 'best' 或 'value'。返回排序后的列表。
    use_offpeak=True 时，成本维度按"折扣价"计算（用于展示错峰后的性价比）。
    """
    prof = TASK_PROFILES[task_type]
    w = prof[f"{angle}_weights"]

    def _cost_of(m):
        """取用于计分的成本：优先折扣价（若启用且存在），否则当前标价。"""
        if use_offpeak and m.get("credits_offpeak") is not None:
            return m["credits_offpeak"]
        return m["credits"] if m["credits"] is not None else m.get("credits_offpeak")

    known = [_cost_of(m) for m in models if _cost_of(m) is not None]
    mid = sorted(known)[len(known) // 2] if known else 0.5
    costs = [_cost_of(m) if _cost_of(m) is not None else mid for m in models]
    cost_score = norm(costs, reverse=True)

    ctxs = [m["ctx"] for m in models]
    ctx_score = norm(ctxs)

    results = []
    for m in models:
        ab = prof.get("ability_override", {}).get(m["id"])
        if ab is None:
            ab = ABILITY.get(m["id"], 0.55)
        vis = 0.9 if (prof["prefer_visual"] and m["img"]) else (0.5 if m["img"] else 0.2)
        ec = EFFORT_COST_FACTOR.get(m["default_effort"], 1.25)
        raw_cost = _cost_of(m) if _cost_of(m) is not None else mid
        eff_cost = raw_cost * ec  # 有效成本 = 名义单价 × 强度系数

        raw_cost_score = cost_score.get(raw_cost, 0.5)
        # 强度越高，成本维度得分越低
        cost_v = raw_cost_score / ec

        s = (
            w.get("cost", 0) * cost_v
            + w.get("context", 0) * ctx_score.get(m["ctx"], 0.5)
            + w.get("ability", 0) * ab
            + w.get("visual", 0) * vis
        )
        if not m["tool"]:
            s *= 0.3
        results.append({**m, "ability": ab, "eff_cost": round(eff_cost, 4), "score": round(s, 4)})

    if angle == "value":
        for r in results:
            r["value_ratio"] = round(r["ability"] / r["eff_cost"], 3) if r["eff_cost"] > 0 else 99

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def star(score, top):
    if top <= 0:
        return 3
    r = score / top
    if r >= 0.97:
        return 5
    if r >= 0.90:
        return 4
    if r >= 0.82:
        return 3
    if r >= 0.72:
        return 2
    return 1


def suggest_effort(model, task_type):
    """
    强度口径（2026-10-17 起）：
      - 任务类型决定"需求强度"，但**不强制用户去改**
      - 卡片一律按模型的**默认强度**给建议（"用默认，不用调"）
      - 只有需求与默认相差 ≥2 档时，才追加一句可选提示
    这样卡片上的性价比分数（本来就按默认强度算）与用户实际花费完全一致。
    """
    demand = EFFORT_DEMAND.get(task_type, "medium")
    default = model.get("default_effort", "medium")
    dr, mv = EFFORT_RANK.get(demand, 2), EFFORT_RANK.get(default, 2)
    gap = dr - mv

    hint = ""
    if gap >= 2:
        hint = (f"这类任务偏重深度推理，默认「{EFFORT_LABEL_CN.get(default, default)}」"
                f"可能不够扎实，可手动升到「{EFFORT_LABEL_CN.get(demand, demand)}」；不升也能跑。")
    elif gap <= -2:
        hint = (f"这类任务偏轻，默认「{EFFORT_LABEL_CN.get(default, default)}」"
                f"会多耗积分，可手动降到「{EFFORT_LABEL_CN.get(demand, demand)}」；不降也能跑。")

    return {
        "demand": demand,                                  # 该任务类型的需求强度
        "model_default": default,                          # 该模型默认强度
        "use": default,                                    # 建议实际使用：就是默认值
        "reason": EFFORT_REASON.get(task_type, ""),        # 为什么这个任务是这个强度
        "hint": hint,                                      # 仅差距≥2档时非空
    }


def build_card(task_type, models, plan=None):
    prof = TASK_PROFILES[task_type]
    out = {
        "task_type": task_type,
        "task_name": prof["name"],
        "features": prof["features"],
        "angles": {"best": None, "value": None},
    }

    import datetime
    now = datetime.datetime.now()
    peak = is_peak_now(now)
    out["time_context"] = {
        "now": now.strftime("%Y-%m-%d %H:%M %A"),
        "is_peak": peak,
        "period": "高峰原价时段" if peak else "非高峰折扣时段",
        "rule": "工作日 09:00-12:00 / 14:00-18:00 为高峰原价，其余时间为非高峰折扣",
    }
    if plan:
        out["fee_snapshot"] = {
            "captured_at": plan["captured_at"],
            "note": "费率数值取自本机服务端下发快照（会自动更新）；时段判定按官方活动规则",
        }

    for angle in ("best", "value"):
        # best 角度：成本维度按原价算，突出"不计成本做到最好"
        # value 角度：成本维度按折扣价算，突出"错峰后的真实性价比"
        use_offpeak = (angle == "value")
        ranked = score_models(models, task_type, angle, use_offpeak=use_offpeak)
        priced = [m for m in ranked if m["credits"] is not None]
        pool = priced if len(priced) >= 1 else ranked
        top = pool[0]["score"] if pool else 1
        picks = pool[:2]
        out["angles"][angle] = {
            "label": "做到最好" if angle == "best" else "性价比最高",
            "picks": [],
        }
        for i, m in enumerate(picks):
            pk = {
                "rank": i + 1,
                "id": m["id"],
                "name": m["name"],
                "credits": m["credits_raw"] or "未标价",
                "ctx": m["ctx"],
                "img": m["img"],
                "stars": star(m["score"], top),
                "effort": suggest_effort(m, task_type),
            }
            # 若该模型历史上有折扣价，把"原价/折扣价"一并给出，并说明按哪个口径评分
            if m.get("credits_peak") is not None and m.get("credits_offpeak") is not None:
                pk["credits_peak"] = m["credits_peak"]
                pk["credits_offpeak"] = m["credits_offpeak"]
                pk["discount_pct"] = round(
                    (1 - m["credits_offpeak"] / m["credits_peak"]) * 100)
                pk["scored_on"] = "offpeak" if angle == "value" else "peak"
            out["angles"][angle]["picks"].append(pk)
    return out


def detect_task(query):
    """按关键词判定任务类型。顺序即优先级：越具体的越靠前。"""
    q = (query or "").lower()
    rules = [
        ("video",    ["视频", "剪辑", "分镜", "短视频", "数字人", "video"]),
        ("ppt",      ["ppt", "幻灯片", "演示文稿", "汇报材料", "slide", "deck"]),
        ("crape",    ["爬", "抓取", "批量", "采集", "收集", "爬虫", "fetch", "crawl"]),
        ("code",     ["代码", "脚本", "编程", "函数", "重构", "报错", "debug", "bug"]),
        ("research", ["研究", "分析", "推理", "论证", "系统级", "机理", "原理"]),
        ("survey",   ["调研", "产业链", "产业", "市场", "图谱", "survey"]),
        ("writing",  ["写报告", "报告", "文案", "写作", "润色", "文章", "公众号", "文档", "总结", "写一份"]),
    ]
    for tt, kws in rules:
        if any(k in q for k in kws):
            return tt
    return "general"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-type", default=None,
                    choices=["crape", "research", "survey", "ppt", "video",
                             "code", "writing", "general", "auto"])
    ap.add_argument("--query", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    models, src, plan, fee_alt = load_models()

    if args.list:
        print(f"数据源: {src}")
        if plan:
            print(f"费率快照时间: {plan['captured_at']}（本机服务端下发，自动更新）")
        print(f"可用对话模型: {len(models)} 个\n")
        for m in sorted(models, key=lambda x: (x["credits"] if x["credits"] is not None else 99)):
            c = m["credits_raw"] or "-"
            extra = ""
            if m.get("credits_peak") is not None and m.get("credits_offpeak") is not None:
                extra = f" | 原价x{m['credits_peak']} / 折扣x{m['credits_offpeak']}"
            print(f"{m['id']:24s} | {m['name']:22s} | {c:14s} | ctx={m['ctx']:>8d} | img={'Y' if m['img'] else 'N'} | 默认强度={m['default_effort']}{extra}")
        return

    tt = args.task_type
    if tt == "auto" or tt is None:
        tt = detect_task(args.query or "")
        if tt not in TASK_PROFILES:
            tt = "general"

    card = build_card(tt, models, plan)
    card["data_source"] = src

    if args.json:
        print(json.dumps(card, ensure_ascii=False, indent=2))
    else:
        tc = card.get("time_context", {})
        print(f"任务类型: {card['task_name']}")
        print(f"特征: {card['features']}")
        print(f"数据源: {src}")
        if plan:
            print(f"费率快照: {plan['captured_at']}（本机服务端下发，自动更新）")
        print(f"当前时段: {tc.get('now','')} — {tc.get('period','')}")
        print(f"          规则：{tc.get('rule','')}\n")
        for angle in ("best", "value"):
            a = card["angles"][angle]
            print(f"【{a['label']}】")
            for p in a["picks"]:
                e = p["effort"]
                print(f"  #{p['rank']} {p['name']} | {p['credits']} | ctx={p['ctx']//1000}K | {'★'*p['stars']}")
                if p.get("credits_peak") is not None:
                    basis = "按折扣价评分" if p.get("scored_on") == "offpeak" else "按原价评分"
                    print(f"      费率：原价 x{p['credits_peak']} / 折扣 x{p['credits_offpeak']}（省 {p['discount_pct']}%，本推荐{basis}）")
                print(f"      思考强度：用模型默认「{EFFORT_LABEL_CN.get(e['use'], e['use'])}」，不用调 — {e['reason']}")
                if e.get("hint"):
                    print(f"      可选提示：{e['hint']}")
            print()


if __name__ == "__main__":
    main()
