"""HTML 交付报告：一页看完链路证据、仿真形貌、G 代码与人工复核清单。

自包含单文件（PNG 用 base64 内嵌，刀路用内联 SVG 叠加），无外部依赖，
可直接发给工艺员复核，也可作为项目演示/面试材料。
"""
from __future__ import annotations

import base64
import hashlib
import html
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .agent import RunResult
from .simulator import heightmap_png
from .models import FeatureKind


CSS = """
:root{--bg:#f5f6f8;--panel:#fff;--ink:#1b2430;--muted:#68758a;--line:#e3e7ee;
--ok:#0f9d6e;--ok-bg:#e8f7f1;--bad:#d64545;--bad-bg:#fdecec;--warn:#c98a12;
--warn-bg:#fff6e2;--blue:#2f6fd0;--blue-bg:#eaf1fd;--code:#111a27;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;}
.wrap{max-width:1420px;margin:0 auto;padding:28px 26px 60px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.3px}
h2{font-size:16px;margin:0 0 14px;padding-bottom:9px;border-bottom:2px solid var(--line);
display:flex;align-items:center;gap:8px}
h2 .n{font-size:11px;background:var(--ink);color:#fff;border-radius:5px;
padding:2px 7px;font-weight:600}
.sub{color:var(--muted);font-size:13px;margin-bottom:22px}
.badge{display:inline-flex;align-items:center;gap:7px;padding:7px 14px;border-radius:8px;
font-weight:700;font-size:13px}
.badge.ok{background:var(--ok-bg);color:var(--ok)} .badge.bad{background:var(--bad-bg);color:var(--bad)}
.dot{width:8px;height:8px;border-radius:50%;background:currentColor}
.grid{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin:18px 0 24px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px 15px}
.kpi .v{font-size:21px;font-weight:700;letter-spacing:-.5px}
.kpi .l{font-size:11px;color:var(--muted);margin-top:2px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:18px 20px;margin-bottom:18px;box-shadow:0 1px 2px rgba(20,30,50,.03)}
.flow{display:flex;align-items:stretch;gap:0;overflow-x:auto;padding:4px 0}
.node{flex:1;min-width:132px;background:#fafbfc;border:1px solid var(--line);
border-radius:9px;padding:11px 12px;position:relative}
.node .t{font-weight:700;font-size:12px} .node .d{font-size:11px;color:var(--muted);margin-top:4px;line-height:1.45}
.node.gate{background:var(--blue-bg);border-color:#bcd2f5}
.node.pass{background:var(--ok-bg);border-color:#b9e6d3}
.node.fail{background:var(--bad-bg);border-color:#f3c4c4}
.arrow{display:flex;align-items:center;padding:0 7px;color:#a9b4c4;font-size:17px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--muted);font-weight:600;padding:7px 8px;border-bottom:1px solid var(--line);
white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid #f0f2f6;vertical-align:top}
tr:last-child td{border-bottom:none}
.mono{font-family:"Cascadia Code",Consolas,monospace;font-size:11.5px}
.tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11px;font-weight:600}
.tag.ok{background:var(--ok-bg);color:var(--ok)} .tag.bad{background:var(--bad-bg);color:var(--bad)}
.tag.warn{background:var(--warn-bg);color:var(--warn)} .tag.blue{background:var(--blue-bg);color:var(--blue)}
ul{margin:6px 0 0;padding-left:19px} li{margin:3px 0}
.note{background:#f8fafc;border-left:3px solid var(--blue);padding:9px 12px;
border-radius:0 6px 6px 0;font-size:12.5px;color:#42506a;margin:10px 0}
.simwrap{position:relative;background:#222732;border-radius:8px;overflow:hidden;line-height:0}
.simwrap img{width:100%;height:auto;display:block;image-rendering:pixelated}
.simwrap svg{position:absolute;inset:0;width:100%;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:11.5px;color:var(--muted);margin-top:10px}
.legend i{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:5px;vertical-align:-1px}
pre{background:var(--code);color:#d6e2f0;border-radius:8px;padding:14px 16px;overflow:auto;
max-height:420px;font:11.5px/1.55 "Cascadia Code",Consolas,monospace;margin:0}
pre .badline{color:#ff8e8e;display:block;background:rgba(214,69,69,.13)}
details summary{cursor:pointer;font-weight:600;padding:6px 0;color:var(--blue)}
.timeline{border-left:2px solid var(--line);margin-left:6px;padding-left:18px}
.tl{position:relative;margin-bottom:14px} .tl:before{content:"";position:absolute;left:-24px;top:5px;
width:10px;height:10px;border-radius:50%;background:var(--ok);border:2px solid #fff;
box-shadow:0 0 0 1px var(--line)}
.tl.fail:before{background:var(--bad)} .tl.repaired:before{background:var(--warn)}
.tl .h{font-weight:600;font-size:13px} .tl .m{font-size:11.5px;color:var(--muted)}
.foot{color:var(--muted);font-size:11.5px;border-top:1px solid var(--line);padding-top:16px;margin-top:8px}
.sign{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:12px}
.sign div{border-bottom:1px solid #b9c2d0;padding-bottom:5px;font-size:12px;color:var(--muted)}
@media (max-width:1050px){.cols{grid-template-columns:1fr}.grid{grid-template-columns:repeat(3,1fr)}}
@media (prefers-color-scheme:dark){
:root{--bg:#151a22;--panel:#1d2430;--ink:#e6ecf5;--muted:#93a1b7;--line:#2e3949;
--ok-bg:#123a2f;--bad-bg:#3d2024;--warn-bg:#3a3018;--blue-bg:#1a2c48;--code:#0c111a}
.node{background:#202836}.note{background:#202836;color:#b9c8dd}.kpi{background:var(--panel)}
.sign div{border-color:#4a5668}}
"""


