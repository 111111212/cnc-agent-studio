"""机型库与能力派生：可换机型 + 按机型生成其应有功能。

收录 2026 年前后主流立式加工中心 / 钻攻中心 / 五轴机型的**典型配置**。
规格为教学演示用示例值，正式投产必须以机床铭牌、参数表与厂商后置手册为准
（报告中会明确标注，避免把示例参数当成实测数据）。

核心机制：
  MachineProfile -> MachineSpec（物理极限，驱动夹紧/校验/仿真）
                -> CapabilitySet（该机型"应该有的功能"及可用性理由）
                -> 后处理方言 + 工业协议 + 视觉接入方案
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .models import MachineSpec


@dataclass(frozen=True)
class MachineProfile:
    id: str
    brand: str
    model: str
    category: str            # VMC 立式加工中心 | 钻攻中心 | 五轴 | 雕铣 | 经济型
    controller: str          # 具体数控系统型号
    post: str                # FANUC | SIEMENS | HNC（本项目实现的三种方言）
    fidelity: str            # native | iso_compatible
    spec: MachineSpec
    spindle_taper: str = "BT40"
    tool_count: int = 24
    rapid_m_min: float = 36.0
    protocols: tuple[str, ...] = ()
    telemetry: tuple[str, ...] = ()
    vision_support: str = "外挂工业相机（机床本体无视觉接口）"
    options: tuple[str, ...] = ()
    notes: str = ""

    @property
    def display(self) -> str:
        return f"{self.brand} {self.model}"

    @property
    def controller_key(self) -> str:
        return self.post.upper()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["display"] = self.display
        return d


def _spec(name, x, y, z, spindle, feed, power, rapid, controller="FANUC", tools=24):
    return MachineSpec(
        name=name, x_min=-x / 2, x_max=x / 2,
        y_min=-y / 2, y_max=y / 2,
        z_min=-z, z_max=50,
        max_spindle=spindle, min_spindle=200,
        max_feed=feed, max_rapid=rapid,
        power_kw=power, tool_count=tools, controller=controller)


# --------------------------------------------------------------------------
# 机型库（典型配置，示例值）
# --------------------------------------------------------------------------
PROFILES: dict[str, MachineProfile] = {
    "vmc850_fanuc": MachineProfile(
        "vmc850_fanuc", "通用", "VMC-850 / FANUC 0i-MF", "VMC 立式加工中心",
        "FANUC 0i-MF Plus", "FANUC", "native",
        _spec("VMC-850 / 0i-MF", 850, 500, 500, 8000, 6000, 7.5, 20000),
        "BT40", 24, 36.0,
        ("FOCAS", "Modbus TCP", "Ethernet DNC"),
        ("主轴负载", "报警历史", "机械坐标", "进给倍率", "刀具寿命"),
        options=("Macro B", "G84.2 高速刚性攻丝", "G68 坐标旋转", "刀具寿命管理"),
        notes="国内保有量最大的通用三轴配置，抗干扰好，适合本项目 2.5D 铣削基线。"),

    "robodrill_d21": MachineProfile(
        "robodrill_d21", "FANUC", "ROBODRILL α-D21MiB Plus", "钻攻中心",
        "FANUC 31i-B5 Plus", "FANUC", "native",
        _spec("ROBODRILL α-D21MiB Plus", 500, 400, 330, 24000, 20000, 11.0, 54000, "FANUC", 21),
        "BT30", 21, 54.0,
        ("FOCAS", "Ethernet DNC", "Modbus TCP"),
        ("主轴负载", "报警历史", "热位移补偿状态", "换刀次数"),
        options=("高速刚性攻丝 G84.2", "AI 轮廓控制", "智能热位移补偿", "4 轴 DDR 转台"),
        notes="3C/新能源壳体批量加工主力，24000rpm + 0.7s 换刀；BT30 刚性有限，ap/ae 需更保守。"),

    "siemens_828d_vmc": MachineProfile(
        "siemens_828d_vmc", "通用", "VMC-850 / SINUMERIK 828D", "VMC 立式加工中心",
        "SINUMERIK 828D", "SIEMENS", "native",
        _spec("VMC-850 / 828D", 850, 500, 500, 12000, 10000, 9.0, 30000),
        "BT40", 24, 48.0,
        ("OPC UA", "PROFINET", "Ethernet DNC"),
        ("主轴负载", "报警日志", "轴电流", "程序状态"),
        options=("ProgramGUIDE", "CYCLE81/83 钻孔循环", "DXF 轮廓读入", "ShopMill"),
        notes="828D 用 G71 公制、T/D 刀补、CYCLE 循环；Profinet/OPC UA 便于产线集成。"),

    "siemens_840d_dmu50": MachineProfile(
        "siemens_840d_dmu50", "DMG MORI", "DMU 50 (3+2 / 5轴)", "五轴加工中心",
        "SINUMERIK 840D sl", "SIEMENS", "native",
        _spec("DMU 50 / 840D sl", 650, 520, 475, 15000, 12000, 13.0, 42000),
        "HSK-A63", 30, 42.0,
        ("OPC UA", "PROFINET"),
        ("主轴负载", "报警日志", "RTCP 状态", "各轴跟随误差"),
        options=("TRAORI 刀尖跟随", "CYCLE800 摆头循环", "3+2 定向加工", "五轴联动"),
        notes="⚠ 本项目仅实现三轴 2.5D；该机型的五轴/RTCP 能力会被显式禁用，"
              "复杂曲面需专业 CAM 与人工工艺评审。"),

    "hnc_848d_tv856": MachineProfile(
        "hnc_848d_tv856", "创世纪/台群", "T-V856S / HNC-848D", "VMC 立式加工中心",
        "华中数控 HNC-848D", "HNC", "native",
        _spec("T-V856S / HNC-848D", 850, 560, 550, 12000, 10000, 11.0, 36000),
        "BT40", 24, 48.0,
        ("NCUC-Bus", "OPC UA", "Modbus TCP", "Ethernet DNC"),
        ("主轴负载", "报警日志", "坐标", "进给倍率"),
        options=("国产五轴 RTCP", "宏变量", "G81/G83 固定循环", "总线式伺服"),
        notes="国产化替代主力机型；HNC-8 采用 FANUC 兼容方言，差异在程序头与回零段。"),

    "mitsubishi_m80_t500": MachineProfile(
        "mitsubishi_m80_t500", "台群精机", "T-500S / 三菱 M80VA", "钻铣加工中心",
        "MITSUBISHI M80VA", "FANUC", "iso_compatible",
        _spec("T-500S / M80VA", 500, 400, 330, 24000, 20000, 7.5, 48000, "FANUC", 21),
        "BT30", 21, 48.0,
        ("Ethernet DNC", "Modbus TCP"),
        ("主轴负载", "报警日志"),
        options=("高速攻牙", "直联主轴", "简易对话式编程"),
        notes="M80 为 ISO 兼容方言；本项目走 FANUC 后置，正式使用需换厂商后置复核。"),

    "brother_s700": MachineProfile(
        "brother_s700", "Brother", "Speedio S700X1", "钻攻中心",
        "Brother CNC-B00", "FANUC", "iso_compatible",
        _spec("Speedio S700X1", 700, 400, 300, 16000, 20000, 9.0, 50000, "FANUC", 28),
        "BT30", 28, 50.0,
        ("Ethernet DNC", "Modbus TCP"),
        ("主轴负载", "报警日志"),
        options=("高速同步攻丝", "28 刀高速换刀", "低惯量高速主轴"),
        notes="BT30 钻攻中心，适合铝件高速加工；后置需按 Brother 手册核对 M 代码。"),

    "haas_vf2": MachineProfile(
        "haas_vf2", "Haas", "VF-2 / Haas Control", "VMC 立式加工中心",
        "Haas NGC", "FANUC", "iso_compatible",
        _spec("Haas VF-2", 762, 406, 508, 8100, 10000, 11.2, 25400, "FANUC", 20),
        "BT40", 20, 25.4,
        ("Ethernet DNC", "Haas Q Commands", "MTConnect"),
        ("主轴负载", "报警日志", "宏变量", "进给倍率"),
        options=("宏变量 #", "M88/M89 通过主轴冷却", "G154 附加工件坐标系"),
        notes="Haas 使用 ISO 兼容代码但 M 代码差异较大；项目内按 FANUC 近似输出并标注。"),

    "mazak_vcn530": MachineProfile(
        "mazak_vcn530", "Mazak", "VCN-530C / SmoothX", "VMC 立式加工中心",
        "MAZATROL SmoothX", "FANUC", "iso_compatible",
        _spec("VCN-530C", 1050, 530, 510, 12000, 12000, 15.0, 42000),
        "BT40", 30, 42.0,
        ("Ethernet DNC", "MTConnect", "OPC UA"),
        ("主轴负载", "报警日志", "程序状态", "热补偿"),
        options=("Mazatrol 会话式编程", "EIA/ISO 双格式", "Smooth 热补偿"),
        notes="Mazatrol 与 EIA 双格式；本项目只输出 EIA/ISO 程序，Mazatrol 会话程序不支持。"),

    "jingdiao_jdgr200": MachineProfile(
        "jingdiao_jdgr200", "北京精雕", "JDGR200 高速雕铣中心", "雕铣中心",
        "精雕 JD50 数控系统", "FANUC", "iso_compatible",
        _spec("JDGR200", 400, 300, 250, 32000, 12000, 5.5, 30000, "FANUC", 16),
        "HSK-E32", 16, 30.0,
        ("Ethernet DNC", "Modbus TCP"),
        ("主轴负载", "报警日志"),
        options=("小刀具高速精加工", "在机检测", "精雕特色宏"),
        notes="小行程高转速，适合精密小件；本项目 ae/ap 会按刀具与材料自动收紧。"),

    "gsk_980mdi": MachineProfile(
        "gsk_980mdi", "广数", "GSK 980MDi 经济型铣床", "经济型",
        "GSK 980MDi", "FANUC", "iso_compatible",
        _spec("GSK 980MDi", 600, 400, 400, 6000, 4000, 5.5, 15000, "FANUC", 16),
        "BT40", 16, 24.0,
        ("RS232 DNC", "Modbus TCP"),
        ("报警日志",),
        options=("基础 ISO G 代码", "简单固定循环"),
        notes="经济型配置：无 FOCAS/OPC UA，视觉与告警依赖外挂采集；RS232 DNC 需串口服务器。"),
}

DEFAULT_PROFILE = "vmc850_fanuc"


def get_profile(machine_id: str | None = None) -> MachineProfile:
    key = (machine_id or DEFAULT_PROFILE).lower().strip()
    if key in PROFILES:
        return PROFILES[key]
    for k, p in PROFILES.items():
        if key in (p.display.lower(), p.model.lower(), p.controller.lower(),
                   p.brand.lower()):
            return p
    raise ValueError(f"未知机型 {machine_id!r}，可用：{', '.join(PROFILES)}")


def list_profiles() -> list[MachineProfile]:
    return list(PROFILES.values())


# --------------------------------------------------------------------------
# 能力派生：根据机型生成"其应有功能"及可用性说明
# --------------------------------------------------------------------------
@dataclass
class Capability:
    name: str
    group: str
    status: str          # native | derived | option | disabled | external
    reason: str
    code_hint: str = ""


def derive_capabilities(p: MachineProfile, scope: str = "2.5D 三轴铣削") -> list[Capability]:
    """按控制器家族 + 机型类别 + 通信协议，派生该机型应有功能清单。

    status 说明：
      native   本项目已实现/原生支持
      derived  按机型规格自动启用的参数与策略
      option   机床支持但需选配/厂商后置
      disabled 超出本项目范围，显式禁用（不假装支持）
      external 需要外部硬件/协议接入
    """
    caps: list[Capability] = []
    add = caps.append

    # --- 1. 所有机型共有的基础能力 ---
    add(Capability("ISO G/M 代码执行", "编程", "native", "后处理器逐行生成，静态校验器白名单复核"))
    add(Capability("工件坐标系 G54~G59", "编程", "native", "程序头显式指定并校验"))
    add(Capability("G43 刀具长度补偿", "编程", "native", f"{p.post} 方言输出，校验器检查 H 号"))
    add(Capability("G81/G83 固定循环", "编程", "native",
                   "SIEMENS 用 CYCLE81，其余用 G81/G80"))
    add(Capability("G41/G42 半径补偿", "编程", "option",
                   "本项目采用 CAM 计算刀位点轨迹，不下发 G41，避免二次偏移"))
    add(Capability("参数物理夹紧", "安全", "native",
                   f"S≤{p.spec.max_spindle}rpm / F≤{p.spec.max_feed}mm/min / "
                   f"P≤{p.spec.power_kw*0.8:.1f}kW"))
    add(Capability("行程软限位校验", "安全", "native",
                   f"X±{p.spec.x_max:.0f} Y±{p.spec.y_max:.0f} Z[{p.spec.z_min:.0f},{p.spec.z_max:.0f}]"))
    add(Capability("体素材料去除仿真", "安全", "native",
                   "按该机型行程与毛坯尺寸建立 heightmap"))

    # --- 2. 按控制器家族派生 ---
    fam = p.post.upper()
    if fam == "FANUC":
        add(Capability("Macro B 宏变量", "数控系统", "option",
                       "FANUC 系支持；本项目不生成宏程序，避免机型差异"))
        add(Capability("G84.2 高速刚性攻丝", "数控系统", "option",
                       "钻攻/大批量攻丝场景需要，本项目未生成攻丝循环"))
        add(Capability("FOCAS 数据采集", "通信", "native" if "FOCAS" in p.protocols else "external",
                       "读取主轴负载/报警/机械坐标，供视觉闭环做工况佐证"))
    elif fam == "SIEMENS":
        add(Capability("G71 公制 + T/D 刀补", "数控系统", "native",
                       "SiemensPost 已实现，校验器做 SIEMENS 方言白名单"))
        add(Capability("CYCLE81 / CYCLE83", "数控系统", "native",
                       "SiemensPost 输出 CYCLE81，仿真器展开为钻孔轨迹"))
        add(Capability("ProgramGUIDE / ShopMill", "数控系统", "option",
                       "会话式编程界面，本项目只输出 DIN/ISO 程序"))
    elif fam == "HNC":
        add(Capability("HNC-8 FANUC 兼容方言", "数控系统", "native",
                       "程序名 %0001、G98 返回初始平面、G91 G28 Z0 回零"))
        add(Capability("NCUC-Bus 总线伺服", "通信", "native" if "NCUC-Bus" in p.protocols else "external",
                       "国产总线，用于轴状态采集"))

    if "五轴" in p.category or "3+2" in p.category:
        add(Capability("五轴联动 / RTCP / TRAORI", "运动", "disabled",
                       "⚠ 超出本项目 2.5D 三轴范围，须专业 CAM 与人工工艺评审"))
        add(Capability("3+2 定向加工", "运动", "disabled",
                       "⚠ 未实现摆头坐标变换，禁止在本项目输出"))
    else:
        add(Capability("三轴直线/圆弧插补", "运动", "native",
                       "G00/G01/G02/G03，含整圆 I/J 输出"))

    if p.category == "钻攻中心":
        add(Capability("高速钻攻策略", "工艺", "derived",
                       f"主轴 {p.spec.max_spindle}rpm、快移 {p.rapid_m_min:g}m/min，"
                       "自适应减小 ap/ae 以匹配 BT30 刚性"))
        add(Capability("大容量刀库少换刀", "工艺", "derived",
                       f"刀库 {p.tool_count} 把，工序按刀号聚合减少换刀"))
    if p.spindle_taper.startswith("HSK"):
        add(Capability(f"{p.spindle_taper} 高速刀柄", "工艺", "derived",
                       "高转速小刀具场景，参数按小直径刀具自动换算"))

    if p.fidelity == "iso_compatible":
        add(Capability(f"{p.controller} 原生后置", "编程", "option",
                       "本项目按 FANUC ISO 兼容方言近似输出，投产前必须换厂商后置并复核 M 代码"))

    # --- 3. 通信 / 视觉 / 控制下发 ---
    for proto in p.protocols:
        add(Capability(f"{proto} 接入", "通信",
                       "native" if proto in ("Modbus TCP", "OPC UA", "Ethernet DNC",
                                            "FOCAS", "PROFINET", "MTConnect")
                       else "external",
                       "用于日志采集与控制信号下发（默认 dry-run）"))
    add(Capability("机器视觉实时监控", "视觉", "external", p.vision_support))
    add(Capability("YOLO 故障检测", "视觉", "native",
                   "可插拔 YOLO 权重；未提供权重时用启发式像素检测器离线演示"))
    add(Capability("视觉事件回传 Agent 诊断", "视觉", "native",
                   "截图 + 检测框作为工具输入，输出原因与处置方案"))
    add(Capability("工业协议控制下发", "控制", "external",
                   "进给保持/循环停止/倍率调整需经安全 PLC 互锁；急停不由 AI 直连"))
    add(Capability("遥测日志采集", "监控", "native" if p.telemetry else "external",
                   "、".join(p.telemetry) if p.telemetry else "该机型无开放遥测接口"))
    add(Capability("工艺参数反哺重编程", "闭环", "native",
                   "视觉故障 -> 调整 ap/ae/F -> 重新夹紧+校验+仿真 -> 修订程序"))

    for opt in p.options:
        add(Capability(opt, "选配", "option", "机床支持；本项目不自动生成相关代码"))
    return caps


def capability_summary(p: MachineProfile) -> dict[str, Any]:
    caps = derive_capabilities(p)
    by_status: dict[str, int] = {}
    for c in caps:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    return {
        "profile": p.to_dict(),
        "total": len(caps),
        "by_status": by_status,
        "capabilities": [asdict(c) for c in caps],
        "recommended_vision_protocol": next(
            (x for x in p.protocols if x in ("OPC UA", "FOCAS", "Modbus TCP")),
            "Modbus TCP（外挂网关）"),
        "disclaimer": "机型规格为典型示例配置，正式投产以机床铭牌/参数表/厂商后置手册为准。",
    }


def effective_cutting_policy(p: MachineProfile) -> dict[str, Any]:
    """机型专属工艺策略：钻攻中心/小锥度刀柄更保守，高刚性 VMC 可放开。"""
    if p.category == "钻攻中心":
        return {"ap_factor": 0.7, "ae_factor": 0.75, "feed_factor": 0.9,
                "reason": "BT30/小直径刀具刚性有限，降低切深与行距防崩刀"}
    if p.category == "雕铣中心":
        return {"ap_factor": 0.5, "ae_factor": 0.6, "feed_factor": 0.85,
                "reason": "小刀具高速精加工，切深与行距按刀具直径收紧"}
    if "五轴" in p.category:
        return {"ap_factor": 0.8, "ae_factor": 0.8, "feed_factor": 0.9,
                "reason": "仅按三轴模式使用，五轴能力禁用"}
    return {"ap_factor": 1.0, "ae_factor": 1.0, "feed_factor": 1.0,
            "reason": "标准 BT40 立式加工中心，按材料许用值取参"}


def export_catalog(path: Path) -> Path:
    data = {k: capability_summary(p) for k, p in PROFILES.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
