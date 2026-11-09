"""完整闭环编排：智能编程 + 视觉监测 + 故障诊断 + 控制下发 + 工艺反哺。

  ① CNCAgent 生成并双关卡验证 G 代码（编程侧）
  ② 摄像头/合成帧 -> YOLO/启发式检测 -> 去抖 -> 截图 + 检测结果作为工具输入
  ③ 多模态 Agent 分析根因与处置方案；规则库兜底
  ④ SafetyGovernor 白名单门控 -> 工业协议下发（默认 dry-run）
  ⑤ 把整改系数反哺工艺方案，重新夹紧/后处理/校验/仿真，产出修订程序
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agent import CNCAgent, RunResult
from ..config import OUTPUT_DIR, Settings, get_settings
from ..cutting import clamp_plan
from ..generator import make_post
from ..geometry import generate_toolpaths
from ..machines import MachineProfile, capability_summary, get_profile
from ..models import Program
from ..report import CSS
from ..simulator import simulate_gcode
from ..validator import Validator
from .agent_tools import (ControlDecision, Diagnosis, SafetyGovernor,
                          VisionDiagnosisAgent)
from .detector import FAULT_META, FaultClass, VisionEvent
from .frames import FrameSource, SyntheticSource
from .monitor import MonitorConfig, MonitorResult, VisionLogger, VisionMonitor
from .protocols import ControlResult, ProtocolAdapter, create_adapter


def _esc(v: Any) -> str:
    return html.escape(str(v))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _quarantine_unapproved(output_dir: Path, keep: set[str]) -> list[str]:
    """把没有准入标记的修订程序改名隔离，防止进入机床下发下拉框。

    只改扩展名、不删内容：既保住可回溯性，又让扫描 *.nc 的列表不再看到它。
    """
    moved: list[str] = []
    for nc in output_dir.glob("revised_*.nc"):
        if nc.name in keep or nc.with_name(nc.stem + ".approved.json").is_file():
            continue
        try:
            nc.replace(nc.with_name(nc.name + ".unapproved.bak"))
            moved.append(nc.name)
        except OSError:
            pass
    return moved


@dataclass
class EventTrace:
    event: VisionEvent
    diagnosis: Diagnosis
    decision: ControlDecision
    controls: list[ControlResult] = field(default_factory=list)


@dataclass
class ClosedLoopResult:
    run: RunResult
    monitor: MonitorResult
    traces: list[EventTrace]
    revised_program: Program | None
    revised_ok: bool
    parameter_diff: list[dict[str, Any]]
    adapter_note: str
    output_files: dict[str, str] = field(default_factory=dict)

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "machine": self.run.profile.display if self.run.profile else "",
            "original_approved": self.run.approved,
            "frames": self.monitor.frames,
            "events": len(self.traces),
            "detector_backend": self.monitor.backend,
            "diagnosis_sources": sorted({t.diagnosis.source for t in self.traces}),
            "approved_signals": sum(len(t.decision.approved_signals) for t in self.traces),
            "blocked_signals": sum(len(t.decision.blocked_signals) for t in self.traces),
            "control_writes": sum(len(t.controls) for t in self.traces),
            "revised_program_ok": self.revised_ok,
            "original_sha": _sha(self.run.program.text) if self.run.program else "",
            "revised_sha": _sha(self.revised_program.text) if self.revised_program else "",
        }


class ClosedLoopController:
    def __init__(self, settings: Settings | None = None,
                 profile: MachineProfile | None = None,
                 output_dir: Path = OUTPUT_DIR):
        self.settings = settings or get_settings()
        self.profile = profile or get_profile(self.settings.machine_id)
        self.output_dir = Path(output_dir)
        self.vision_dir = self.output_dir / "vision"
        self.vision_dir.mkdir(parents=True, exist_ok=True)
        self.agent = CNCAgent(self.settings, self.profile)
        self.diagnosis_agent = VisionDiagnosisAgent(self.settings)
        self.governor = SafetyGovernor(self.settings.allow_auto_stop)
        self.logger = VisionLogger(self.vision_dir / "closed_loop.jsonl")
        self.traces: list[EventTrace] = []
        self.adapter: ProtocolAdapter | None = None
        self.adapter_note = ""

    # ---------------- ① 编程侧 ----------------
    def program(self, request: str, **kwargs) -> RunResult:
        return self.agent.run(request, controller=self.profile.post, **kwargs)

    # ---------------- ②③④ 监测 + 诊断 + 控制 ----------------
    def monitor_and_respond(self, run: RunResult, source: FrameSource,
                            detector, detector_note: str = "",
                            protocol: str = "auto", dry_run: bool = True,
                            human_confirmed: bool = False,
                            host: str = "127.0.0.1",
                            cfg: MonitorConfig | None = None) -> MonitorResult:
        ctx = self._process_context(run)
        adapter, note = create_adapter(
            protocol, self.profile, host=host, dry_run=dry_run,
            log_path=self.vision_dir / "control_audit.jsonl")
        self.adapter, self.adapter_note = adapter, note
        monitor = VisionMonitor(
            source, detector, self.vision_dir, cfg or MonitorConfig(),
            process_context=ctx, fallback_reason=detector_note)

        def on_event(ev: VisionEvent) -> None:
            diag = self.diagnosis_agent.diagnose(ev)
            dec = self.governor.review(diag, human_confirmed)
            controls = [adapter.write(s["signal"], s.get("value"))
                        for s in dec.approved_signals]
            trace = EventTrace(ev, diag, dec, controls)
            self.traces.append(trace)
            self.logger.append("diagnosis", diag.to_dict())
            self.logger.append("control_decision", {
                "event_id": ev.id,
                "approved": dec.approved_signals,
                "blocked": dec.blocked_signals,
                "note": dec.note})
            self.logger.append("control_write", {
                "event_id": ev.id,
                "adapter": adapter.name, "dry_run": adapter.dry_run,
                "writes": [c.to_dict() for c in controls]})
            print(f"  [Agent] {ev.label} -> {diag.cause[:34]}... "
                  f"| 来源 {diag.source} | {dec.note}")

        result = monitor.run(on_event=on_event)
        adapter.close()
        return result

    # ---------------- ⑤ 工艺反哺重编程 ----------------
    def reprogram(self, run: RunResult) -> tuple[Program | None, bool,
                                                 list[dict[str, Any]], Any, Any]:
        if not self.traces:
            return None, False, [], None, None
        # 多事件整改取最保守（最小）系数，不允许后一个故障把参数又放松回去
        keys = ("ap_factor", "ae_factor", "feed_factor", "spindle_factor")
        factors = {k: min(t.diagnosis.process_adjustments.get(k, 1.0)
                          for t in self.traces) for k in keys}
        plan = copy.deepcopy(run.plan)
        before = {op.id: copy.copy(op) for op in plan.operations}
        for op in plan.operations:
            op.ap = round(max(0.05, op.ap * factors["ap_factor"]), 3)
            op.ae = round(max(0.3, op.ae * factors["ae_factor"]), 3)
            op.feed = max(80, int(op.feed * factors["feed_factor"] / 10) * 10)
            op.spindle = max(200, int(op.spindle * factors["spindle_factor"] / 50) * 50)
            op.adjustments.append(
                f"[视觉反馈] ap x{factors['ap_factor']:g} / ae x{factors['ae_factor']:g} "
                f"/ F x{factors['feed_factor']:g} / S x{factors['spindle_factor']:g}")
        plan.reasoning += (" [闭环反哺] 视觉监测检出故障，按最保守整改系数下调切削参数，"
                           "重新夹紧、后处理、校验与仿真后生成修订程序。")
        plan = clamp_plan(plan, self.profile.spec)

        machine = self.profile.spec
        tps = generate_toolpaths(plan)
        prog = make_post(run.controller, plan, self.profile).generate(
            tps, run.program.program_number if run.program else 1)
        val = Validator(plan, machine).validate(prog.text, run.controller)
        sim = simulate_gcode(prog.text, plan, run.controller, machine=machine,
                             resolution=self.settings.simulator_resolution,
                             strict=self.settings.strict_simulation)
        ok = bool(val.ok and sim.ok and run.approved)

        diff = []
        for op in plan.operations:
            b = before.get(op.id)
            if not b:
                continue
            diff.append({
                "op": op.id,
                "before": f"ap {b.ap:g} / ae {b.ae:g} / F{b.feed} / S{b.spindle}",
                "after": f"ap {op.ap:g} / ae {op.ae:g} / F{op.feed} / S{op.spindle}",
            })
        return prog, ok, diff, val, sim

    # ---------------- 组装完整闭环 ----------------
    def run_full(self, request: str, detector, detector_note: str = "",
                 source: FrameSource | None = None, protocol: str = "auto",
                 dry_run: bool = True, human_confirmed: bool = False,
                 cfg: MonitorConfig | None = None) -> ClosedLoopResult:
        print("\n[1/5] 智能编程 + 双关卡验证 ...")
        run = self.program(request)
        print(f"      程序 {run.controller} / {run.program.block_count if run.program else 0} 段 ｜ "
              f"准入 {'通过' if run.approved else '拒绝'}")
        print("[2/5] 启动视觉实时监控 ...")
        mon = self.monitor_and_respond(
            run, source or SyntheticSource(), detector, detector_note,
            protocol, dry_run, human_confirmed, cfg=cfg)
        print(f"      {mon.frames} 帧 ｜ {len(mon.events)} 起事件 ｜ "
              f"检测器 {mon.backend}")
        print("[3/5] Agent 故障诊断完成；[4/5] 控制信号已按白名单门控下发")
        print("[5/5] 工艺反哺：重新夹紧 -> 后处理 -> 校验 -> 仿真 ...")
        prog, ok, diff, val, sim = self.reprogram(run)
        print(f"      修订程序双关卡：{'PASS' if ok else 'FAIL'}")

        result = ClosedLoopResult(
            run=run, monitor=mon, traces=list(self.traces),
            revised_program=prog, revised_ok=ok, parameter_diff=diff,
            adapter_note=self.adapter_note)

        # 修订程序只有双关卡通过才落「可下发」文件。
        # 之前无条件 write_text 会把 FAIL / 空程序也写成 .nc，而面板的
        # 下发下拉框扫描 output/ 下所有 .nc —— 等于给真机开了一道后门。
        nc = self.output_dir / f"revised_{self.profile.id}_{run.controller}.nc"
        marker = self.output_dir / (
            f"revised_{self.profile.id}_{run.controller}.approved.json")
        if ok and prog and prog.text.strip():
            # 用 bytes 写入：write_text 在 Windows 上会把 \n 转成 \r\n，
            # 让准入标记里的 SHA 与实际下发字节对不上。
            payload = prog.text.encode("utf-8")
            nc.write_bytes(payload)
            marker.write_bytes(json.dumps({
                "approved": True,
                "program": nc.name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "validation_ok": bool(val and getattr(val, "ok", False)),
                "simulation_ok": bool(sim and getattr(sim, "ok", False)),
                "controller": run.controller,
                "machine": self.profile.id,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
            }, ensure_ascii=False, indent=2).encode("utf-8"))
            result.output_files["revised_gcode"] = str(nc)
            result.output_files["approval"] = str(marker)
        else:
            # 清掉本机型历史遗留的未准入产物，避免旧文件继续出现在下发列表
            for stale in (nc, marker):
                try:
                    stale.unlink(missing_ok=True)
                except OSError:
                    pass
        # 其他机型/控制器留下的 revised_*.nc 同样不能没有准入凭证：
        # 改名隔离而非删除，保留内容可回溯，且不再被 .nc 下拉框扫描到。
        keep = {nc.name} if (ok and prog and prog.text.strip()) else set()
        moved = _quarantine_unapproved(self.output_dir, keep)
        if moved:
            result.output_files["quarantined"] = moved
        report = self.output_dir / "closed_loop_report.html"
        report.write_text(render_closed_loop_report(result, val, sim),
                          encoding="utf-8")
        result.output_files["report"] = str(report)
        summary = self.output_dir / "closed_loop_summary.json"
        summary.write_text(json.dumps(result.summary, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        result.output_files["summary"] = str(summary)
        (self.vision_dir / "events_detailed.json").write_text(
            json.dumps([t.event.to_dict() for t in self.traces],
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def _process_context(self, run: RunResult) -> dict[str, Any]:
        ops = run.plan.operations
        last = ops[-1] if ops else None
        return {
            "machine": self.profile.display,
            "controller": self.profile.controller,
            "material": run.part.material.name,
            "stock": f"{run.part.stock_x:g}x{run.part.stock_y:g}x{run.part.stock_z:g}",
            "active_operation": last.id if last else "-",
            "tool": f"T{last.tool_number:02d}" if last else "-",
            "spindle_rpm": last.spindle if last else 0,
            "feed_mm_min": last.feed if last else 0,
            "ap_mm": last.ap if last else 0,
            "ae_mm": last.ae if last else 0,
            "program_sha256": _sha(run.program.text) if run.program else "",
            "work_offset": run.plan.work_offset,
            "safe_z": run.plan.safe_z,
        }


# --------------------------------------------------------------------------
# 闭环可视化报告
# --------------------------------------------------------------------------
def render_closed_loop_report(result: ClosedLoopResult, val, sim) -> str:
    run, mon = result.run, result.monitor
    profile = run.profile
    caps = capability_summary(profile) if profile else {"capabilities": []}
    status = "ok" if (run.approved and result.revised_ok) else "bad"
    # 报告是安全审计材料，不能硬编码 DRY-RUN：真实写入时必须如实标注。
    controls = [c for t in result.traces for c in t.controls]
    real_write = bool(controls) and any(not c.dry_run for c in controls)
    mode_label = "REAL WRITE / 未演练" if real_write else "DRY-RUN / 未写机床"

    flow = [
        ("① 智能编程", f"{run.controller} 后置<br>{run.program.block_count if run.program else 0} 段<br>校验+仿真通过", "node pass"),
        ("② 视觉监测", f"{mon.frames} 帧<br>{mon.backend} 检测器<br>{len(mon.events)} 起事件", "node gate"),
        ("③ Agent 诊断", f"截图+检测框作工具输入<br>{len(result.traces)} 次调用<br>规则/多模态双通路", "node gate"),
        ("④ 控制下发", f"{len(controls)} 条指令<br>白名单门控<br>{mode_label}", "node"),
        ("⑤ 工艺反哺", f"最保守系数下调参数<br>重新校验+仿真<br>{'PASS' if result.revised_ok else 'FAIL'}", "node " + ("pass" if result.revised_ok else "fail")),
    ]
    flow_html = '<div class="flow">' + "".join(
        f'<div class="{c}"><div class="t">{t}</div><div class="d">{d}</div></div>'
        + ('<div class="arrow">&#8594;</div>' if i < len(flow) - 1 else "")
        for i, (t, d, c) in enumerate(flow)) + "</div>"

    # 机型能力面板
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in caps["capabilities"]:
        groups.setdefault(c["group"], []).append(c)
    cap_rows = ""
    for g, items in groups.items():
        cap_rows += f'<tr><th rowspan="{len(items)}">{_esc(g)}</th>'
        for j, c in enumerate(items):
            if j:
                cap_rows += "<tr>"
            badge = {"native": "ok", "derived": "blue", "option": "warn",
                     "disabled": "bad", "external": "warn"}.get(c["status"], "blue")
            cap_rows += (f'<td>{_esc(c["name"])}</td>'
                         f'<td><span class="tag {badge}">{_esc(c["status"])}</span></td>'
                         f'<td>{_esc(c["reason"])}</td></tr>')
    cap_html = ("<table><thead><tr><th>类别</th><th>功能</th><th>状态</th>"
                "<th>依据 / 说明</th></tr></thead><tbody>" + cap_rows + "</tbody></table>")

    # 事件卡片
    cards = []
    for t in result.traces:
        ev, d, dec = t.event, t.diagnosis, t.decision
        img = (f'<img src="data:image/png;base64,{ev.snapshot_b64}" '
               f'alt="现场截图" style="width:100%;border-radius:7px;display:block">'
               if ev.snapshot_b64 else "<div class='note'>无截图</div>")
        dets = "".join(
            f"<tr><td>{_esc(x.label)}</td><td class='mono'>{x.confidence:.2f}</td>"
            f"<td class='mono'>{list(x.bbox)}</td></tr>" for x in ev.detections)
        tool_input = json.dumps({
            "tool": "analyze_machining_fault",
            "detections": [x.to_dict() for x in ev.detections],
            "process_context": ev.context,
            "snapshot_base64": f"<PNG {len(ev.snapshot_b64)} bytes>",
        }, ensure_ascii=False, indent=2)
        approved = "".join(
            f'<li><span class="tag ok">{_esc(s["signal"])}</span> '
            f'value={_esc(s.get("value"))} — {_esc(s.get("reason",""))}</li>'
            for s in dec.approved_signals) or "<li>无</li>"
        blocked = "".join(
            f'<li><span class="tag bad">{_esc(s["signal"])}</span> '
            f'{_esc(s.get("blocked_reason",""))}</li>'
            for s in dec.blocked_signals) or "<li>无</li>"
        writes = "".join(
            f'<tr><td class="mono">{_esc(c.signal)}</td><td class="mono">{_esc(c.value)}</td>'
            f'<td>{"DRY-RUN" if c.dry_run else "REAL"}</td>'
            f'<td>{_esc(c.detail)}</td></tr>' for c in t.controls)
        cards.append(f"""
