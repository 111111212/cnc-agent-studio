"""虚拟机床 PLC 网关（Modbus TCP 从站，纯标准库）。

用途：在没有真实机床的本机上，验证「面板连接 -> 读遥测 -> 下发控制指令 ->
程序下发 -> 状态回读」的完整真实网络链路。上线时把面板里的主机/端口换成
车间网关地址即可，客户端代码不变。

!! 这不是机床仿真器：不模拟切削物理，只模拟 PLC 寄存器与状态机行为。
!! 真实寄存器映射必须以机床电气图 / PLC 变量表为准，本文件仅为示例映射。

启动：python panel/plc_sim.py [--host 127.0.0.1] [--port 15020]
"""
from __future__ import annotations

import argparse
import json
import socketserver
import struct
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = ROOT / "output" / "machine"

# ---- 示例寄存器映射（与 cncagent/vision/protocols.py 的控制地址保持一致）----
COIL = {
    "FEED_HOLD": 0x0100,
    "CYCLE_STOP": 0x0101,
    "SPINDLE_STOP": 0x0102,
    "CYCLE_START": 0x0103,
    "RESET": 0x0104,
    "COOLANT_ON": 0x0110,
    "ALARM_LIGHT": 0x0120,
}
HOLD = {
    "FEED_OVERRIDE": 0x0200,        # 可写：进给倍率 %
    "SPINDLE_CMD": 0x1000,          # 主轴设定转速
    "SPINDLE_ACT": 0x1001,          # 主轴实际转速
    "SPINDLE_LOAD": 0x1002,         # 主轴负载 %
    "FEED_ACT": 0x1003,             # 实际进给 mm/min
    "X_POS": 0x1005,                # X 坐标 ×10（有符号 16 位）
    "Y_POS": 0x1006,
    "Z_POS": 0x1007,
    "RUN_STATE": 0x1008,            # 0待机 1运行 2保持 3报警
    "ALARM_CODE": 0x1009,
    "PROGRAM_NO": 0x100A,
    "BLOCK_NO": 0x100B,
    "BLOCK_TOTAL": 0x100C,
    "COOLANT_STATE": 0x100D,
    "SPINDLE_STATE": 0x100E,
    "HEARTBEAT": 0x100F,
    "PROGRAM_BUFFER": 0x0300,       # FC16 写入 G 代码文本（每寄存器 2 字节）
    "PROGRAM_COMMIT": 0x0310,       # 写 1 = 缓冲区收尾入库
    "PROGRAM_BYTES": 0x0311,        # 回读：已接收字节数
    "PROGRAM_BLOCKS": 0x0312,       # 回读：程序段数
}
STATE_NAME = {0: "待机", 1: "运行", 2: "进给保持", 3: "报警"}


def _s16(v: int) -> int:
    return v & 0xFFFF


