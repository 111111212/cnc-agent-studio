# CNC Agent — 加工智能编程 + 视觉监测 + 故障诊断 闭环系统

> 仓库名：`cnc-agent-studio` · CNC 智能工艺规划与视觉闭环工作台

> 自然语言 → 工艺规划 → 确定性 G 代码 → 静态校验 → 体素仿真 → 下发准入
> → **机器视觉实时监测 → Agent 故障诊断 → 工业协议控制 → 反哺工艺重编程**
>
> 全程可换机型（11 种主流加工中心/钻攻中心），无需 API Key、无需摄像头、无需 YOLO 权重即可离线跑通。

---

## 零、密钥私密策略（公开仓库必读）

- 真实密钥**只放本机 `.env`**（已被 `.gitignore` 忽略，绝不入库）；仓库里只有 `.env.example`，其中所有 KEY / TOKEN 项均为空值。
- 代码只从环境变量读取 `LLM_API_KEY` / `PANEL_TOKEN`，以 `Authorization` 请求头发送；日志、报告、面板响应一律用 `masked_key()` 打码（前缀 3 + 后缀 2 + 长度），不回显明文。
- **提交前自动闸门**：仓库自带 `scripts/check_secrets.py` 与 `.githooks/pre-commit`。每个 clone 执行一次 `git config core.hooksPath .githooks` 即启用；暂存区出现疑似真实凭据（或 `.env`、私钥、凭据文件）会直接拒绝提交。手动全量扫：`python scripts/check_secrets.py`。
- 密钥一旦被提交过，删文件无效：**先去平台吊销重发**，再清理 Git 历史。

---

## 一、对原始方案的架构优化（最重要的改动）

原方案让大模型**直接生成 G 代码**，再用校验器兜底。这里改成了：

| | 原方案 | 本项目 |
|---|---|---|
| LLM 输出 | G 代码文本 | **结构化工艺方案 JSON**（选刀/工序/S/F/ap/ae） |
| G 代码来源 | 模型逐行写 | **确定性后处理器**逐行生成 |
| 幻觉影响面 | 语法、行程、模态、圆弧全可能被污染 | 只能污染**参数**，且被物理夹紧层拦截 |
| 安全关卡 | 校验 + 仿真 | **参数夹紧 + 校验 + 仿真 + 人工复核** 四层 |
| 仿真输入 | 刀路对象 | **校验后的 G 代码文本**（模态状态机复原），与下发文件同一份 |
| 失败策略 | 返回模型重生成 | 有工程依据的确定性修复；轮次用尽 **fail-closed 拒绝下发** |

一句话：**大模型不懂机床电机，那就别让它碰电机能读的字符**。

---

## 二、系统链路

```
① 输入层     自然语言 / DXF 工程图 / 位图视觉模型 / STEP包围盒 / 旧G代码体检
② 工艺规划   RulePlanner(离线) 或 LLMPlanner(多模态可切) → 结构化方案
③ 参数夹紧   机床极限 / 材料许用 / 主轴功率三重夹紧，越界回退推荐值
④ 后处理     FANUC / SIEMENS 840D / HNC 三种方言 + 机型专属工艺策略
⑤ 静态校验   词法解析 + 模态状态机 + 行程/方言/补偿/危险指令检查
⑥ 体素仿真   281×281 heightmap，撞刀/过切/欠切/切穿/夹具干涉/体积守恒
⑦ 自修复     校验或仿真失败 → 证据回传 → 参数调整 → 重新生成（最多 N 轮）
⑧ 下发准入   双关卡通过才落 .nc；生成 SHA-256 指纹与人工复核清单

──────────────── 加工过程闭环 ────────────────
⑨ 视觉监测   摄像头/RTSP/视频/合成帧 → YOLO 或像素启发式 → 多帧去抖
⑩ Agent诊断   【截图 + 检测框 + 工况上下文】作为工具输入 → 根因/处置/整改系数
⑪ 控制下发   SafetyGovernor 白名单门控 → Modbus TCP / OPC UA / MQTT（默认 dry-run）
⑫ 工艺反哺   最保守整改系数 → 重新夹紧/后处理/校验/仿真 → 修订程序
```

---

## 三、快速开始