def _esc(s: Any) -> str:
    return html.escape(str(s))


def _flow_svg(result: RunResult) -> str:
    """五阶段链路图，双关卡用蓝色高亮，失败节点标红。"""
    val_ok = bool(result.validation and result.validation.ok)
    sim_ok = bool(result.simulation and result.simulation.ok)
    nodes = [
        ("1 输入层", f"{result.part.source}<br>{result.part.material.name}<br>"
         f"{result.part.stock_x:g}x{result.part.stock_y:g}mm", "node"),
        ("2 工艺规划", f"{result.plan.planner} 规划器<br>"
         f"{len(result.plan.operations)} 道工序<br>参数夹紧", "node"),
        ("3 生成 G 代码", f"{result.controller} 后置<br>"
         f"{result.program.block_count if result.program else 0} 程序段", "node"),
        ("◆ 静态校验", "语法 / 方言 / 行程<br>模态状态机" if val_ok else "拦截错误指令",
         "node gate " + ("pass" if val_ok else "fail")),
        ("◆ 体素仿真", "撞刀 / 过切 / 欠切<br>体积守恒" if sim_ok else "检出形貌异常",
         "node gate " + ("pass" if sim_ok else "fail")),
        ("4-5 下发准入", "双关卡 + 哈希清单<br>人工复核签字" if result.approved
         else "拒绝下发", "node " + ("pass" if result.approved else "fail")),
    ]
    out = ['<div class="flow">']
    for i, (t, d, cls) in enumerate(nodes):
        out.append(f'<div class="{cls}"><div class="t">{t}</div><div class="d">{d}</div></div>')
        if i < len(nodes) - 1:
            out.append('<div class="arrow">&#8594;</div>')
    out.append("</div>")
    return "".join(out)