class MachineState:
    """带锁的寄存器状态机；tick 线程模拟加工过程的状态演化。"""

    def __init__(self, audit_dir: Path = AUDIT_DIR):
        self.lock = threading.RLock()
        self.coils = {a: False for a in COIL.values()}
        self.holding = {a: 0 for a in HOLD.values()}
        self.holding[HOLD["SPINDLE_CMD"]] = 2700
        self.holding[HOLD["SPINDLE_ACT"]] = 0
        self.holding[HOLD["FEED_OVERRIDE"]] = 100
        self.holding[HOLD["FEED_ACT"]] = 900
        self.holding[HOLD["RUN_STATE"]] = 0
        self.holding[HOLD["PROGRAM_NO"]] = 1
        self.holding[HOLD["BLOCK_TOTAL"]] = 70
        self.holding[HOLD["BLOCK_NO"]] = 0
        self.holding[HOLD["HEARTBEAT"]] = 0
        self.holding[HOLD["Z_POS"]] = _s16(50 * 10)   # 安全高度 Z50.0
        self.program_chunks: list[bytes] = []
        self.audit_dir = audit_dir
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.audit = self.audit_dir / "plc_audit.jsonl"
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._t.start()

    def stop(self):
        self._stop.set()

    def log(self, kind: str, payload: dict):
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "kind": kind, **payload}
        with self.lock:
            with self.audit.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _loop(self):
        n = 0
        while not self._stop.wait(0.4):
            n += 1
            with self.lock:
                st = self.holding[HOLD["RUN_STATE"]]
                self.holding[HOLD["HEARTBEAT"]] = (n // 2) & 0xFFFF
                if st == 1:
                    # 运行中：主轴转动、负载波动、进给随倍率、坐标缓动、段号推进
                    ov = max(0, min(150, self.holding[HOLD["FEED_OVERRIDE"]]))
                    self.holding[HOLD["SPINDLE_ACT"]] = self.holding[HOLD["SPINDLE_CMD"]]
                    self.holding[HOLD["SPINDLE_STATE"]] = 1
                    self.holding[HOLD["FEED_ACT"]] = int(900 * ov / 100)
                    self.holding[HOLD["SPINDLE_LOAD"]] = 38 + (n % 9)
                    self.holding[HOLD["X_POS"]] = _s16(
                        (self.holding[HOLD["X_POS"]] + (1 if n % 4 else -1)))
                    self.holding[HOLD["BLOCK_NO"]] = min(
                        self.holding[HOLD["BLOCK_TOTAL"]],
                        self.holding[HOLD["BLOCK_NO"]] + (1 if n % 3 == 0 else 0))
                    if self.holding[HOLD["BLOCK_NO"]] >= self.holding[HOLD["BLOCK_TOTAL"]]:
                        self.holding[HOLD["RUN_STATE"]] = 0
                        self.holding[HOLD["FEED_ACT"]] = 0
                        self.log("state", {"note": "程序执行完毕，回到待机"})
                elif st == 2:
                    self.holding[HOLD["FEED_ACT"]] = 0
                else:
                    self.holding[HOLD["SPINDLE_ACT"]] = 0
                    self.holding[HOLD["SPINDLE_LOAD"]] = 0
                    self.holding[HOLD["SPINDLE_STATE"]] = 0
                    self.holding[HOLD["FEED_ACT"]] = 0
                # 脉冲线圈：PLC 边沿触发后自动复位
                for a in (COIL["CYCLE_START"], COIL["FEED_HOLD"],
                          COIL["SPINDLE_STOP"], COIL["RESET"]):
                    self.coils[a] = False

    # ---- Modbus 功能码处理 ------------------------------------------------
    def handle(self, pdu: bytes) -> bytes:
        fc = pdu[0]
        d = pdu[1:]

        def err(code: int) -> bytes:
            return bytes([fc | 0x80, code])

        with self.lock:
            if fc == 0x01:                       # 读线圈
                if len(d) < 4:
                    return err(3)
                start, qty = struct.unpack(">HH", d[:4])
                if not 1 <= qty <= 2000:
                    return err(3)
                bits = bytearray((qty + 7) // 8)
                for i in range(qty):
                    if self.coils.get(start + i, False):
                        bits[i // 8] |= 1 << (i % 8)
                return bytes([fc, len(bits)]) + bytes(bits)

            if fc == 0x03:                       # 读保持寄存器
                if len(d) < 4:
                    return err(3)
                start, qty = struct.unpack(">HH", d[:4])
                if not 1 <= qty <= 125:
                    return err(3)
                vals = [self.holding.get(start + i, 0) & 0xFFFF for i in range(qty)]
                return bytes([fc, qty * 2]) + struct.pack(">" + "H" * qty, *vals)

            if fc == 0x05:                       # 写单线圈
                if len(d) < 4:
                    return err(3)
                addr, val = struct.unpack(">HH", d[:4])
                if addr not in self.coils:
                    return err(2)                # 非法数据地址
                on = val == 0xFF00
                self.coils[addr] = on
                self._apply_coil(addr, on)
                self.log("coil", {"addr": hex(addr), "value": on,
                                  "state": STATE_NAME.get(self.holding[HOLD["RUN_STATE"]], "?")})
                return pdu[:5]

            if fc == 0x06:                       # 写单寄存器
                if len(d) < 4:
                    return err(3)
                addr, val = struct.unpack(">HH", d[:4])
                if addr not in self.holding:
                    return err(2)
                if addr == HOLD["PROGRAM_COMMIT"] and val == 1:
                    r = self.store_program()
                    self.holding[HOLD["PROGRAM_BYTES"]] = r.get("bytes", 0)
                    self.holding[HOLD["PROGRAM_BLOCKS"]] = r.get("blocks", 0)
                    return pdu[:5]
                self.holding[addr] = val
                self.log("register", {"addr": hex(addr), "value": val})
                return pdu[:5]

            if fc == 0x10:                       # 写多寄存器（程序下发）
                if len(d) < 5:
                    return err(3)
                start, qty, bc = struct.unpack(">HHB", d[:5])
                if not 1 <= qty <= 123 or bc < qty * 2:
                    return err(3)
                vals = struct.unpack(">" + "H" * qty, d[5:5 + qty * 2])
                if start == HOLD["PROGRAM_BUFFER"]:
                    # 程序缓冲区：每帧都写同一基址，按 TCP 到达顺序追加分块。
                    # 客户端串行写、TCP 保序，COMMIT 时按序拼接为完整程序。
                    self.program_chunks.append(
                        b"".join(struct.pack(">H", v) for v in vals))
                else:
                    for i, v in enumerate(vals):
                        self.holding[start + i] = v
                self.log("multi_register", {"addr": hex(start), "count": qty})
                return bytes([fc]) + struct.pack(">HH", start, qty)

            return err(1)                        # 不支持的功能码

    def _apply_coil(self, addr: int, on: bool) -> None:
        """把线圈动作映射成机床状态变化（模拟 PLC 逻辑）。"""
        if not on:
            return
        if addr == COIL["FEED_HOLD"]:
            self.holding[HOLD["RUN_STATE"]] = 2
        elif addr == COIL["CYCLE_START"]:
            self.holding[HOLD["RUN_STATE"]] = 1
            self.holding[HOLD["ALARM_CODE"]] = 0
        elif addr == COIL["CYCLE_STOP"]:
            self.holding[HOLD["RUN_STATE"]] = 0
        elif addr == COIL["SPINDLE_STOP"]:
            self.holding[HOLD["SPINDLE_STATE"]] = 0
            self.holding[HOLD["SPINDLE_ACT"]] = 0
        elif addr == COIL["RESET"]:
            self.holding[HOLD["RUN_STATE"]] = 0
            self.holding[HOLD["ALARM_CODE"]] = 0
            self.holding[HOLD["BLOCK_NO"]] = 0
        elif addr == COIL["COOLANT_ON"]:
            self.holding[HOLD["COOLANT_STATE"]] = 1
        elif addr == COIL["ALARM_LIGHT"]:
            self.holding[HOLD["ALARM_CODE"]] = 1001

    def store_program(self) -> dict:
        """程序缓冲区收尾：由面板在最后一块写完后调用一次 HTTP 提交。"""
        with self.lock:
            text = b"".join(self.program_chunks).decode(
                "ascii", errors="replace").replace("\x00", "").strip()
            self.program_chunks.clear()
            if not text:
                return {"ok": False, "note": "程序缓冲区为空"}
            path = self.audit_dir / "program_buffer.nc"
            path.write_text(text, encoding="ascii", errors="replace")
            blocks = [l for l in text.splitlines()
                      if l.strip() and not l.strip().startswith("(")]
            self.holding[HOLD["BLOCK_TOTAL"]] = len(blocks)
            self.holding[HOLD["BLOCK_NO"]] = 0
            self.holding[HOLD["PROGRAM_NO"]] = (self.holding[HOLD["PROGRAM_NO"]] + 1) or 1
            self.holding[HOLD["RUN_STATE"]] = 0
            self.log("program", {"bytes": len(text), "blocks": len(blocks),
                                 "sha_hint": text[:24]})
            return {"ok": True, "bytes": len(text), "blocks": len(blocks),
                    "path": str(path), "program_no": self.holding[HOLD["PROGRAM_NO"]]}


STATE: MachineState | None = None


class ModbusHandler(socketserver.BaseRequestHandler):
    def handle(self):
        conn = self.request
        conn.settimeout(5.0)
        while True:
            try:
                header = self._recv_exact(7)
                if header is None:
                    return
                tid, pid, length, unit = struct.unpack(">HHHB", header)
                if pid != 0 or not 2 <= length <= 260:
                    return
                payload = self._recv_exact(length - 1)
                if payload is None or not payload:
                    return
                resp_pdu = STATE.handle(payload)
                resp = struct.pack(">HHHB", tid, 0, len(resp_pdu) + 1, unit) + resp_pdu
                conn.sendall(resp)
            except (OSError, struct.error):
                return

    def _recv_exact(self, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            try:
                chunk = self.request.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf


class ModbusServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    global STATE
    ap = argparse.ArgumentParser(description="虚拟机床 PLC 网关（Modbus TCP 从站）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=15020)
    args = ap.parse_args()
    STATE = MachineState()
    STATE.start()
    srv = ModbusServer((args.host, args.port), ModbusHandler)
    print(f"[虚拟机床] Modbus TCP 从站已启动 {args.host}:{args.port} | "
          f"遥测寄存器 0x1000~0x100F | 控制线圈 0x0100~0x0120 | 程序缓冲 0x0300")
    print("[虚拟机床] 注意：仅用于本地链路验证，不代表真实机床行为")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE.stop()
        srv.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