```bash
# 环境：Python 3.10+，仅依赖 numpy（可选 opencv-python / ultralytics / pymodbus）
pip install -r requirements.txt

# ① 跑通编程全链路（内置示例零件，无需任何密钥/硬件）
python main.py demo

# ② 主动注入幻觉，证明安全屏障有效
python main.py demo --hallucination

# ③ 视觉监控独立演示（145 帧合成工况，6 类故障）
python main.py monitor

# ④ 完整闭环：编程 → 监测 → 诊断 → 控制 → 反哺重编程
python main.py closed-loop --machine hnc_848d_tv856

# ⑤ 查看机型目录 / 某机型派生出的全部功能
python main.py machines
python main.py machines --id robodrill_d21
python main.py machines --export

# ⑥ 其它
python main.py controllers                       # 三种系统方言对比
python main.py run --file examples/part.json --machine siemens_828d_vmc
python main.py legacy --file examples/legacy.nc  # 旧程序 AI 体检
python main.py alarms --file examples/alarms.log # 机床报警日志诊断
python main.py prompt                            # 导出核心 System Prompt
python main.py drawing-schema                    # 导出图纸 OCR 抽取模板
python main.py from-drawing                      # 内置示例工程图 -> G 代码全链路
python main.py from-drawing --file 图纸.dxf --machine robodrill_d21 --deliver usb
python main.py from-drawing --analyze-only       # 只识别图纸、导出 SVG/JSON，不生成程序
python -m unittest discover -s tests -v          # 56 项回归测试（含 15 项安全/正确性加固 + 7 项密钥私密回归）
python scripts/check_secrets.py                 # 全仓密钥扫描（提交前自动跑）
git config core.hooksPath .githooks            # 启用提交前密钥闸门
```

**接入真实大模型**：复制 `.env.example` 为 `.env`，填 `LLM_API_KEY`（支持任意 OpenAI 兼容端点）。
留空则自动使用确定性规则规划器，链路照样完整跑通。密钥只在本机，不进仓库。
视觉诊断走多模态消息（截图 base64 + 结构化检测结果），模型不可用时自动回退规则知识库。

---

## 四、可换机型：11 种主流配置

`--machine <id>` 或 `.env` 的 `CNC_MACHINE` 切换。机型决定：
**行程/转速/进给/功率极限 → 参数夹紧与校验**、**后置方言**、**专属工艺策略**、
**通信协议与遥测能力**、**按机型派生的功能清单**。

| ID | 机型 | 数控系统 | 后置 | 特点 |
|---|---|---|---|---|
| `vmc850_fanuc` | VMC-850 | FANUC 0i-MF Plus | FANUC | 通用基线，FOCAS 采集 |
| `robodrill_d21` | FANUC ROBODRILL α-D21MiB Plus | 31i-B5 Plus | FANUC | 24000rpm 钻攻中心，BT30 自动收紧 ap/ae |
| `siemens_828d_vmc` | VMC-850 | SINUMERIK 828D | SIEMENS | G71 / T-D / CYCLE81 / OPC UA |
| `siemens_840d_dmu50` | DMG MORI DMU 50 | 840D sl | SIEMENS | 五轴能力**显式禁用**（仅按三轴用） |
| `hnc_848d_tv856` | 创世纪 T-V856S | 华中 HNC-848D | HNC | 国产化主力，NCUC-Bus |
| `mitsubishi_m80_t500` | 台群 T-500S | 三菱 M80VA | FANUC* | ISO 兼容，需厂商后置复核 |
| `brother_s700` | Brother Speedio S700X1 | CNC-B00 | FANUC* | 28 刀高速钻攻 |
| `haas_vf2` | Haas VF-2 | Haas NGC | FANUC* | M 代码差异需核对 |
| `mazak_vcn530` | Mazak VCN-530C | MAZATROL SmoothX | FANUC* | 仅输出 EIA/ISO，不支持 Mazatrol |
| `jingdiao_jdgr200` | 北京精雕 JDGR200 | 精雕 JD50 | FANUC* | 32000rpm 雕铣，小刀具策略 |
| `gsk_980mdi` | 广数 GSK 980MDi | GSK 980MDi | FANUC* | 经济型，RS232 DNC |

`*` = `iso_compatible`：按 FANUC ISO 兼容方言近似输出，程序头会写 `NOTICE: VERIFY M-CODES WITH MACHINE BUILDER MANUAL`，投产前必须换厂商后置。

**"根据机型生成其应有功能"**由 `derive_capabilities()` 完成，每项功能带状态和理由，
分 `native`（已实现）/ `derived`（按机型规格自动启用）/ `option`（需选配或厂商后置）/
`disabled`（超范围，显式禁用，不假装支持）/ `external`（需外部硬件/协议）。
例如钻攻中心自动派生「高速钻攻策略」「大容量刀库少换刀」，DMU 50 显式禁用「五轴联动/RTCP/3+2」。
能力清单输出到 `output/capabilities.json`，全机型目录 `output/machine_catalog.json`，并展示在闭环报告中。