def _sim_overlay(result: RunResult) -> str:
    """在仿真底图上叠加刀路 polyline、毛坯/特征轮廓与碰撞点。"""
    sim = result.simulation
    if not sim or sim.height is None:
        return "<div class='note'>未生成仿真数据</div>"
    H = sim.height
    ny, nx = H.shape
    x0, x1, y0, y1 = sim.extent
    res = sim.stats["resolution"]
    up = 2  # heightmap_png 默认放大 2 倍
    W, Hh = nx * up, ny * up

    def px(x: float) -> float:
        return (x - x0) / (x1 - x0) * W

    def py(y: float) -> float:
        # 图像坐标 Y 向下，工件坐标 Y 向上
        return (y1 - y) / (y1 - y0) * Hh

    parts = [f'<svg viewBox="0 0 {W} {Hh}" preserveAspectRatio="none" '
             f'xmlns="http://www.w3.org/2000/svg">']
    p = result.part
    parts.append(
        f'<rect x="{px(-p.stock_x/2):.1f}" y="{py(p.stock_y/2):.1f}" '
        f'width="{p.stock_x/res*up:.1f}" height="{p.stock_y/res*up:.1f}" '
        f'fill="none" stroke="#ffffff" stroke-width="2" stroke-dasharray="8 5" opacity=".85"/>')
    for f in p.features:
        if f.kind in (FeatureKind.CIRCLE_POCKET, FeatureKind.HOLE):
            parts.append(
                f'<circle cx="{px(f.cx):.1f}" cy="{py(f.cy):.1f}" '
                f'r="{f.diameter/2/res*up:.1f}" fill="none" stroke="#9fe8ff" '
                f'stroke-width="1.6" stroke-dasharray="5 4" opacity=".9"/>')
        elif f.kind in (FeatureKind.RECT_POCKET, FeatureKind.SLOT):
            w = f.width if f.kind == FeatureKind.RECT_POCKET else f.length
            h = f.height if f.kind == FeatureKind.RECT_POCKET else f.width
            parts.append(
                f'<rect x="{px(f.cx-w/2):.1f}" y="{py(f.cy+h/2):.1f}" '
                f'width="{w/res*up:.1f}" height="{h/res*up:.1f}" fill="none" '
                f'stroke="#9fe8ff" stroke-width="1.6" stroke-dasharray="5 4" opacity=".9"/>')

    palette = ["#ffd166", "#7ae7c7", "#ff9f68", "#b8a2ff", "#6ec6ff", "#f7a8d8"]
    for idx, path in enumerate(sim.trajectories):
        if len(path) < 2:
            continue
        step = max(1, len(path) // 500)
        pts = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y, _ in path[::step])
        parts.append(f'<polyline points="{pts}" fill="none" '
                     f'stroke="{palette[idx % len(palette)]}" stroke-width="1.1" '
                     f'opacity=".82" stroke-linejoin="round"/>')
    for x, y in sim.collision_points[:120]:
        cx, cy = px(x), py(y)
        parts.append(f'<path d="M{cx-4:.1f},{cy-4:.1f} l8,8 M{cx+4:.1f},{cy-4:.1f} l-8,8" '
                     f'stroke="#ff3b3b" stroke-width="2.2"/>')
    parts.append("</svg>")
    return (
        '<div class="simwrap"><img alt="材料去除仿真高度图" src="data:image/png;base64,'
        + base64.b64encode(heightmap_png(sim, result.part)).decode("ascii")
        + '">' + "".join(parts) + "</div>"
    )


