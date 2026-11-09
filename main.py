"""CNC Agent 命令行入口。

常用命令：
  python main.py demo                           # 跑通完整链路（示例零件）
  python main.py demo --hallucination           # 注入幻觉，验证拦截 + 自修复
  python main.py run --text "45#钢 100x100x20 铣直径50圆槽深3mm" --controller SIEMENS
  python main.py run --file examples/part.json
  python main.py controllers                    # 三种数控系统方言对比
  python main.py legacy --file examples/legacy.nc
  python main.py alarms                         # 机床报警日志智能诊断
  python main.py prompt                         # 导出核心 System Prompt
  python main.py drawing-schema                 # 导出图纸/图片 OCR 的结构化抽取模板
  python main.py from-drawing --file examples/drawing_plate.dxf   # 图纸 -> G 代码
  python main.py from-drawing --analyze-only   # 只识别图纸，不生成程序
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dataclasses import replace

from cncagent.agent import CNCAgent, RunResult, diagnose_legacy
from cncagent.config import EXAMPLE_DIR, OUTPUT_DIR, get_settings
from cncagent.cutting import estimate_cycle_time
from cncagent.diagnose import (SAMPLE_ALARM_LOG, alarm_summary,
                               build_delivery, diagnose_alarms)
from cncagent.drawing import DrawingError, analyze_file
from cncagent.inputs import create_drawing_interpreter_payload
from cncagent.machines import (PROFILES, capability_summary, export_catalog,
                               get_profile, list_profiles)
from cncagent.prompts import build_system_prompt, example_plan_json
from cncagent.vision.closed_loop import ClosedLoopController
from cncagent.vision.detector import create_detector
from cncagent.vision.frames import make_source
from cncagent.vision.monitor import MonitorConfig, VisionMonitor

DEMO_TEXT = "加工一块45#钢方块，长宽100×100mm，铣一个直径50mm圆形凹槽，深度3mm"


def _print_result(r: RunResult) -> None:
    print("\n" + "=" * 74)
    print("  CNC Agent 运行报告")
    print("=" * 74)
    for s in r.stages:
        mark = {"pass": "[PASS]", "fail": "[FAIL]", "repaired": "[FIXED]",
                "blocked": "[BLOCK]"}.get(s.status, "[----]")
        print(f"  {mark:<8} {s.name}")
        print(f"           {s.detail}")
        for issue in s.issues[:4]:
            print(f"           - {issue}")
        if len(s.issues) > 4:
            print(f"           ... 另有 {len(s.issues) - 4} 条")
    print("-" * 74)
    if r.validation:
        print(f"  静态校验 : {'PASS' if r.validation.ok else 'FAIL'} "
              f"(错误 {len(r.validation.errors)} / 警告 {len(r.validation.warnings)})")
    if r.simulation:
        st = r.simulation.stats
        print(f"  体素仿真 : {'PASS' if r.simulation.ok else 'FAIL'} "
              f"网格 {st['grid']} ｜ 去除体积误差 {st['volume_error_pct']}% ｜ "
              f"欠切 {st['undercut_ratio']}% ｜ 碰撞 {st['collision_points']}")
    print(f"  规划器   : {r.plan.planner} ｜ 工序 {len(r.plan.operations)} ｜ "
          f"修复轮次 {r.attempts}")
    if r.program:
        print(f"  程序     : {r.controller} ｜ {r.program.block_count} 段 ｜ "
              f"预估节拍 {estimate_cycle_time(r.plan, r.program.metadata.get('cut_length', 0))} min")
    print(f"  下发准入 : {'✅ 双关卡通过，可进入人工复核' if r.approved else '⛔ 拒绝下发'}")
    if r.blocked_reason:
        print(f"  拦截原因 : {r.blocked_reason}")
    for k, v in r.output_files.items():
        print(f"  产出 {k:<8}: {v}")
    print("=" * 74 + "\n")


def _read_request(args) -> str:
    if args.file:
        return str(Path(args.file).resolve())
    return args.text or DEMO_TEXT


def _settings(args):
    s = get_settings()
    if getattr(args, "machine", None):
        s = replace(s, machine_id=args.machine)
    if getattr(args, "controller", None):
        s = replace(s, controller=args.controller)
    return s


def cmd_run(args) -> None:
    settings = _settings(args)
    agent = CNCAgent(settings)
    inject = None
    faults = None
    if args.hallucination:
        # 模拟两类幻觉：① 参数层（S/F/ap 越界）② 代码层（错误 G 代码段）
        inject = {"spindle": 30000, "feed": 9000, "ap": 25.0}
        faults = ["overtravel", "plunge", "bad_g", "overspeed"]
        print("  [演示] 已注入幻觉参数 S=30000 F=9000 ap=25，"
              "以及 4 条错误 G 代码段")
    r = agent.run(
        _read_request(args), source=args.source,
        inject=inject, faults=faults,
        write_files=not args.no_files)
    _print_result(r)
    if args.deliver != "none" and not args.no_files:
        pkg = build_delivery(r, OUTPUT_DIR, args.deliver)
        print(f"  [下发] {'允许' if pkg.approved else '拒绝'} ｜ "
              f"SHA256 {pkg.sha256[:24] if pkg.sha256 else '-'} ｜ {pkg.note}")
        (OUTPUT_DIR / "delivery.json").write_text(
            json.dumps(pkg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_controllers(args) -> None:
    """同一工艺方案，三种后置方言 + 各自校验/仿真结果对比。"""
    from cncagent.cutting import clamp_plan
    from cncagent.generator import make_post
    from cncagent.geometry import generate_toolpaths
    from cncagent.inputs import parse_natural_language
    from cncagent.planner import RulePlanner
    from cncagent.simulator import simulate_gcode
    from cncagent.validator import Validator

    part = parse_natural_language(args.text or DEMO_TEXT)
    plan = clamp_plan(RulePlanner().plan(part))
    tps = generate_toolpaths(plan)
    print(f"\n零件：{part.material.name} {part.stock_x:g}x{part.stock_y:g} "
          f"｜ 特征 {len(part.features)} 个 ｜ 工序 {len(plan.operations)} 道\n")
    print(f"{'控制器':<10}{'程序段':>7}{'校验':>8}{'仿真':>8}"
          f"{'体积误差':>11}{'首行':<28}")
    print("-" * 78)
    for ctrl in ("FANUC", "SIEMENS", "HNC"):
        prog = make_post(ctrl, plan).generate(tps)
        val = Validator(plan).validate(prog.text, ctrl)
        sim = simulate_gcode(prog.text, plan, ctrl)
        head = next((l.strip() for l in prog.lines if l.strip()), "")[:26]
        print(f"{ctrl:<10}{prog.block_count:>7}{'PASS' if val.ok else 'FAIL':>8}"
              f"{'PASS' if sim.ok else 'FAIL':>8}"
              f"{sim.stats['volume_error_pct']:>10}%  {head:<28}")
    print("\n方言差异：FANUC/HNC 用 O 程序号 + G43 H + G81 + G91 G28 Z0；"
          "\n          SIEMENS 840D 用 %_N_..._MPF + G71 + T/D1 + CYCLE81 + G74 回参考点。\n")


def cmd_legacy(args) -> None:
    info = diagnose_legacy(str(Path(args.file).resolve()))
    print("\n旧 G 代码体检报告")
    print("-" * 60)
    print(f"  文件       : {info['path']}")
    print(f"  行数       : {info['line_count']}")
    print(f"  S 范围     : {info['spindle_range']}")
    print(f"  F 范围     : {info['feed_range']}")
    print(f"  使用刀号   : {info['tools']}")
    print(f"  坐标范围   : {info['axis_range']}")
    print("  优化建议：")
    for s in info["suggestions"]:
        print(f"    - {s}")
    print(f"  说明：{info['note']}\n")


def cmd_alarms(args) -> None:
    text = Path(args.file).read_text(encoding="utf-8") if args.file else SAMPLE_ALARM_LOG
    ds = diagnose_alarms(text)
    print("\n机床报警智能诊断")
    print("-" * 78)
    for d in ds:
        print(f"  L{d.line_no:<3} [{d.severity.upper():<8}] {d.raw}")
        print(f"        原因：{d.cause}")
        print(f"        处置：{d.action}")
    print(f"\n  汇总：{alarm_summary(ds)}\n")


def cmd_prompt(args) -> None:
    out = (build_system_prompt() +
           "\n\n# 九、Few-shot 输出示例（供参考，不要照抄参数）\n" +
           example_plan_json() + "\n")
    path = OUTPUT_DIR / "system_prompt.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out, encoding="utf-8")
    print(f"System Prompt 已导出：{path}（{len(out)} 字符）")


def cmd_drawing_schema(args) -> None:
    payload = create_drawing_interpreter_payload("图纸/图片 OCR 抽取")
    path = OUTPUT_DIR / "drawing_schema.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    print(f"图纸视觉抽取模板已导出：{path}\n{payload[:400]} ...")


def cmd_from_drawing(args) -> None:
    """工程图 -> 结构化特征 -> 既有完整编程链路（夹紧/校验/仿真/下发准入）。"""
    path = Path(args.file)
    if not path.exists():
        print(f"  [错误] 图纸不存在：{path}")
        raise SystemExit(2)
    try:
        a = analyze_file(path, args.hint)
    except DrawingError as exc:
        print(f"  [图纸解析失败] {exc}")
        raise SystemExit(2)

    print("\n" + "=" * 74)
    print("  图纸识别报告")
    print("=" * 74)
    print(f"  文件      : {path.name}")
    print(f"  解析后端  : {a.backend}   置信度 {a.confidence:.2f}")
    print(f"  图元/标注 : {len(a.entities)} / {len(a.notes)}")
    print(f"  零件      : {a.part.name} | {a.part.material.name} | "
          f"毛坯 {a.part.stock_x:g}×{a.part.stock_y:g}×{a.part.stock_z:g} mm")
    print("-" * 74)
    for row in a.feature_rows:
        size = (f"Φ{row['diameter']:g}" if row.get("diameter")
                else f"{row.get('width', 0):g}×{row.get('height', 0):g}")
        print(f"  F{row['index']} {row['kind']:<15} 中心({row['cx']:g},{row['cy']:g}) "
              f"尺寸 {size:<12} 深{row['depth']:g}  ±{row['tolerance']:g}  "
              f"置信 {row['confidence']:.2f}")
        print(f"      标注: {row['note']}")
    for w in a.warnings:
        print(f"  [需复核] {w}")
    print("=" * 74)

    if not args.no_files:
        (OUTPUT_DIR / "drawing_analysis.json").write_text(
            json.dumps(a.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        (OUTPUT_DIR / "drawing_preview.svg").write_text(a.svg, encoding="utf-8")
        (OUTPUT_DIR / "drawing_part.json").write_text(a.part_json, encoding="utf-8")
        print(f"  [产出] 识别报告 / SVG 图纸 / 可编辑零件 JSON -> {OUTPUT_DIR}")
    if args.analyze_only:
        print("  --analyze-only：已停止，未生成 G 代码\n")
        return

    print("\n  >>> 将识别结果送入编程链路（参数夹紧 → 后处理 → 静态校验 → 体素仿真）")
    settings = _settings(args)
    agent = CNCAgent(settings)
    r = agent.run(a.part_json, source="json",
                  controller=args.controller, write_files=not args.no_files)
    _print_result(r)
    if args.deliver != "none" and not args.no_files:
        pkg = build_delivery(r, OUTPUT_DIR, args.deliver)
        print(f"  [下发] {'允许' if pkg.approved else '拒绝'} ｜ "
              f"SHA256 {pkg.sha256[:24] if pkg.sha256 else '-'} ｜ {pkg.note}")


def cmd_machines(args) -> None:
    """机型目录与按机型派生的功能清单。"""
    if args.id:
        p = get_profile(args.id)
        cap = capability_summary(p)
        print(f"\n机型：{p.display}")
        print(f"  类别 / 系统   : {p.category} ｜ {p.controller}")
        print(f"  后置 / 保真度 : {p.post} ｜ {p.fidelity}")
        print(f"  行程 X/Y/Z    : {p.spec.x_max*2:g} / {p.spec.y_max*2:g} / "
              f"{abs(p.spec.z_min):g} mm")
        print(f"  主轴 / 进给   : {p.spec.max_spindle} rpm ｜ "
              f"{p.spec.max_feed} mm/min ｜ {p.spec.power_kw:g} kW")
        print(f"  刀柄 / 刀库   : {p.spindle_taper} ｜ {p.tool_count} 把 ｜ "
              f"快移 {p.rapid_m_min:g} m/min")
        print(f"  通信协议      : {', '.join(p.protocols)}")
        print(f"  遥测数据      : {', '.join(p.telemetry) or '无'}")
        print(f"  视觉接入      : {p.vision_support}")
        print(f"  推荐协议      : {cap['recommended_vision_protocol']}")
        print(f"  说明          : {p.notes}\n")
        print(f"  按机型派生的功能（共 {cap['total']} 项，"
              f"{cap['by_status']}）：")
        print(f"  {'类别':<8}{'功能':<34}{'状态':<12}依据")
        print("  " + "-" * 96)
        for c in cap["capabilities"]:
            print(f"  {c['group']:<8}{c['name'][:32]:<34}{c['status']:<12}"
                  f"{c['reason'][:46]}")
        print(f"\n  {cap['disclaimer']}\n")
    else:
        print(f"\n{'ID':<24}{'机型':<34}{'控制器':<24}{'后置':<10}{'Smax':>7}{'Fmax':>8}  类别")
        print("-" * 128)
        for p in list_profiles():
            print(f"{p.id:<24}{p.display[:32]:<34}{p.controller[:22]:<24}"
                  f"{p.post:<10}{p.spec.max_spindle:>7}{p.spec.max_feed:>8}  {p.category}")
        print("\n用 --id <ID> 查看该机型完整功能清单；--export 导出全机型能力目录 JSON。")
    if args.export:
        path = export_catalog(OUTPUT_DIR / "machine_catalog.json")
        print(f"机型能力目录已导出：{path}")


def _vision_source(args):
    kind = getattr(args, "frames", None) or "demo"
    return make_source(kind, device=getattr(args, "device", 0),
                       file=getattr(args, "video", ""),
                       directory=getattr(args, "images", ""),
                       max_frames=getattr(args, "max_frames", None))


def cmd_monitor(args) -> None:
    """独立视觉监控演示（不含闭环重编程）。"""
    settings = _settings(args)
    profile = get_profile(settings.machine_id)
    detector, note = create_detector(args.weights or settings.vision_weights)
    source = _vision_source(args)
    cfg = MonitorConfig(max_frames=args.max_frames, webhook=args.webhook)
    ctx = {"machine": profile.display, "controller": profile.controller,
           "mode": "standalone_monitor"}
    mon = VisionMonitor(source, detector, OUTPUT_DIR / "vision", cfg,
                        process_context=ctx, fallback_reason=note)
    print(f"\n[监控] 帧源 {source.name} ｜ 检测器 {detector.name} ｜ "
          f"{note or 'YOLO 权重已加载'}")
    res = mon.run()
    print("\n监控结果：" + json.dumps(res.summary(), ensure_ascii=False))
    print(f"  日志   : {res.log_path}")
    print(f"  截图   : {res.snapshot_dir}")
    for e in res.events:
        print(f"  - {e.id} 第{e.frame_no}帧 {e.label} conf={e.confidence} "
              f"严重度={e.severity}")


def cmd_closed_loop(args) -> None:
    """完整闭环：编程 -> 视觉监测 -> Agent 诊断 -> 控制下发 -> 工艺反哺。"""
    settings = _settings(args)
    profile = get_profile(settings.machine_id)
    detector, note = create_detector(args.weights or settings.vision_weights)
    source = _vision_source(args)
    controller = ClosedLoopController(settings, profile, OUTPUT_DIR)
    result = controller.run_full(
        args.text or DEMO_TEXT, detector, note, source=source,
        protocol=args.protocol, dry_run=not args.live,
        human_confirmed=args.human_confirmed,
        cfg=MonitorConfig(max_frames=args.max_frames, webhook=args.webhook))
    print("\n" + "=" * 74)
    print("  闭环运行结果")
    print("=" * 74)
    for k, v in result.summary.items():
        print(f"  {k:<22}: {v}")
    print("-" * 74)
    for k, v in result.output_files.items():
        print(f"  产出 {k:<18}: {v}")
    print("=" * 74 + "\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="CNC Agent：自然语言 -> 工艺规划 -> G 代码 -> 校验 -> 仿真 -> 下发",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--text", help="自然语言零件需求")
        sp.add_argument("--file", help="输入文件（.json/.step/.nc/.txt）")
        sp.add_argument("--source", default="auto",
                        choices=["auto", "json", "step", "legacy"],
                        help="输入类型（默认自动识别）")
        sp.add_argument("--controller", default=None,
                        choices=["FANUC", "SIEMENS", "HNC"],
                        help="目标数控系统（默认读 .env 的 CNC_CONTROLLER）")
        sp.add_argument("--hallucination", action="store_true",
                        help="注入幻觉参数与错误 G 代码，演示安全屏障")
        sp.add_argument("--deliver", default="none", choices=["none", "usb", "dnc"],
                        help="生成下发包（dnc 为 dry-run，不连机床）")
        sp.add_argument("--machine", default=None, choices=sorted(PROFILES),
                        help="目标机型 ID（决定行程/转速/方言/能力/协议）")
        sp.add_argument("--no-files", action="store_true", help="不落盘，仅打印结果")

    run = sub.add_parser("run", help="运行完整链路")
    add_common(run); run.set_defaults(func=cmd_run)
    demo = sub.add_parser("demo", help="用内置示例零件跑通链路")
    add_common(demo); demo.set_defaults(func=cmd_run)
    ctl = sub.add_parser("controllers", help="三种数控系统方言对比")
    ctl.add_argument("--text"); ctl.set_defaults(func=cmd_controllers)
    lg = sub.add_parser("legacy", help="旧 G 代码体检与优化建议")
    lg.add_argument("--file", required=True); lg.set_defaults(func=cmd_legacy)
    al = sub.add_parser("alarms", help="机床报警日志诊断")
    al.add_argument("--file"); al.set_defaults(func=cmd_alarms)
    pr = sub.add_parser("prompt", help="导出核心 System Prompt")
    pr.set_defaults(func=cmd_prompt)
    ds = sub.add_parser("drawing-schema", help="导出图纸 OCR 结构化模板")
    ds.set_defaults(func=cmd_drawing_schema)

    fd = sub.add_parser("from-drawing", help="读工程图 -> 结构化特征 -> G 代码 -> 校验/仿真/下发")
    fd.add_argument("--file", default=str(EXAMPLE_DIR / "drawing_plate.dxf"),
                    help="图纸路径（.dxf 矢量 / .json 结构化 / .png 位图需视觉模型）")
    fd.add_argument("--hint", default="", help="给视觉模型的补充提示（仅位图有效）")
    fd.add_argument("--analyze-only", action="store_true", help="只输出识别结果，不生成 G 代码")
    fd.add_argument("--machine", default=None, choices=sorted(PROFILES),
                    help="目标机型 ID")
    fd.add_argument("--controller", default=None,
                    choices=["FANUC", "SIEMENS", "HNC"], help="强制指定后置方言")
    fd.add_argument("--deliver", default="none", choices=["none", "usb", "dnc"])
    fd.add_argument("--no-files", action="store_true", help="不落盘，仅打印")
    fd.set_defaults(func=cmd_from_drawing)

    mc = sub.add_parser("machines", help="机型目录与按机型派生的功能清单")
    mc.add_argument("--id", default=None, choices=sorted(PROFILES),
                    help="查看指定机型的完整能力")
    mc.add_argument("--export", action="store_true", help="导出全机型能力目录 JSON")
    mc.set_defaults(func=cmd_machines)

    def add_vision(sp):
        sp.add_argument("--frames", default="demo",
                        choices=["demo", "camera", "video", "images"],
                        help="帧源：demo 合成工况 / camera 摄像头 / video 视频 / images 图片序列")
        sp.add_argument("--device", type=int, default=0, help="摄像头索引或 RTSP 地址")
        sp.add_argument("--video", default="", help="视频文件路径")
        sp.add_argument("--images", default="", help="图片序列目录")
        sp.add_argument("--weights", default="", help="自训练 YOLO 权重 .pt（留空用启发式后端）")
        sp.add_argument("--max-frames", type=int, default=None, help="最多处理的帧数")
        sp.add_argument("--webhook", default="", help="告警 Webhook 地址")
        sp.add_argument("--machine", default=None, choices=sorted(PROFILES))
        sp.add_argument("--controller", default=None,
                        choices=["FANUC", "SIEMENS", "HNC"])

    mn = sub.add_parser("monitor", help="独立机器视觉监控演示")
    add_vision(mn)
    mn.set_defaults(func=cmd_monitor)

    cl = sub.add_parser("closed-loop", help="编程+视觉监测+诊断+控制+工艺反哺完整闭环")
    add_vision(cl)
    cl.add_argument("--text", default=None, help="零件需求（默认内置示例）")
    cl.add_argument("--protocol", default="auto",
                    choices=["auto", "dryrun", "modbus", "opcua", "mqtt"])
    cl.add_argument("--live", action="store_true",
                    help="真实连接工业协议下发（默认 dry-run，仅生成审计指令）")
    cl.add_argument("--human-confirmed", action="store_true",
                    help="模拟操作员已确认（影响急停门控）")
    cl.set_defaults(func=cmd_closed_loop)
    return p


def main() -> int:
    args = build_parser().parse_args()
    if getattr(args, "command", None) == "demo" and not args.text and not args.file:
        args.text = DEMO_TEXT
    try:
        args.func(args)
    except Exception as exc:
        print(f"\n[错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        if "--debug" in sys.argv:
            raise
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