> 机型规格为**典型示例配置**，正式投产以机床铭牌、参数表与厂商后置手册为准。

---

## 五、机器视觉实时监控子模块

| 能力 | 实现 |
|---|---|
| 帧源 | `CameraSource`（本机摄像头/RTSP，opencv）/ `VideoFileSource` / `ImageSequenceSource` / `SyntheticSource`（离线合成工况） |
| 检测 | `YOLODetector`（ultralytics，加载自训练 `.pt` 权重，带类别名映射）/ `HeuristicDetector`（颜色·亮度·几何统计，无依赖） |
| 故障类别 | 异常火花、切屑堆积、刀具破损、工件位移、冷却液缺失、碎片飞溅（疑似碰撞） |
| 去抖 | 时间窗 6 帧内 ≥3 帧命中 + 置信度均值 ≥0.55 + 同类冷却 14 帧，抑制单帧误报 |
| 留存 | 带检测框截图 `output/vision/snapshots/*.png` + JSONL 日志 `events.jsonl` / `closed_loop.jsonl` / `control_audit.jsonl` |
| 告警 | 控制台分级 + 可配置 Webhook（企业微信/钉钉机器人）+ 冷却去重 |
| Agent 工具 | `analyze_machining_fault(snapshot_base64, detections, process_context)`，function-calling 契约 |
| 诊断 | 多模态 LLM 输出 `{cause, reasoning, immediate_actions, control_signals, process_adjustments}`，规则知识库兜底 |
| 控制 | `SafetyGovernor` 白名单 → Modbus TCP / OPC UA / MQTT 适配器（默认 dry-run） |

真实 YOLO 接入：
```bash
python main.py closed-loop --frames camera --device 0 --weights runs/train/weights/best.pt
python main.py closed-loop --frames video --video data/line.mp4 --weights best.pt
```
权重未提供/库不可用时自动回退启发式后端，并在报告中标注 backend 与回退原因，**绝不假装用了 YOLO**。
COCO 权重不适用本项目，需用机加工故障数据集自训练（类别映射见 `detector.py` 的 `_normalize`）。

### 三层安全边界
1. 编程侧：校验 + 仿真抵消大模型幻觉；
2. 视觉侧：连续多帧去抖抑制误报，整改系数只允许 ≤1.0（禁止借诊断提速）；
3. 控制侧：信号白名单、值域夹紧（`FEED_OVERRIDE` 上限 100）、人工确认门控；
   **急停没有软写地址，必须由硬件安全 PLC/急停按钮执行，适配器会拒绝任何软写。**

---

## 六、实测数据（本机跑出，非设计说明）

| 项目 | 结果 |
|---|---|
| 端到端编程链路 | 70 段 G 代码，静态校验 0 错误，仿真去除体积误差 **0.71%**、欠切 **0.67%**、碰撞 **0** |
| 三种方言 | FANUC / SIEMENS / HNC 校验与仿真全 PASS，体积误差一致 0.71% |
| 幻觉注入演示 | 参数层夹紧 **6 项**（S30000/F9000/ap25 → 回退推荐值），代码层校验拦截 **6 项**；第 2 轮重生成后双关卡 PASS |
| 视觉监控 | **145 帧 / 6 起事件 / 6 类故障各 1 次**，零误报零漏报，全部落在预设场景帧内 |
| 完整闭环 | 18 条控制信号批准、1 条急停被拦截；修订程序双关卡 PASS，程序 SHA 变化证明确实重编程 |
| 回归测试 | `56 tests, OK`，耗时约 4.5 s；面板自检 57/57、前端无头 12/12 |

---

## 七、核心实现要点

- **参数夹紧可重入**：`reclamp_operation()` 让 LLM 输出、幻觉注入、修复动作产生的参数在任何一轮都强制过夹紧；越界视为不可信，**回退到公式推荐值**而非贴着机床极限（合法 ≠ 合理）。
- **模态状态机**：后处理器会省略未变化的坐标，校验器与仿真器都用 G00/G01/G02/G03 + G90/G91 模态复原真实轨迹；整圆 G02 强制输出 X/Y，否则会被解析器跳过（这是实测踩过的坑）。
- **真实几何刀路**：分层等距环切、螺旋/斜线下刀、G02 整圆、G03 圆角矩形轮廓、往复行切、G81 固定循环；精加工走满所有环以免型腔中心留台阶。
- **体素仿真吃 G 代码文本**：不是理想刀路对象，保证"仿真跑的"和"下发机床的"是同一个文件。
- **方言白名单**：FANUC 不认 CYCLE/G71/G74；SINUMERIK 不认 O 程序号/G43 H/G91 G28，交叉即报错。
- **工艺铁律**：面铣建基准面永远最先；同特征先粗后精；同刀号聚合减少换刀。
- **下发 fail-closed**：修复轮次用尽仍未通过时，只写 `.blocked.txt`，绝不产出可下发 `.nc`。

