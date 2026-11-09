"""阶段 5：下发适配 + 机床日志智能诊断。

下发遵循两条铁律：
  - 只有 RunResult.safe_to_deliver 为真才允许生成"可下发包"；
  - DNC 传输默认 dry-run，真实下发需要显式开关 + 人工确认，防止 AI 直接控制机床。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 报警知识库（可用 LLM 兜底未知报警文本）
# --------------------------------------------------------------------------
ALARM_KB: list[dict[str, Any]] = [
    {"patterns": [r"超程|OVERTRAVEL|OT0?1\d{2}", r"X\+?\s*行程"],
     "cause": "坐标轴越过软/硬限位，常见于工件坐标系偏置错误或程序坐标超程",
     "action": "① 手动反向退出限位 ② 复核 G54 对刀值 ③ 用本项目校验器重跑行程检查 "
               "④ 空运行验证后再上件", "severity": "high"},
    {"patterns": [r"撞刀|碰撞|过切|COLLISION|干涉"],
     "cause": "刀路在材料内快速移动 / 刀具长度补偿错误 / 安全高度不足",
     "action": "① 立即急停并检查刀具与工件 ② 调出仿真报告定位段号 "
               "③ 抬高安全高度或改螺旋下刀 ④ 重新校验+仿真", "severity": "critical"},
    {"patterns": [r"主轴过载|SPINDLE\s*OVERLOAD|ALM\s*7\d{3}|S\s*轴负载"],
     "cause": "切削功率超出主轴额定值：ap/ae 过大、进给过高、刀具磨损或材料硬度偏高",
     "action": "① 降 F 30% 后单段试切 ② 检查刀具磨损 ③ 用 P=ap·ae·Vf·kc/(60·10⁶) "
               "重算功率并校核 ≤80% 额定功率", "severity": "high"},
    {"patterns": [r"伺服报警|SV0?4\d{2}|SERVO", r"跟随误差|FOLLOW"],
     "cause": "伺服跟随误差过大，多因进给突变、加减速设置或机械负载异常",
     "action": "① 检查丝杠/导轨润滑 ② 降低快移倍率 ③ 检查 G00 段是否带过大坐标跳变",
     "severity": "medium"},
    {"patterns": [r"刀具未夹紧|TOOL\s*CLAMP|换刀失败|ATC"],
     "cause": "刀库/主轴松夹刀信号异常，或换刀点 Z 位置不正确",
     "action": "① 检查气压与拉钉 ② 确认换刀点 Z 与第二参考点一致 ③ 重新登记刀具号",
     "severity": "medium"},
    {"patterns": [r"冷却液|COOLANT|M08"],
     "cause": "切削液压力/液位不足，钢件干切会导致刀具急剧磨损",
     "action": "① 补液、检查泵与管路 ② 确认程序 M08 生效 ③ 暂停加工直至冷却恢复",
     "severity": "low"},
    {"patterns": [r"程序(?:语法|格式)|ILLEGAL\s*G|PS0?0\d{2}|非法指令"],
     "cause": "程序段含该数控系统不支持的 G/M 代码或方言不匹配",
     "action": "① 确认后置处理版本（FANUC/SIEMENS/HNC）② 用本项目校验器做方言白名单检查 "
               "③ 重新生成而非手工改程序", "severity": "high"},
    {"patterns": [r"断刀|崩刃|TOOL\s*BREAK", r"振动|振刀|CHATTER"],
     "cause": "刀具伸出过长、悬深过大或切削参数共振",
     "action": "① 缩短刀具悬伸 ② 降低 ap/ae ③ 调整 S 避开共振区 ④ 检查装夹刚性",
     "severity": "high"},
]


@dataclass
class Diagnosis:
    line_no: int
    raw: str
    cause: str
    action: str
    severity: str
    matched_by: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


def diagnose_alarms(log_text: str, llm=None) -> list[Diagnosis]:
    """逐行匹配报警文本；规则库未覆盖的行可交给 LLM 兜底（可选）。"""
    out: list[Diagnosis] = []
    for no, raw in enumerate(log_text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        hit = None
        for item in ALARM_KB:
            if any(re.search(p, line, re.IGNORECASE) for p in item["patterns"]):
                hit = item
                break
        if hit:
            out.append(Diagnosis(no, line, hit["cause"], hit["action"],
                                 hit["severity"]))
        elif llm is not None:
            out.append(Diagnosis(no, line, "规则库未覆盖，需结合机型手册判断",
                                 "查阅该型号报警手册并联系设备厂商", "unknown",
                                 matched_by="llm_fallback"))
    return out


def alarm_summary(diagnoses: list[Diagnosis]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for d in diagnoses:
        counts[d.severity] = counts.get(d.severity, 0) + 1
    return {"total": len(diagnoses), "by_severity": counts,
            "critical": [d.raw for d in diagnoses if d.severity == "critical"]}


# --------------------------------------------------------------------------
# 下发适配
# --------------------------------------------------------------------------
@dataclass
class DeliveryPackage:
    approved: bool
    controller: str
    gcode_path: str
    sha256: str
    checklist: list[str] = field(default_factory=list)
    created_at: str = ""
    method: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


def build_delivery(result, output_dir: Path,
                   method: str = "usb") -> DeliveryPackage:
    """method: usb(拷贝目录) | dnc(生成待发送包，默认不发)。"""
    method = method.lower()
    if not result.safe_to_deliver:
        return DeliveryPackage(
            approved=False, controller=result.controller, gcode_path="",
            sha256="", created_at=datetime.now().isoformat(timespec="seconds"),
            method=method,
            note="拒绝下发：静态校验或体素仿真未通过，或缺少人工复核。",
            checklist=["先修复报告中的拦截项，重新跑校验+仿真"])

    src = Path(result.output_files.get("gcode", ""))
    text = src.read_text(encoding="ascii", errors="replace") if src.exists() else \
        (result.program.text if result.program else "")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    checklist = [
        "核对程序指纹 SHA-256 与报告一致（防篡改）",
        "核对刀库实际刀具直径/刃长/刀号与工序卡一致",
        "确认 G54 工件坐标系、Z0 顶面设定已对刀",
        "确认压板/虎钳位置避开仿真刀路",
        "首件单段运行 + 空运行 + 低倍率试切",
        f"确认控制器为 {result.controller} 后置处理版本",
    ]
    if method == "usb":
        target_dir = output_dir / "usb_delivery"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"O{result.program.program_number:04d}.NC"
        target.write_text(text, encoding="ascii", errors="replace")
        (target_dir / "CHECKSUM.txt").write_text(
            f"SHA256 {digest}\nCTRL {result.controller}\n"
            f"GENERATED {datetime.now():%Y-%m-%d %H:%M:%S}\n", encoding="ascii")
        note = (f"已生成 U 盘拷贝目录 {target_dir}；请人工复核清单签字后拷贝至机床。"
                "本系统不代替操作员确认。")
    elif method == "dnc":
        packet = output_dir / "dnc_packet.json"
        packet.write_text(json.dumps({
            "controller": result.controller,
            "program": f"O{result.program.program_number:04d}",
            "sha256": digest,
            "blocks": result.program.block_count,
            "requires_human_confirmation": True,
            "note": "DNC 传输为 dry-run：仅生成待发送包，不建立到机床的 socket 连接",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        note = (f"已生成 DNC 待发送包 {packet}（dry-run，未连接机床）。"
                "真实下发需配置机床 IP/端口并由操作员二次确认。")
    else:
        raise ValueError(f"未知下发方式 {method}，可选 usb / dnc")

    return DeliveryPackage(
        approved=True, controller=result.controller, gcode_path=str(src),
        sha256=digest, checklist=checklist,
        created_at=datetime.now().isoformat(timespec="seconds"),
        method=method, note=note)


SAMPLE_ALARM_LOG = """\
2026-11-07 08:31:02 [INFO] 程序 O0001 启动，G54 已激活
2026-11-07 08:31:40 [ALARM] X+ 超程报警 OT0101，坐标轴越过软限位
2026-11-07 08:32:11 [ALARM] 主轴过载 ALM 7002，负载率 118%
2026-11-07 08:33:05 [WARN] 冷却液压力低，M08 已输出但压力开关未闭合
2026-11-07 08:34:19 [ALARM] 撞刀风险：G00 在材料内快速移动
2026-11-07 08:35:00 [ALARM] PS0010 非法 G 指令，程序段 N210
2026-11-07 08:36:22 [INFO] 操作员急停，进给倍率归零
"""