def _ops_table(result: RunResult) -> str:
    rows = []
    for op in result.plan.operations:
        f = result.plan.part.features[op.feature_index]
        adj = "<br>".join(f'<span class="tag warn">夹紧</span> {_esc(a)}'
                          for a in op.adjustments)
        rows.append(
            f"<tr><td class='mono'>{_esc(op.id)}</td>"
            f"<td>F{op.feature_index + 1} {_esc(f.kind.value)}<br>"
            f"<span class='tag blue'>{_esc(op.stage)}</span></td>"
            f"<td>T{op.tool_number:02d}</td><td class='mono'>S{op.spindle}<br>F{op.feed}</td>"
            f"<td class='mono'>ap {op.ap:g}<br>ae {op.ae:g}</td>"
            f"<td>{_esc(op.strategy)}<br>{_esc(op.note)}</td><td>{adj or '-'}</td></tr>")
    return ("<table><thead><tr><th>工序</th><th>特征/阶段</th><th>刀具</th>"
            "<th>S / F</th><th>ap / ae</th><th>策略</th><th>安全夹紧记录</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def _gcode_html(result: RunResult) -> str:
    if not result.program:
        return "<div class='note'>未生成程序</div>"
    bad_lines = set()
    if result.validation:
        bad_lines = {e.line_no for e in result.validation.errors}
    lines = []
    for i, line in enumerate(result.program.lines, 1):
        text = _esc(line)
        if i in bad_lines:
            lines.append(f'<span class="badline">{i:4d} | {text}</span>')
        else:
            lines.append(f"{i:4d} | {text}")
    return "<pre>" + "\n".join(lines) + "</pre>"


def render_report(result: RunResult, settings: Settings | None = None) -> str:
    val = result.validation
    sim = result.simulation
    st = sim.stats if sim else {}
    vstats = val.stats if val else {}
    prog_hash = (hashlib.sha256(result.program.text.encode()).hexdigest()[:24]
                 if result.program else "-")
    status_cls = "ok" if result.approved else "bad"
    status_txt = ("双关卡通过 · 允许进入人工复核下发" if result.approved
                  else "安全拦截 · 拒绝下发机床")

    kpis = [
        (result.controller, "目标数控系统"),
        (result.program.block_count if result.program else 0, "G 代码程序段"),
        (len(result.plan.operations), "工艺工序"),
        (f"{result.attempts}", "自修复轮次"),
        (f"{st.get('volume_error_pct', 0)}%", "去除体积误差"),
        (f"{st.get('collision_points', 0)}", "检出碰撞点"),
    ]
    kpi_html = "".join(f'<div class="kpi"><div class="v">{_esc(v)}</div>'
                       f'<div class="l">{_esc(l)}</div></div>' for v, l in kpis)

    # 校验/仿真问题清单
    issues_html = "<li><span class='tag ok'>无</span> 未检出问题</li>"
    all_issues: list[str] = []
    if val:
        all_issues += [f"<b>校验</b> {_esc(e)}" for e in val.errors]
    if sim:
        all_issues += [f"<b>仿真</b> {_esc(i['message'])}" for i in sim.errors]
    if all_issues:
        issues_html = "".join(f"<li>{i}</li>" for i in all_issues)
    warnings = []
    if val:
        warnings += [str(w) for w in val.warnings]
    if result.part.warnings:
        warnings += result.part.warnings

    timeline = []
    for s in result.stages:
        cls = "pass" if s.status == "pass" else ("fail" if s.status in ("fail", "blocked")
                                                 else "repaired")
        issue_txt = ("<ul>" + "".join(f"<li>{_esc(i)}</li>" for i in s.issues[:8]) + "</ul>"
                     if s.issues else "")
        timeline.append(
            f'<div class="tl {cls}"><div class="h">{_esc(s.name)} '
            f'<span class="tag {"ok" if s.status=="pass" else ("bad" if s.status in ("fail","blocked") else "warn")}">'
            f'{_esc(s.status)}</span> <span style="color:var(--muted);font-weight:400">'
            f'{s.elapsed_ms}ms</span></div><div class="m">{_esc(s.detail)}</div>'
            f'{issue_txt}</div>')

    repair_html = ""
    if result.repair_log:
        blocks = []
        for r in result.repair_log:
            blocks.append(
                f"<tr><td>第{r['round']}轮</td><td>{_esc(r['gate'])}</td><td>" +
                "<br>".join(_esc(i) for i in r["issues"][:4]) + "</td></tr>")
        repair_html = (
            '<div class="panel"><h2><span class="n">E</span>幻觉拦截与自修复证据链</h2>'
            '<div class="note">被污染的 G 代码段在第 1 轮被静态校验/仿真拦截；'
            '修复不是"再问一次模型碰运气"，而是把机器检出的客观证据映射成具体工艺参数调整，'
            '并由确定性后处理器重新生成 —— 幻觉指令不会在新程序中重现。</div>'
            "<table><thead><tr><th>轮次</th><th>拦截关卡</th><th>检出问题 → 修复依据</th>"
            "</tr></thead><tbody>" + "".join(blocks) + "</tbody></table></div>")
    else:
        repair_html = (
            '<div class="panel"><h2><span class="n">E</span>安全屏障状态</h2>'
            '<div class="note">本次运行未触发自修复：参数夹紧层、静态校验器、'
            '体素仿真三道关卡首轮全部通过。可用 <span class="mono">--demo-hallucination</span> '
            '主动注入幻觉参数与错误 G 代码，验证拦截与修复闭环。</div></div>')

    main_html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CNC Agent 加工安全报告</title><style>{CSS}</style></head><body><div class="wrap">
<h1>CNC Agent · 加工安全与链路报告</h1>
<div class="sub">生成时间 {datetime.now():%Y-%m-%d %H:%M:%S} ｜ 零件：{_esc(result.part.name)} ｜
输入来源：{_esc(result.part.source)} ｜ 规划器：{_esc(result.plan.planner)}</div>
<div class="badge {status_cls}"><span class="dot"></span>{status_txt}</div>
<div class="grid">{kpi_html}</div>

<div class="panel"><h2><span class="n">A</span>五阶段链路与双安全关卡</h2>{_flow_svg(result)}
<div class="note"><b>架构优化：</b>大模型只输出结构化工艺方案 JSON，<b>不写 G 代码</b>；
G 代码由确定性后处理器生成。幻觉只能污染参数，而参数会被物理夹紧层拦截；
语法、行程、模态、圆弧插补等机床级细节完全不经过模型。校验 + 仿真是第二、第三道防线。</div></div>

<div class="cols">
<div>
  <div class="panel"><h2><span class="n">B</span>输入解析与工艺方案</h2>
  <table><tbody>
  <tr><th>材料</th><td>{_esc(result.part.material.name)} (Vc {result.part.material.vc:g} m/min,
      fz {result.part.material.fz:g} mm/z, kc {result.part.material.kc:g} N/mm²)</td></tr>
  <tr><th>毛坯</th><td>{result.part.stock_x:g} × {result.part.stock_y:g} × {result.part.stock_z:g} mm</td></tr>
  <tr><th>特征</th><td>{len(result.part.features)} 个：
      {_esc("、".join(f.label(i+1) for i, f in enumerate(result.part.features)))}</td></tr>
  <tr><th>坐标系</th><td>{_esc(result.plan.work_offset)}，安全高度 Z{result.plan.safe_z:g}，
      下刀方式 {_esc(result.plan.approach)}，切削液 {_esc(result.plan.coolant)}</td></tr>
  </tbody></table>
  <div class="note"><b>规划思路：</b>{_esc(result.plan.reasoning[:700])}</div>
  </div>
  <div class="panel"><h2><span class="n">C</span>工序卡（含安全夹紧记录）</h2>{_ops_table(result)}</div>
  <div class="panel"><h2><span class="n">D</span>静态校验结果</h2>
  <table><tbody>
  <tr><th>判定</th><td><span class="tag {'ok' if val and val.ok else 'bad'}">
      {'PASS' if val and val.ok else 'FAIL'}</span> 错误 {len(val.errors) if val else '-'} 项，
      警告 {len(val.warnings) if val else '-'} 项</td></tr>
  <tr><th>模态复原</th><td>程序段 {vstats.get('blocks', '-')} ｜
      运动段 {vstats.get('motion_blocks', '-')} ｜ 用刀 {vstats.get('tools_used', '-')}</td></tr>
  <tr><th>坐标范围</th><td class="mono">X {vstats.get('coord_min',{}).get('X','-')} ~
      {vstats.get('coord_max',{}).get('X','-')} ｜
      Y {vstats.get('coord_min',{}).get('Y','-')} ~ {vstats.get('coord_max',{}).get('Y','-')} ｜
      Z {vstats.get('coord_min',{}).get('Z','-')} ~ {vstats.get('coord_max',{}).get('Z','-')}</td></tr>
  <tr><th>S / F</th><td class="mono">{vstats.get('spindle_range','-')} /
      {vstats.get('feed_range','-')}</td></tr>
  </tbody></table><ul>{issues_html}</ul>
  {'<div class="note">提示：' + _esc("；".join(warnings[:4])) + '</div>' if warnings else ''}
  </div>
</div>
<div>
  <div class="panel"><h2><span class="n">F</span>材料去除仿真 · 俯视形貌</h2>
  {_sim_overlay(result)}
  <div class="legend">
    <span><i style="background:#e0e2de"></i>未加工面</span>
    <span><i style="background:#26609c"></i>已加工到位</span>
    <span><i style="background:#d63a3a"></i>过切</span>
    <span><i style="background:#e8b22e"></i>欠切残留</span>
    <span><i style="background:#a64cc4"></i>区域外误切</span>
    <span><i style="background:#ffd166"></i>刀具轨迹</span>
    <span><i style="background:#9fe8ff"></i>特征轮廓</span>
  </div>
  <div class="note">仿真输入是<b>校验后的 G 代码文本</b>（经模态状态机复原轨迹），
  不是内部理想刀路对象 —— 仿真跑的程序与下发机床的程序是同一个文件。</div>
  </div>
  <div class="panel"><h2><span class="n">G</span>仿真量化指标</h2>
  <table><tbody>
  <tr><th>网格 / 轨迹</th><td>{st.get('grid','-')} ｜ {st.get('moves','-')} 段 ｜
      {st.get('cutting_paths','-')} 条刀路 ｜ 固定循环 {st.get('fixed_cycles','-')}</td></tr>
  <tr><th>去除体积</th><td>实际 {st.get('removed_volume_mm3','-')} mm³ /
      理论 {st.get('expected_volume_mm3','-')} mm³，误差 {st.get('volume_error_pct','-')}%</td></tr>
  <tr><th>形貌缺陷</th><td>过切 {st.get('overcut_cells','-')} 格 ｜
      误切 {st.get('stray_cut_cells','-')} 格 ｜ 欠切 {st.get('undercut_ratio','-')}%</td></tr>
  <tr><th>碰撞</th><td>快移入料 {st.get('rapid_in_material','-')} ｜
      快速扎刀 {st.get('rapid_plunge','-')} ｜ 超程 {st.get('overtravel_segments','-')} ｜
      夹具干涉 {st.get('clamp_interference','-')}</td></tr>
  <tr><th>最大切深</th><td>Z {st.get('max_cut_depth','-')} mm（毛坯底面 {st.get('floor','-')} mm）</td></tr>
  </tbody></table></div>
  <div class="panel"><h2><span class="n">H</span>阶段日志</h2>
  <div class="timeline">{''.join(timeline)}</div></div>
</div>
</div>

{repair_html}

<div class="panel"><h2><span class="n">I</span>下发前人工复核清单</h2>
<table><tbody>
<tr><th>程序指纹</th><td class="mono">SHA-256 (前24位) {prog_hash}</td></tr>
<tr><th>目标系统</th><td>{_esc(result.controller)} 后置处理版本（方言已校验）</td></tr>
<tr><th>准入状态</th><td><span class="tag {'ok' if result.approved else 'bad'}">
    {'双关卡通过' if result.approved else '拒绝下发'}</span>
    {_esc(result.blocked_reason)}</td></tr>
<tr><th>复核项</th><td>① 装夹与压板位置避开刀路 ② 刀库中刀具实际几何与程序一致
    ③ G54 对刀与 Z0 设定 ④ 首件单段/空运行验证 ⑤ 进给倍率置低试切</td></tr>
</tbody></table>
<div class="sign"><div>工艺员签字 / 日期</div><div>操作员签字 / 日期</div>
<div>首件检验记录号</div></div>
<div class="note"><b>安全声明：</b>AI 输出的程序不得未经复核直接上机。
校验 + 仿真是抵消大模型幻觉的硬性屏障；本项目范围限定为三轴 2.5D 铣削，
复杂曲面、五轴联动、刀杆干涉与切屑载荷仍需专业 CAM 与人工工艺评审。</div>
</div>

<div class="panel"><h2><span class="n">J</span>最终 G 代码（{_esc(result.controller)} 后置）</h2>
<details><summary>展开 / 收起程序全文（{result.program.block_count if result.program else 0} 段）</summary>
<div style="height:10px"></div>{_gcode_html(result)}</details></div>

<div class="foot">CNC Agent · 确定性后处理 + 静态校验 + 体素仿真 ｜
本报告由流水线自动生成，仅作为工艺复核辅助材料，不替代机床操作规范。</div>
</div></body></html>"""
    return main_html


def save_report(result: RunResult, settings: Settings | None,
                output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "report.html"
    path.write_text(render_report(result, settings), encoding="utf-8")
    return path