---

## 八、面试讲法（精简版）

> CNC 接入大模型的核心矛盾是：大模型不懂机床电机，机床看不懂自然语言，中间翻译载体是 G 代码。
> 我的做法不是让模型写 G 代码——那会把幻觉带进机床级指令，而是让模型只输出结构化工艺方案，
> G 代码由确定性后处理器生成；参数经过机床极限、材料许用、主轴功率三重夹紧。
> 之后静态校验器用词法解析加模态状态机复原真实轨迹，检查方言、行程、补偿和危险指令；
> 体素仿真再从校验后的 G 代码文本复原刀路，检查撞刀、过切、欠切和体积守恒。
> 任一道关卡失败，把机器检出的客观证据回传给 Agent 做有工程依据的修复，轮次用尽就拒绝下发。
> 加工过程再加视觉闭环：摄像头接 YOLO，截图和检测框作为工具输入交给多模态 Agent 分析根因，
> 处置建议经白名单和人工确认门控后通过 Modbus/OPC UA 下发，急停永远走硬件安全回路；
> 整改系数反哺工艺方案，重新夹紧、校验、仿真后生成修订程序。
> 整套系统支持 11 种主流机型切换，按机型派生行程、方言、工艺策略与通信能力。
>
> **必须主动说的三条限制**：① AI 程序不能无复核上机，夹紧+校验+仿真+人工签字是硬屏障；
> ② 不同数控系统 G 代码方言不同，要预选后置，ISO 兼容机型需厂商后置复核；
> ③ 现阶段限定三轴 2.5D 铣削，复杂曲面/五轴联动/刀杆干涉仍需专业 CAM 与人工工艺评审。

---

## 九、目录结构

```
H:\cncshijue
├── main.py                     CLI 入口（run/demo/controllers/machines/monitor/closed-loop/...）
├── cncagent/
│   ├── models.py               材料库/刀具库/机床规格/零件·工序数据模型
│   ├── config.py               .env 加载、密钥打码、全局配置
│   ├── inputs.py               四种输入解析（NL / JSON / STEP / 旧 G 代码）
│   ├── prompts.py              核心 System Prompt + 返工提示词 + few-shot
│   ├── planner.py              规则规划器 / LLM 规划器 / 降级路由 / 幻觉注入
│   ├── cutting.py              切削参数计算 + 可重入安全夹紧 + 工序排序
│   ├── geometry.py             2.5D 刀路几何（环切/行切/螺旋下刀/圆弧）
│   ├── generator.py            FANUC / SIEMENS / HNC 后置 + 故障注入
│   ├── validator.py            G 代码词法 + 模态状态机 + 规则检查
│   ├── simulator.py            体素材料去除仿真 + 纯标准库 PNG 编码
│   ├── machines.py             11 种机型库 + 能力派生 + 机型工艺策略
│   ├── agent.py                五阶段编排 + 自修复闭环 + fail-closed
│   ├── report.py               编程侧 HTML 报告
│   ├── diagnose.py             报警知识库 + 下发适配器（USB/DNC）
│   └── vision/
│       ├── frames.py           帧源抽象 + 合成加工画面
│       ├── detector.py         YOLO / 启发式检测 + 故障类别
│       ├── monitor.py          监控循环 + 去抖 + 截图 + 日志 + 告警
│       ├── agent_tools.py      工具契约 + 诊断 Agent + 安全门控
│       ├── protocols.py        Modbus TCP / OPC UA / MQTT / dry-run
│       └── closed_loop.py      闭环编排 + 闭环 HTML 报告
├── examples/                   多特征零件 JSON / 旧程序 / 报警日志
├── tests/test_pipeline.py      含 9 项 G 代码加固回归
├── tests/test_secrets.py       7 项密钥私密回归（共 56 项 unittest）
├── scripts/check_secrets.py    提交前密钥扫描器
├── .githooks/pre-commit        提交闸门（clone 后需 git config core.hooksPath .githooks）
└── output/                     G 代码、报告、日志、截图、指纹清单
```