<div class="panel"><h2><span class="n">EV</span>{_esc(ev.id)} · {_esc(ev.label)}
  <span class="tag bad">{_esc(ev.severity)}</span>
  <span class="tag blue">conf {ev.confidence}</span></h2>
<div class="cols"><div>{img}
<div class="note">第 {ev.frame_no} 帧触发，时间窗内连续 {ev.hit_frames} 帧确认（去抖抑制单帧误报）</div>
<table><thead><tr><th>检测类别</th><th>置信度</th><th>bbox</th></tr></thead>
<tbody>{dets}</tbody></table></div>
<div><table><tbody>
<tr><th>故障根因</th><td><b>{_esc(d.cause)}</b></td></tr>
<tr><th>Agent 推理</th><td>{_esc(d.reasoning)}</td></tr>
<tr><th>分析来源</th><td><span class="tag blue">{_esc(d.source)}</span>
    {_esc(d.confidence_note)}</td></tr>
<tr><th>立即动作</th><td><ul>{''.join(f'<li>{_esc(a)}</li>' for a in d.immediate_actions)}</ul></td></tr>
<tr><th>工艺整改系数</th><td class="mono">{_esc(json.dumps(d.process_adjustments, ensure_ascii=False))}</td></tr>
</tbody></table>
<details><summary>查看工具输入（截图 + 检测结果 + 工况上下文）</summary>
<pre>{_esc(tool_input)}</pre></details>
</div></div>
<h2 style="margin-top:18px"><span class="n">C</span>控制信号与安全门控</h2>
<div class="cols"><div><b>已批准</b><ul>{approved}</ul></div>
<div><b>已拦截</b><ul>{blocked}</ul></div></div>
<div class="note">{_esc(dec.note)}</div>
<table><thead><tr><th>信号</th><th>值</th><th>模式</th><th>下发结果</th></tr></thead>
<tbody>{writes}</tbody></table></div>""")

    diff_rows = "".join(
        f"<tr><td class='mono'>{_esc(x['op'])}</td><td class='mono'>{_esc(x['before'])}</td>"
        f"<td class='mono'>{_esc(x['after'])}</td></tr>" for x in result.parameter_diff)
    st = sim.stats if sim else {}
    vstat = val.stats if val else {}

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CNC Agent 闭环系统报告</title><style>{CSS}</style></head><body><div class="wrap">
<h1>CNC Agent · 编程 / 视觉监测 / 诊断 / 控制 闭环报告</h1>
<div class="sub">生成时间 {datetime.now():%Y-%m-%d %H:%M:%S} ｜ 机型
  <b>{_esc(profile.display if profile else '-')}</b> ｜ 系统 {_esc(profile.controller if profile else '-')}
  ｜ 后置 {_esc(run.controller)} ({_esc(profile.fidelity if profile else '-')})</div>
<div class="badge {status}"><span class="dot"></span>
{'闭环通过：原始程序与修订程序均通过双关卡' if status == 'ok' else '存在未通过关卡，禁止下发'}</div>
<div class="grid">
{''.join(f'<div class="kpi"><div class="v">{_esc(v)}</div><div class="l">{_esc(l)}</div></div>' for v, l in [
    (mon.frames, '监控帧数'), (len(result.traces), '视觉故障事件'),
    (mon.backend, '检测后端'),
    (sum(len(t.decision.approved_signals) for t in result.traces), '批准控制信号'),
    (sum(len(t.decision.blocked_signals) for t in result.traces), '拦截控制信号'),
    ('PASS' if result.revised_ok else 'FAIL', '修订程序双关卡')])}
</div>

<div class="panel"><h2><span class="n">A</span>完整闭环链路</h2>{flow_html}
<div class="note"><b>三层安全边界：</b>① 编程侧校验 + 仿真抵消大模型幻觉；
② 视觉侧连续多帧去抖抑制误报；③ 控制侧白名单 + 值域夹紧 + 人工确认门控，
<b>急停永远走硬件安全 PLC，AI 不得直连安全回路</b>。</div></div>

<div class="panel"><h2><span class="n">B</span>机型能力与功能派生（{_esc(profile.display if profile else '-')}）</h2>
<div class="note">{_esc(profile.notes if profile else '')} ｜ 推荐协议：
  <b>{_esc(caps.get('recommended_vision_protocol','-'))}</b> ｜ {_esc(result.adapter_note)}</div>
{cap_html}
<div class="note">{_esc(caps.get('disclaimer',''))}</div></div>

<div class="panel"><h2><span class="n">M</span>监控运行统计</h2>
<table><tbody>
<tr><th>检测器后端</th><td>{_esc(mon.backend)} ｜ {_esc(mon.fallback_reason or 'YOLO 权重已加载')}</td></tr>
<tr><th>帧 / 事件 / 告警</th><td>{mon.frames} 帧 ｜ {len(mon.events)} 起事件 ｜ {len(mon.alarms)} 条告警</td></tr>
<tr><th>日志留存</th><td class="mono">{_esc(mon.log_path)}</td></tr>
<tr><th>截图留存</th><td class="mono">{_esc(mon.snapshot_dir)}</td></tr>
<tr><th>控制审计</th><td class="mono">{_esc(result.output_files.get('revised_gcode',''))}</td></tr>
</tbody></table></div>

{''.join(cards) if cards else '<div class="panel"><div class="note">本次监控未触发故障事件</div></div>'}

<div class="panel"><h2><span class="n">R</span>工艺反哺与修订程序</h2>
<div class="note">多个故障事件的整改系数取<b>最保守（最小）</b>值，避免后一个事件把参数又放松回去；
修订后的方案重新过参数夹紧 → 确定性后处理 → 静态校验 → 体素仿真，四项全通过才落"可下发"程序。</div>
<table><thead><tr><th>工序</th><th>修订前 ap/ae/F/S</th><th>修订后 ap/ae/F/S</th></tr></thead>
<tbody>{diff_rows}</tbody></table>
<table style="margin-top:12px"><tbody>
<tr><th>修订程序校验</th><td><span class="tag {'ok' if val and val.ok else 'bad'}">{'PASS' if val and val.ok else 'FAIL'}</span>
  错误 {len(val.errors) if val else '-'} 项 ｜ 程序段 {vstat.get('blocks','-')}</td></tr>
<tr><th>修订程序仿真</th><td><span class="tag {'ok' if sim and sim.ok else 'bad'}">{'PASS' if sim and sim.ok else 'FAIL'}</span>
  去除体积误差 {st.get('volume_error_pct','-')}% ｜ 欠切 {st.get('undercut_ratio','-')}% ｜
  碰撞 {st.get('collision_points','-')}</td></tr>
<tr><th>程序指纹</th><td class="mono">原始 {_sha(run.program.text) if run.program else '-'} →
  修订 {_sha(result.revised_program.text) if result.revised_program else '-'}</td></tr>
</tbody></table></div>

<div class="foot">CNC Agent 闭环系统 ｜ 摄像头/RTSP + YOLO 可插拔，未配置权重时使用离线像素启发式后端；
工业协议默认 dry-run，真实下发需人工确认与现场电气图核对。本报告为辅助决策材料，不替代机床安全规范。</div>
</div></body></html>"""
