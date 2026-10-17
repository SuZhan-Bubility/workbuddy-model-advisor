#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workbuddy-model-advisor 卡片渲染器（双角度版）
把 recommend.py 的输出渲染成自包含 HTML 推荐卡：
  【做到最好】 + 【性价比最高】 两个角度并列，各自带思考强度建议。

用法:
    python render_card.py --task-type crape --out card.html
    python render_card.py --task-type auto --query "帮我爬这批论文" --out card.html
    python render_card.py --task-type survey            # 打印 HTML 到 stdout
"""
import argparse
import html
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable  # 使用当前运行的 Python 解释器，跨平台、跨版本

THEME = {
    "crape":    {"c1": "#2563eb", "c2": "#3b82f6", "fg": "#1d4ed8", "hf": "#dbeafe", "bg": "#eff6ff"},
    "research": {"c1": "#7c3aed", "c2": "#a78bfa", "fg": "#6d28d9", "hf": "#ede9fe", "bg": "#f5f3ff"},
    "survey":   {"c1": "#0d9488", "c2": "#2dd4bf", "fg": "#0f766e", "hf": "#ccfbf1", "bg": "#f0fdfa"},
}

# 各角度配色
ANGLE_COLOR = {
    "best":  {"bar": "#d97706", "bg": "#fffbeb", "bd": "#fbbf24", "fg": "#92400e", "tag": "拼最好"},
    "value": {"bar": "#16a34a", "bg": "#f0fdf4", "bd": "#4ade80", "fg": "#15803d", "tag": "拼性价比"},
}

EFFORT_LABEL = {"minimal": "极低", "low": "低", "medium": "中", "high": "高"}


def star_html(n):
    return "★" * n + "☆" * (5 - n)


def reason_text(pick, task_type, angle, rank):
    """自动生成理由（agent 层可覆盖）。"""
    name = pick["name"]
    cr = pick["credits"]
    ctx = pick["ctx"] // 1000
    if angle == "best":
        if rank == 1:
            return f"能力最强的一档，{ctx}K 上下文；不在乎成本、要最优产出时选它"
        return f"能力接近主推，{cr}，是「最强」的稳健备选"
    else:
        if rank == 1:
            return f"{cr} 单价 + {ctx}K 上下文，能力÷成本比全场最优，预算敏感时的答案"
        return f"能力更强但更贵（{cr}），只在结果不达标时升级上来"


def fee_html(p, ac):
    """渲染费率行：若存在折扣，显示原价/折扣价 + 省幅 + 评分口径。"""
    base = html.escape(p["credits"])
    ctx = f'上下文 {p["ctx"]//1000}K'
    stars = f'性价比 {star_html(p["stars"])}'
    if p.get("credits_peak") is None:
        return f'{base} · {ctx} · {stars}'
    peak, off = p["credits_peak"], p["credits_offpeak"]
    pct = p["discount_pct"]
    basis = "按折扣价评分" if p.get("scored_on") == "offpeak" else "按原价评分"
    return (
        f'{base} · {ctx} · {stars}<br>'
        f'<span style="display:inline-block;margin-top:2px;padding:1px 6px;background:#fff;border:1px solid #cbd5e1;border-radius:4px;font-size:9.5px;color:#475569;">'
        f'原价 x{peak} → 折扣 x{off}　<b style="color:#16a34a;">省 {pct}%</b>　<span style="color:#94a3b8;">{basis}</span></span>'
    )


def pick_block(p, angle, first, theme):
    ac = ANGLE_COLOR[angle]
    e = p["effort"]
    eff_line = f'建议强度 <b style="color:{ac["fg"]}">{EFFORT_LABEL.get(e["demand"], e["demand"])}</b>'
    eff_detail = html.escape(e["note"])
    if first:
        box = f'background:{ac["bg"]};border:2px solid {ac["bd"]};'
        name_c = ac["fg"]
        badge = f'<span style="background:#fff;color:{ac["fg"]};border:1px solid {ac["bd"]};border-radius:9px;padding:1px 8px;font-size:10px;font-weight:700;">{ac["tag"]}</span>'
    else:
        box = "background:#f8fafc;border:1.3px solid #cbd5e1;"
        name_c = "#0f172a"
        badge = ""
    reason = html.escape(reason_text(p, "", angle, p["rank"]))
    return f'''
      <div style="{box}border-radius:8px;padding:11px 13px;margin-bottom:9px;">
        <div style="display:flex;align-items:center;gap:7px;flex-wrap:wrap;">
          <span style="font-size:10px;color:#94a3b8;font-weight:700;">#{p["rank"]}</span>
          <span style="font-size:13.5px;font-weight:{'700' if first else '600'};color:{name_c};">{html.escape(p["name"])}</span>
          {badge}
        </div>
        <div style="margin-top:5px;font-size:10.5px;color:#64748b;line-height:1.5;">{fee_html(p, ac)}</div>
        <div style="margin-top:6px;padding:5px 8px;background:rgba(255,255,255,.7);border-radius:5px;font-size:10.5px;color:#475569;line-height:1.5;">
          <span style="font-weight:700;color:#0f172a;">思考强度：</span>{eff_line}（模型默认 {EFFORT_LABEL.get(e["model_default"], e["model_default"])}）<br>
          <span style="color:#64748b;">{eff_detail}</span>
        </div>
        <div style="margin-top:6px;font-size:10.5px;color:#475569;line-height:1.5;">{reason}</div>
      </div>'''


def build_html(card, theme):
    t = theme
    best = card["angles"]["best"]
    value = card["angles"]["value"]

    def col(angle_data, angle):
        blocks = "".join(pick_block(p, angle, i == 0, theme) for i, p in enumerate(angle_data["picks"]))
        ac = ANGLE_COLOR[angle]
        return f'''
      <div style="flex:1;min-width:270px;">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:9px;">
          <span style="width:5px;height:16px;border-radius:3px;background:{ac["bar"]};display:inline-block;"></span>
          <span style="font-size:13px;font-weight:700;color:{ac["fg"]};">{html.escape(angle_data["label"])}</span>
        </div>
        {blocks}
      </div>'''

    tc = card.get("time_context", {})
    period = tc.get("period", "")
    is_peak = tc.get("is_peak", False)
    period_color = "#fef3c7" if is_peak else "#dcfce7"
    period_fg = "#92400e" if is_peak else "#166534"
    snap = card.get("fee_snapshot", {}).get("captured_at", "")

    # 暂停提示里要给出"具体动作"：主推哪个模型、强度几档
    _picks = card["angles"]["best"].get("picks") or []
    top_pick = _picks[0] if _picks else {}
    top_name = top_pick.get("name") or ""
    _eff_raw = (top_pick.get("effort") or {}).get("demand") or ""
    top_eff = EFFORT_LABEL.get(_eff_raw, _eff_raw)

    if top_name and top_eff:
        action = (f'建议切到「<b>{html.escape(top_name)}</b>」，思考强度设为'
                  f'「<b>{html.escape(top_eff)}</b>」，然后回我一句「<b>继续</b>」，我接着把原来的活干完。')
    elif top_name:
        action = (f'建议切到「<b>{html.escape(top_name)}</b>」，然后回我一句「<b>继续</b>」，'
                  f'我接着把原来的活干完。')
    else:
        action = '选好之后回我一句「<b>继续</b>」，我接着把原来的活干完。'

    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>模型推荐卡 · {html.escape(card["task_name"])}</title></head>
<body style="margin:0;padding:16px;background:#eef2f7;font-family:system-ui,-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;">
  <div style="max-width:760px;margin:0 auto;background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,.05);">
    <div style="background:linear-gradient(90deg,{t["c1"]},{t["c2"]});padding:15px 22px;">
      <div style="font-size:15.5px;font-weight:700;color:#fff;">模型推荐卡｜{html.escape(card["task_name"])}</div>
      <div style="font-size:11px;color:rgba(255,255,255,.88);margin-top:3px;">任务特征：{html.escape(card["features"])} ｜ 两个角度：拼最好 / 拼性价比</div>
    </div>
    <div style="background:#fffbeb;border-bottom:1px solid #fde68a;padding:11px 22px;">
      <div style="font-size:12.5px;font-weight:700;color:#92400e;">先停一下 —— 任务已暂停，等你定好模型</div>
      <div style="margin-top:5px;font-size:11.5px;color:#78350f;line-height:1.75;">
        卡上写清了选哪个、为什么选它；真正动手切换，在输入框上方的模型下拉框里。
        {action}
        暂时不想换也可以，直接说一句「继续」，我按原样往下做。
      </div>
    </div>
    <div style="padding:11px 18px 0;">
      <div style="display:flex;gap:7px;flex-wrap:wrap;font-size:10.5px;">
        <span style="background:{period_color};color:{period_fg};border-radius:5px;padding:3px 9px;font-weight:700;">
          当前：{html.escape(tc.get("now",""))}　{html.escape(period)}
        </span>
        <span style="background:#f1f5f9;color:#475569;border-radius:5px;padding:3px 9px;">
          费率快照：{html.escape(snap)}
        </span>
      </div>
      <div style="margin-top:5px;font-size:10px;color:#94a3b8;">
        时段规则：{html.escape(tc.get("rule",""))}
      </div>
    </div>
    <div style="padding:13px 18px 4px;display:flex;gap:16px;flex-wrap:wrap;">
      {col(best, "best")}
      {col(value, "value")}
    </div>
    <div style="padding:2px 18px 14px;font-size:10.5px;color:#94a3b8;line-height:1.65;">
      ※ 费率来自本机服务端下发快照（自动更新）；标「原价→折扣」的模型表示历史快照中出现过两档价，折扣一般在非高峰时段生效。<br>
      ※ 拼最好＝成本维度按原价算（不计成本求最优）；拼性价比＝成本维度按折扣价算（错峰后的真实划算度）。<br>
      ※ 思考强度影响实际 token 消耗：想得越久越贵。本机配置不含各档费率倍率，故只给相对建议，不给精确倍数。<br>
      ※ 模型清单与计费实时读取本机配置；此卡只做建议，最终由你在界面下拉框选择（含思考强度）。<br>
      ※ 数据源：{html.escape(str(card.get("data_source", "")))}
    </div>
  </div>
</body></html>'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-type", default="crape", choices=["crape", "research", "survey", "auto"])
    ap.add_argument("--query", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.task_type == "auto":
        cmd = [PY, str(HERE / "recommend.py"), "--task-type", "auto", "--query", args.query, "--json"]
    else:
        cmd = [PY, str(HERE / "recommend.py"), "--task-type", args.task_type, "--json"]

    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        sys.stderr.write(out.stderr)
        sys.exit(1)

    card = json.loads(out.stdout)
    theme = THEME.get(card["task_type"], THEME["crape"])
    h = build_html(card, theme)

    if args.out:
        Path(args.out).write_text(h, encoding="utf-8")
        print(f"已生成: {args.out}")
        print()
        print("=" * 70)
        print("⛔ 卡片已就绪，接下来必须照做（本 skill 是检查点，不是可跳过的步骤）：")
        print("   1) 立刻调用 present_files 打开上面这个 HTML 的预览（存成文件不算交付）")
        print("   2) 用 2-3 句说明「主推模型 + 思考强度 + 备选」，然后【立即结束本轮回复】")
        print("   3) 严禁在同一轮里继续原任务（爬取/下载/生成文档等）——")
        print("      用户必须先到界面手动切模型，否则任务会跑在错的模型上，这张卡就白做了")
        print("   4) 等用户回复「继续」后，再按原任务正常执行，不要重复弹卡")
        print("=" * 70)
    else:
        print(h)


if __name__ == "__main__":
    main()