---

## 十、安全声明

AI 输出的程序**不得未经复核直接上机**。闭环链路中的工业协议写入固定 `dry-run`；
真实机床写入只在操控面板的「机床操控」页开放，需 `PANEL_ALLOW_WRITE=1` + 连接时启用写入
+ 白名单 + 操作员二次确认 + 写后读回校验，寄存器/节点地址为示例映射。
视觉检测权重、机床规格、切削参数均为演示配置，正式投产必须用实测数据与厂商资料替换。
本项目不替代机床操作规范、安全 PLC 与工艺评审；急停始终走硬件安全回路。

---

## 十一、操控面板与上线

新增 `panel/` 目录，提供可直接连接机床的 Web 操控台（零新增第三方依赖）：

| 文件 | 作用 |
|---|---|
| `panel/server.py` | HTTP 服务与 REST API；任务串行队列、实时日志、产物快照、鉴权 |
| `panel/machine.py` | 标准库 Modbus TCP 客户端 + 私网限制 + 白名单/二次确认/限速 + 审计 |
| `panel/plc_sim.py` | 虚拟机床 PLC 网关，用于本机实测连接/遥测/写指令/程序下发 |
| `cncagent/drawing.py` | DXF 矢量解析 / 图元→特征映射 / 标注抽取 / SVG 预览 / 位图视觉模型适配器 |
| `examples/drawing_plate.dxf` | 示例工程图：100×100 毛坯、Φ40 圆槽、36×18 矩形槽、2×Φ8 孔、尺寸与技术要求标注 |
| `panel/static/` | 单页控制台：总览、机床操控、智能编程、**图纸编程**、视觉监控、闭环、机型库、产物、上线规划 |
| `panel/selftest.py` | 57 项端到端验收（HTTP + 真实 Modbus 写入 + 图纸 API + 任务链路 + 只读/鉴权） |
| `panel/headless_check.js` | 12 项前端无头验收：真实执行 `app.js` 的 boot，含图纸识别与渲染 |
| `DEPLOY.md` | 分阶段上线规划、安全红线、真机接入步骤、验收清单、回退与应急 |

**本地一键验证**（两个服务，只启动一次）：
```bat
panel\start_local.bat
```
浏览器打开 `http://127.0.0.1:8787` → **默认直达「机床操控」页**，且 demo 模式会自动连上本地虚拟机床网关 `127.0.0.1:15020`（写入门控已开），勾选页内「我已确认现场安全」后即可点按钮真实下发指令。页面顶部横幅会标明当前是**虚拟机床网关**还是**现场机床**。

接真机：在连接卡片填写机床网关的私网 IP / 端口 / 单元号，按 `DEPLOY.md` 第四章完成寄存器映射签字与三级试运行；生产环境设 `PANEL_MODE=intranet` 关闭自动连接。

也可手动开两个终端：`python panel/plc_sim.py` 与 `PANEL_ALLOW_WRITE=1 python panel/server.py`。

**改动后验收**（一次跑完，无需重启服务）：
```bash
python panel/selftest.py --full --readonly   # 57 项
node panel/headless_check.js                 # 12 项，需 Node 18+
python -m unittest discover -s tests         # 56 项
```

> 已知坑已修复：`/api/tasks` 曾在持有 `_TASKS_LOCK` 时调用同样加锁的 `_task_public()` 自死锁，
> 空任务表不复现、一旦有任务记录就永久无响应，导致前端 `loadAll()` 的 `Promise.all` 卡住、
> `bindEvents()` 不执行、所有按钮变成死按钮。现在锁改为可重入且列表只在锁内取快照，
> 前端亦改为「先绑定交互、再拉数据」并给每个接口加超时降级。

**主要 API**：`/api/health`、`/api/state`、`/api/machines[/<id>]`、`/api/files`、
`/api/tasks`（POST 创建 / GET 轮询）、`/api/drawing/{example,analyze}`、
`/api/machine/{status,telemetry,connect,disconnect,command,program,audit}`。

**上线要点**：控制面只部署车间内网 / VPN，禁止公网；`PANEL_ALLOW_WRITE` 默认 0；
公网只读镜像用 `PANEL_MODE=readonly`。完整路线与验收清单见 `DEPLOY.md`。

---

## 十二、图纸编程（工程图 → G 代码）

