"""操控面板端到端自检：一次跑完 HTTP API + 真实 Modbus 写入 + 任务链路 + 只读模式。

前置：虚拟机床网关与面板服务已启动（panel/start_local.bat）。
用法：python panel/selftest.py [--full] [--readonly]
  --full      额外跑 monitor / closed_loop 任务（较慢）
  --readonly  临时起一个 8788 端口的只读实例，验证执行类 API 被禁用

输出逐项 PASS/FAIL 与汇总，退出码 0 表示全部通过。上线验收可直接复用。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.getenv("PANEL_TEST_URL", "http://127.0.0.1:8787")
RESULTS: list[tuple[str, bool, str]] = []


def req(method: str, path: str, data=None, timeout: float = 30.0,
        base: str = BASE, headers: dict | None = None):
    url = base + path
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    hd = {"Content-Type": "application/json"}
    if headers:
        hd.update(headers)
    r = urllib.request.Request(url, data=body, method=method, headers=hd)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return resp.status, json.loads(raw.decode("utf-8"))
            return resp.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except Exception:
            return e.code, raw.decode("utf-8", errors="replace")
    except Exception as e:
        return -1, {"error": f"{type(e).__name__}: {e}"}


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), str(detail)[:160]))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" | {str(detail)[:120]}" if detail else ""),
          flush=True)
    return bool(ok)


def wait_task(tid: str, limit: int = 240) -> dict:
    t0 = time.time()
    while time.time() - t0 < limit:
        code, t = req("GET", "/api/tasks/" + tid)
        if code == 200 and t.get("status") in ("done", "error"):
            return t
        time.sleep(0.6)
    return {"status": "timeout", "error": "等待任务超时"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="额外跑监控与闭环任务")
    ap.add_argument("--readonly", action="store_true", help="验证只读模式拦截")
    args = ap.parse_args()

    print(f"\n=== CNC 操控面板自检 {BASE} ===\n")
    print("[1] 静态资源与只读 API")
    code, html = req("GET", "/")
    check("首页可访问", code == 200 and "CNC Agent 操控台" in str(html), f"HTTP {code}")
    code, js = req("GET", "/static/app.js")
    check("前端脚本可访问", code == 200 and "function boot" in str(js), f"HTTP {code} {len(str(js))}B")
    code, css = req("GET", "/static/style.css")
    check("样式可访问", code == 200 and "--bg" in str(css), f"HTTP {code}")
    code, h = req("GET", "/api/health")
    check("健康检查", isinstance(h, dict) and h.get("ok") is True,
          f"mode={h.get('mode')} machines={h.get('machine_count')} write={h.get('control_mode')}"
          if isinstance(h, dict) else h)
    check("机型库 11 种", isinstance(h, dict) and h.get("machine_count") == 11)
    code, ms = req("GET", "/api/machines")
    check("机型列表接口", code == 200 and len(ms.get("machines", [])) == 11)
    code, cap = req("GET", "/api/machines/robodrill_d21")
    check("按机型派生能力", code == 200 and cap.get("total", 0) >= 20,
          f"共 {cap.get('total')} 项 {cap.get('by_status')}")
    code, st = req("GET", "/api/state")
    check("状态汇总接口", code == 200 and "machine" in st and "last_run" in st,
          f"files={st.get('file_count')} events={st.get('event_count')}")
    code, fl = req("GET", "/api/files")
    check("产物清单接口", code == 200 and fl.get("total", 0) > 0,
          f"{fl.get('total')} 个文件")
    code, _ = req("GET", "/view/report.html")
    check("报告可打开", code == 200)
    code, _ = req("GET", "/view/../../config.py")
    check("目录穿越被拒绝", code in (400, 403, 404), f"HTTP {code}")

    print("\n[2] 机床直连与安全门控")
    code, lstatus = req("GET", "/api/machine/status")
    check("写入总开关已开启", isinstance(lstatus, dict) and
          lstatus.get("server_allows_write") is True, lstatus.get("target"))
    code, r = req("POST", "/api/machine/connect",
                  {"host": "8.8.8.8", "port": 502, "allow_write": True})
    check("公网地址连接被拒绝", isinstance(r, dict) and r.get("ok") is False,
          r.get("error") if isinstance(r, dict) else r)
    code, r = req("POST", "/api/machine/connect",
                  {"host": "127.0.0.1", "port": 15020, "unit": 1,
                   "protocol": "modbus_tcp", "allow_write": True,
                   "machine_id": "vmc850_fanuc"})
    check("连接虚拟机床网关", isinstance(r, dict) and r.get("ok") is True,
          r.get("target") if isinstance(r, dict) else r)
    if not (isinstance(r, dict) and r.get("ok")):
        print("\n网关不可用，后续直连测试跳过。请先运行 python panel/plc_sim.py")
        return 1
    code, tm = req("GET", "/api/machine/telemetry")
    check("实时遥测读取", code == 200 and tm.get("connected") is True,
          f"状态={tm.get('state_label')} 心跳={tm.get('heartbeat')} S={tm.get('spindle_cmd')}")
    code, r = req("POST", "/api/machine/command",
                  {"signal": "FEED_OVERRIDE", "value": 75, "confirm": False})
    check("缺少二次确认被拦截", isinstance(r, dict) and r.get("ok") is False,
          r.get("error") if isinstance(r, dict) else r)
    code, r = req("POST", "/api/machine/command",
                  {"signal": "JOG_X", "value": 10, "confirm": True})
    check("白名单外指令被拦截", isinstance(r, dict) and r.get("ok") is False)
    code, r = req("POST", "/api/machine/command",
                  {"signal": "EMERGENCY_STOP", "value": True, "confirm": True})
    check("急停软写被拒绝", isinstance(r, dict) and r.get("ok") is False and
          "硬件" in str(r.get("error", "")), r.get("error") if isinstance(r, dict) else r)
    code, r = req("POST", "/api/machine/command",
                  {"signal": "FEED_OVERRIDE", "value": 999, "confirm": True})
    check("越限值被拦截", isinstance(r, dict) and r.get("ok") is False)
    code, r = req("POST", "/api/machine/command",
                  {"signal": "FEED_OVERRIDE", "value": 75, "confirm": True})
    check("真实写入 + 读回校验", isinstance(r, dict) and r.get("ok") is True,
          r.get("detail") if isinstance(r, dict) else r)
    code, tm = req("GET", "/api/machine/telemetry")
    check("遥测倍率已变更", tm.get("feed_override") == 75,
          f"feed_override={tm.get('feed_override')}")
    code, r = req("POST", "/api/machine/command",
                  {"signal": "CYCLE_START", "value": True, "confirm": True})
    check("循环启动写入", isinstance(r, dict) and r.get("ok") is True)
    code, tm = req("GET", "/api/machine/telemetry")
    check("机床状态变为运行", tm.get("state") == 1, f"state={tm.get('state_label')}")
    code, r = req("POST", "/api/machine/command",
                  {"signal": "FEED_HOLD", "value": True, "confirm": True})
    check("进给保持写入", isinstance(r, dict) and r.get("ok") is True)
    code, tm = req("GET", "/api/machine/telemetry")
    check("机床状态变为保持", tm.get("state") == 2, f"state={tm.get('state_label')}")
    code, r = req("POST", "/api/machine/program",
                  {"path": "part_vmc850_fanuc_FANUC.nc", "confirm": False})
    check("程序下发缺确认被拦截", isinstance(r, dict) and r.get("ok") is False)
    nc_file = Path("output/part_vmc850_fanuc_FANUC.nc")
    exp_raw = nc_file.read_bytes() if nc_file.is_file() else b""
    exp_bytes = len(exp_raw)
    exp_blocks = len([l for l in exp_raw.decode("ascii", errors="replace").splitlines()
                      if l.strip() and not l.strip().startswith("(")])
    code, r = req("POST", "/api/machine/program",
                  {"path": "part_vmc850_fanuc_FANUC.nc", "confirm": True}, timeout=60)
    check("G 代码真实下发 + 回读", isinstance(r, dict) and r.get("ok") is True and
          r.get("blocks", 0) >= exp_blocks - 2 and
          r.get("bytes", 0) >= exp_bytes - 8,
          f"实收 {r.get('bytes')}B/{r.get('blocks')}段/{r.get('frames')}帧 "
          f"vs 源文件 {exp_bytes}B/{exp_blocks}段"
          if isinstance(r, dict) else r)
    code, au = req("GET", "/api/machine/audit")
    recs = au.get("records", []) if isinstance(au, dict) else []
    kinds = {x.get("kind") for x in recs}
    check("审计日志完整", len(recs) >= 8 and
          {"connect", "command", "program", "connect_rejected"} <= kinds,
          f"{len(recs)} 条 / {sorted(kinds)}")

    print("\n[3] 后台任务链路")
    code, r = req("POST", "/api/tasks", {"type": "run", "params": {
        "text": "加工一块45#钢方块，长宽100×100mm，铣一个直径50mm圆形凹槽，深度3mm",
        "machine": "vmc850_fanuc", "controller": "", "deliver": "none"}})
    check("编程任务创建", code == 202 and r.get("ok") is True, r.get("task_id"))
    t = wait_task(r.get("task_id", ""))
    check("编程任务完成", t.get("status") == "done",
          f"status={t.get('status')} logs={len(t.get('logs', []))}")
    res = t.get("result") or {}
    check("编程结果双关卡 PASS", res.get("approved") is True and
          (res.get("validation") or {}).get("ok") is True,
          f"程序 {len((res.get('program') or {}).get('lines', []))} 行预览 / "
          f"仿真误差 {(res.get('simulation') or {}).get('volume_error_pct')}%")
    check("产物快照已隔离", bool(t.get("snapshot")), t.get("snapshot"))
    # 回归：曾因 /api/tasks 持 _TASKS_LOCK 时调用同样加锁的 _task_public 而自死锁，
    # 空任务表时不复现，一旦有任务记录就永久无响应，导致前端 boot 卡住、按钮全失效。
    t0 = time.time()
    code, tl = req("GET", "/api/tasks", timeout=5)
    dt = time.time() - t0
    check("任务列表有记录时不死锁", code == 200 and
          len(tl.get("tasks", [])) >= 1 and dt < 4,
          f"HTTP {code} / {len(tl.get('tasks', []))} 条 / {dt:.2f}s")
    js = Path("panel/static/app.js").read_text(encoding="utf-8")
    check("前端交互先于数据加载绑定",
          js.find("bindEvents();") < js.find("await loadAll();") and
          "safeApi" in js, "接口异常也不得让操控按钮失效")
    print("\n[3.5] 图纸编程 API")
    drawing_bytes = Path("examples/drawing_plate.dxf").read_bytes()
    code, ex = req("GET", "/api/drawing/example")
    check("示例工程图一键载入", code == 200 and ex.get("ok") is True and
          ex.get("backend") == "dxf" and
          len(ex.get("feature_rows", [])) == 4 and "<svg" in ex.get("svg", ""),
          f"{len(ex.get('feature_rows', []))} 特征 / 置信 {ex.get('confidence')}")
    code, up = req("POST", "/api/drawing/analyze", {
        "name": "drawing_plate.dxf",
        "content_base64": base64.b64encode(drawing_bytes).decode("ascii")})
    check("上传 DXF 并识别", code == 200 and up.get("ok") is True and
          len(up.get("feature_rows", [])) == 4)
    pj = up.get("part_json", "")
    try:
        parsed = json.loads(pj)
    except Exception:
        parsed = {}
    check("识别结果可编辑 JSON 结构合法",
          len(parsed.get("features", [])) == 4 and
          parsed.get("stock", {}).get("x") == 100)
    code, bad = req("POST", "/api/drawing/analyze",
                    {"name": "../../evil.dxf", "text": "0\nEOF\n"})
    check("上传文件名目录穿越被拒绝", code == 400)
    code, bad = req("POST", "/api/drawing/analyze",
                    {"name": "part.exe", "text": "MZ"})
    check("非法图纸扩展名被拒绝", code == 400)
    code, bad = req("POST", "/api/drawing/analyze", {
        "name": "p.png",
        "content_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode("ascii")})
    check("无视觉模型时位图明确报错", code == 400 and
          "LLM_API_KEY" in str(bad.get("error", "")))
    code, ed = req("POST", "/api/drawing/analyze",
                   {"name": "edited.json", "text": pj})
    check("编辑后的 JSON 可重新识别", code == 200 and ed.get("backend") == "json")
    code, rr = req("POST", "/api/tasks", {"type": "run", "params": {
        "text": pj, "source": "json", "no_files": True,
        "machine": "vmc850_fanuc"}})
    t4 = wait_task(rr.get("task_id", ""))
    r4 = t4.get("result") or {}
    check("图纸 -> G 代码完整链路通过双关卡",
          t4.get("status") == "done" and r4.get("approved") is True and
          (r4.get("validation") or {}).get("ok") is True and
          (r4.get("simulation") or {}).get("collision_points", 0) == 0,
          f"{((r4.get('program') or {}).get('blocks'))} 段 / "
          f"误差 {(r4.get('simulation') or {}).get('volume_error_pct')}%")

    if args.full:
        code, r = req("POST", "/api/tasks", {"type": "monitor",
                                            "params": {"frames": "demo", "max_frames": 145}})
        t2 = wait_task(r.get("task_id", ""))
        s2 = (t2.get("result") or {}).get("summary") or {}
        check("视觉监控任务", t2.get("status") == "done" and s2.get("frames") == 145,
              f"{s2.get('frames')} 帧 / {s2.get('events')} 事件 / {s2.get('backend')}")
        code, r = req("POST", "/api/tasks", {"type": "closed_loop", "params": {
            "text": "加工一块45#钢方块，长宽100×100mm，铣一个直径50mm圆形凹槽，深度3mm",
            "machine": "hnc_848d_tv856", "frames": "demo", "max_frames": 145}})
        t3 = wait_task(r.get("task_id", ""))
        s3 = (t3.get("result") or {}).get("summary") or {}
        check("完整闭环任务", t3.get("status") == "done" and
              s3.get("revised_program_ok") is True,
              f"批准 {s3.get('approved_signals')} / 拦截 {s3.get('blocked_signals')} / "
              f"修订 PASS={s3.get('revised_program_ok')}")

    if args.readonly:
        print("\n[4] 只读上线模式 + 令牌鉴权")
        TOKEN = "selftest-token-123"
        env = dict(os.environ, PANEL_MODE="readonly", PANEL_PORT="8788",
                   PANEL_ALLOW_WRITE="0", PANEL_HOST="127.0.0.1",
                   PANEL_TOKEN=TOKEN)
        p = subprocess.Popen([sys.executable, "panel/server.py"], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            rb = "http://127.0.0.1:8788"
            ok = False
            for _ in range(25):
                c, hh = req("GET", "/api/health", base=rb, timeout=2)
                if c == 200 and isinstance(hh, dict) and hh.get("readonly"):
                    ok = True
                    break
                time.sleep(0.3)
            check("只读实例启动", ok)
            c, hh = req("GET", "/api/health", base=rb)
            check("健康检查标记鉴权", c == 200 and isinstance(hh, dict)
                  and hh.get("auth_required") is True)
            c, rr = req("GET", "/api/state", base=rb)
            check("未带令牌 API 被拒绝", c == 401)
            c, js = req("GET", "/static/app.js", base=rb)
            check("登录页静态资源免鉴权", c == 200 and "function boot" in str(js))
            c, lg = req("POST", "/api/login", {"token": TOKEN}, base=rb)
            check("登录接口返回成功", c == 200 and isinstance(lg, dict)
                  and lg.get("ok") is True)
            H = {"X-Panel-Token": TOKEN}
            c, rr = req("GET", "/api/state", base=rb, headers=H)
            check("带令牌可读取状态", c == 200 and isinstance(rr, dict) and "machine" in rr)
            c, rr = req("POST", "/api/tasks", {"type": "run", "params": {}},
                        base=rb, headers=H)
            check("只读模式禁用执行类任务", c == 403 and isinstance(rr, dict)
                  and rr.get("readonly") is True,
                  rr.get("error") if isinstance(rr, dict) else rr)
            c, rr = req("POST", "/api/machine/command",
                        {"signal": "FEED_HOLD", "confirm": True}, base=rb, headers=H)
            check("只读模式禁用机床写入", c == 403)
            c, rr = req("GET", "/view/report.html", base=rb)
            check("报告未带令牌被拒绝", c == 401)
            c, rr = req("GET", "/view/report.html", base=rb, headers=H)
            check("带令牌可打开报告", c == 200)
            c, rr = req("GET", "/api/files", base=rb, headers=H)
            check("只读模式保留产物查看", c == 200 and isinstance(rr, dict)
                  and rr.get("total", 0) > 0)
            c, dr = req("GET", "/api/drawing/example", base=rb, headers=H)
            check("只读模式仍可看示例图纸", c == 200 and dr.get("ok") is True)
            c, dr = req("POST", "/api/drawing/analyze",
                        {"name": "p.png", "content_base64":
                         base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode("ascii")},
                        base=rb, headers=H)
            check("只读模式禁用位图识读（不触发外部视觉模型）", c == 400 and
                  "只读模式" in str(dr.get("error", "")))
        finally:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 66)
    print(f"  自检结果：{passed}/{total} PASS" + ("  ✅ 全部通过" if passed == total else "  ❌ 存在失败"))
    print("=" * 66)
    if passed != total:
        print("失败项：")
        for n, ok, d in RESULTS:
            if not ok:
                print(f"  - {n} | {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