**主通路是 DXF 矢量工程图**：几何图元是精确数值，标准库逐对解析，不存在 OCR 误差；
`TEXT/MTEXT` 只补充几何里没有的深度、公差、材料与粗糙度。位图（PNG/JPG）走多模态视觉模型，
未配置 Key 时**明确报错并提示改传 DXF**，不用「假装识别」的空壳兜底。

```bash
python main.py from-drawing                                   # 内置示例，完整链路
python main.py from-drawing --file 零件.dxf --analyze-only   # 只出识别报告/SVG/可编辑 JSON
```

面板「图纸编程」页：拖拽或选择图纸 → 看 SVG 预览与识别特征表 → 直接改切深/公差/尺寸
（或编辑结构化 JSON）→ 一键生成 G 代码 → 自动跑参数夹紧 / 静态校验 / 体素仿真 →
准入通过后可直接下发到已连接机床。

识别与安全规则：
- 图层约定 `OUTLINE`（毛坯外轮廓）/ `FEATURE`（加工特征）/ `DIM` / `NOTE`；缺失时退化到几何包围盒并告警。
- 支持 `CIRCLE` / `ARC` / `LINE` / `LWPOLYLINE` / `POLYLINE+VERTEX` / `TEXT` / `MTEXT` / `DIMENSION`，
  兼容 `%%c`(Φ)、`%%p`(±)、`%%d`(°) 控制码与闭合多段线重复首点。
- **几何实测尺寸与标注文字做一致性校验**：不一致时以几何为准、降置信度并输出「需复核」告警；
  读不到深度时填默认 2mm 并告警，绝不静默猜测。
- 识别结果统一落到 `PartSpec` / `parse_json()` 标准格式，复用既有夹紧、校验、仿真、下发准入，**不新增旁路**。

示例图纸实测：4 个特征（Φ40 圆槽深3、36×18 矩形槽深4 R3、2×Φ8 孔深10），
毛坯 100×100×20 / 45#钢，置信度 0.90；生成 130 段 FANUC 程序，静态校验 0 错、
体素仿真碰撞 0、体积误差 4.27%，下发准入通过。

> 图纸识别不替代工艺评审：尺寸基准、装夹、刀具可达性、公差配合仍须人工复核；
> 位图识读结果尤其必须逐项对照原图确认后才能上机。

---

## 十三、仓库推送（镜像通道）

仓库 `https://github.com/111111212/cnc-agent-studio` 的 `main` 已推送，`origin` 保留官方地址。
本机 `github.com` 的 git 智能 HTTP 端点不可达（CONNECT 502 / 读超时），但 `api.github.com`
与镜像站可用，因此推送走镜像转发：

```bash
bash scripts/push_mirror.sh        # Git Bash
scripts\push_mirror.bat            # Windows 命令行，自动定位 Git Bash
```

| 项 | 结果 |
|---|---|
| 通道 | `ghfast.top` 透传 `Authorization`，带认证的 `git-receive-pack` POST 返回 200 |
| 对照 | `gh-proxy.com` 会**剥离** `Authorization`，GitHub 回 401 并让 git 索要用户名，推送卡死，故不作默认 |
| 凭据 | 运行时从本机 Git 凭据管理器读取（`x-access-token` 键，`scope=repo`）；不写 URL、不写 `.git/config`、不落盘、不回显明文 |
| 闸门 | 推送前先跑 `scripts/check_secrets.py`，命中即终止 |
| 校验 | 推送后回读 `ls-remote`，与本地 HEAD 比对 |
| 实测 | 远端 `main` 与本地 HEAD 同 SHA，密钥闸门 0 命中，无 `.env`、`.env.example` 入库 |

> **镜像缓存滞后**：镜像的 `receive-pack` advertisement 可能停在旧 SHA，重复推送会报
> `cannot lock ref ... but expected <旧 SHA>`。脚本因此先用 GitHub API 读取真实分支 SHA：
> 已同步直接跳过，未同步时附带 `Cache-Control: no-cache` 推送；仍失败可稍后重试或换 `CNC_MIRROR`。

可用 `CNC_REPO`、`CNC_BRANCH`、`CNC_MIRROR` 覆盖默认值。镜像都不可用时，退回离线方案
`output/cnc-agent-studio.bundle` + `output/push_from_bundle.bat`，拷到能直连 GitHub 的机器执行。

> 镜像只负责传输，不改变仓库归属，`origin` 始终是官方地址。不要把镜像返回内容当作发布依据，
> 发布前用 `https://api.github.com/repos/111111212/cnc-agent-studio` 复核分支 SHA。

