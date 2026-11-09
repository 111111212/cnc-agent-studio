# CNC Agent 操控台 · 上线规划（面向真实机床直连）

> 目标：把「智能编程 + 视觉监测 + 故障诊断 + 机床操控」部署到车间，且**不因 AI 引入新的安全风险**。
> 结论先行：**机床控制面禁止发布到公网**。本面板只部署在车间内网 / VPN；公网如需演示，另起一台与机床物理隔离的只读镜像（`PANEL_MODE=readonly`）。

---

## 一、系统边界与数据流

```
[车间内网 / VPN]
  浏览器 ──HTTPS/Token──> Nginx ──> panel/server.py :8787
                                      │
              ┌───────────────────────┼────────────────────────┐
              │                       │                        │
      cncagent 编程链路        panel/machine.py          output/ 产物与审计
   输入→规划→后处理→校验→仿真    Modbus TCP 客户端          报告/G代码/快照/JSONL
              │                       │
              │ 工艺反哺修订程序       │ FC01/03/05/06/16
              └─────── DNC 程序 ──────┼──── 线圈/保持寄存器 ────> 机床 PLC 网关
                                      │                          (本地可用 plc_sim 替代)
   视觉闭环中的 AI 控制建议 ──> 待确认队列 ──人工逐条确认──┘
```

- **两条写入通道严格分离**：闭环自动诊断固定 `dry-run`，只产审计指令；真实写入只发生在「机床操控」页，经白名单 + 二次确认 + 服务端总开关。
- **急停不在此链路内**：无软件写入地址，必须走硬件安全 PLC / 操作面板急停按钮。
- 客户端仅用标准库 `socket` 实现 Modbus TCP，避免现场工控机装不上第三方包；OPC UA / MQTT 为可选扩展（见第八节）。

---

## 二、分阶段上线路线（每阶段都有准入与回退）

| 阶段 | 内容 | 准入条件 | 验收标准 | 回退 |
|---|---|---|---|---|
| **0 本地虚拟网关验证** | `plc_sim.py` 起虚拟机床，面板连 `127.0.0.1:15020`，跑连接/遥测/指令/程序下发/审计 | 代码冻结 + 56 项单测全绿 | 所有 API 实测通过；急停软写被拒绝；写后读回一致；审计完整 | 停服务即可，无现场影响 |
| **1 内网只读遥测** | 部署到车间工控机，`PANEL_ALLOW_WRITE=0`，只连读寄存器 | 电气图 / PLC 变量表核对签字；确认无公网入口；`PANEL_TOKEN` 已设 | 连续 72h 遥测稳定；心跳可判活；无任何写帧 | 断开连接，机床侧无感 |
| **2 受控写入试运行** | 开 `PANEL_ALLOW_WRITE=1`，只放行进给保持 / 倍率 / 循环停止；空运行 → 单段 → 低倍率 | 双人确认制度、硬件急停有效、看门狗超时自动进给保持、完成一次急停演练 | 每条写入有审计 + 读回校验；限速与白名单拦截生效 | 立即 `PANEL_ALLOW_WRITE=0` 并重启面板 |
| **3 DNC 程序下发 + 视觉闭环** | 开放程序缓冲下发；视觉建议进待确认队列，人工逐条放行 | 程序双人复核、M30/M02 校验、SHA 留痕；AI 建议禁止自动执行 | 程序段数/字节数读回一致；修订程序双关卡 PASS | 清空程序缓冲区，切回 U 盘拷贝 |
| **4 生产运行** | 网络隔离、mTLS/强令牌、集中审计、变更管理、定期回归 | 应急预案、权限分级、日志保留周期、点检表齐备 | 月度演练与回归通过 | 切回上一版本镜像 + 恢复配置备份 |

**阶段 0 是硬门槛**：没有在虚拟网关上跑通全部写指令与回读校验，不允许进入阶段 1。

---

## 三、安全红线（不可协商）

1. 控制面只监听内网，Nginx `allow 192.168.x.0/24; deny all;`，公网只读镜像与机床网络物理/逻辑隔离。
2. 急停、主轴启动、轴点动、换刀**没有软件写入通道**，走硬件回路或机床操作面板。
3. `PANEL_ALLOW_WRITE` 默认 `0`；开启需变更单，且连接时还要显式勾选「启用控制写入」。
4. 白名单外指令一律拒绝（含 AI 幻觉编造的信号名）；同信号 0.8s 限速。
5. 每次写入必须操作员二次确认；`caution` 级指令额外弹窗，含现场安全提示。
6. 保持寄存器写后读回校验；程序下发校验 `M30/M02`、60KB 上限，并回读字节/段数。
7. 只接受私网目标地址（loopback / 10 / 172.16~31 / 192.168 / link-local），域名形式保守拒绝。
8. AI 诊断建议**只进待确认队列**，永不自动写机床；闭环链路固定 dry-run。
9. 全量审计 append-only：`output/machine/audit.jsonl`、`plc_audit.jsonl`、`vision/control_audit.jsonl`。
10. 程序上机前仍必须经过校验 + 体素仿真 + 人工复核清单，AI 输出不得直接投产。
11. `X-Frame-Options: SAMEORIGIN` 在 **demo 本机模式**下放宽（便于内置预览 iframe 嵌入，
    由 `PANEL_ALLOW_EMBED=0` 可恢复）；**车间内网 / 公网只读模式必须保持 SAMEORIGIN**，
    防止点击劫持诱导操作员误发控制指令。上线前用 `curl -I` 核对响应头。
12. 图纸识读结果**必须人工逐项对照原图复核**：几何尺寸与标注不一致、缺深度/公差标注、
    以及全部位图/视觉模型输出，均须签字确认后才能生成并下发程序。矢量 DXF 优先，
    位图识读仅作为辅助手段；`output/drawings/` 保留原图与识别报告以便追溯。

---

## 四、真机接入操作步骤

1. **拿资料**：机床电气图、PLC 变量表、厂商通信手册、后置处理手册、安全回路图。
2. **核对映射**（下表为模板，需逐项签字确认后替换 `panel/plc_sim.py` 与 `panel/machine.py` 中的常量）：

| 功能 | 示例地址 | 真实地址 | 数据类型 | 是否允许写 | 核对人 |
|---|---|---|---|---|---|
| 进给保持 | coil 0x0100 | ______ | BOOL 脉冲 | 是 | ______ |
| 循环启动 | coil 0x0103 | ______ | BOOL 脉冲 | 是（双人） | ______ |
| 主轴停止 | coil 0x0102 | ______ | BOOL | 是 | ______ |
| 进给倍率 | holding 0x0200 | ______ | UINT 0~150 | 是 | ______ |
| 主轴负载 | holding 0x1002 | ______ | UINT % | 否 | ______ |
| X/Y/Z 坐标 | holding 0x1005~07 | ______ | INT ×10 | 否 | ______ |
| 运行/报警状态 | holding 0x1008/09 | ______ | UINT | 否 | ______ |
| 心跳 | holding 0x100F | ______ | UINT | 否 | ______ |
| 程序缓冲 | holding 0x0300 | ______ | ASCII | 是 | ______ |
| 急停 | **硬件回路** | ______ | — | **禁止** | ______ |

3. **先在机床侧做只读监听**：用面板连上只读遥测，比对面板数值与机床屏幕数值一致。
4. **逐条试写**：从 `FEED_HOLD` 开始，一次一条，观察机床实际响应并与审计日志比对；异常立即停。
5. **程序下发**：空运行 → 单段 → 低倍率（≤25%）→ 逐步提升；每次确认刀具、装夹、坐标系 G54。
6. **急停演练**：在空载状态下按下硬件急停，确认面板不尝试任何软写、状态能正确回读。

---

## 五、部署配置样例

**.env（车间工控机）**
```ini
PANEL_HOST=127.0.0.1        # 由 Nginx 反代，不直接监听 0.0.0.0
PANEL_PORT=8787
PANEL_TOKEN=<强随机令牌，勿提交仓库>
PANEL_ALLOW_WRITE=0         # 阶段 2 才由变更单改为 1
PANEL_MODE=demo
CNC_MACHINE=vmc850_fanuc
YOLO_WEIGHTS=/opt/models/best.pt
LLM_API_KEY=<由部署平台环境变量注入>
```

**Nginx（仅内网 + TLS + IP 白名单）**
```nginx
server {
  listen 443 ssl;
  ssl_certificate     /etc/ssl/cnc.crt;
  ssl_certificate_key /etc/ssl/cnc.key;
  allow 192.168.1.0/24;
  deny  all;
  client_max_body_size 2m;
  location / { proxy_pass http://127.0.0.1:8787; proxy_read_timeout 120s; }
}
```

**systemd（/etc/systemd/system/cnc-panel.service）**
```ini
[Unit]
Description=CNC Agent Panel
After=network.target

[Service]
WorkingDirectory=/opt/cncshijue
EnvironmentFile=/opt/cncshijue/.env
ExecStart=/usr/bin/python3 panel/server.py
Restart=on-failure
User=cncpanel
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/cncshijue/output

[Install]
WantedBy=multi-user.target
```

**Docker（仅在有内网构建环境时使用）**
```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8787
CMD ["python", "panel/server.py"]
```
> 容器网络必须 `--network=internal` 或仅挂内网 bridge；禁止 `-p 8787:8787` 映射到公网网卡。

**本地一键验证**
```bat
:: 双击 panel/start_local.bat，或手动开两个终端
python panel/plc_sim.py
set PANEL_ALLOW_WRITE=1 && python panel/server.py
```
浏览器打开 `http://127.0.0.1:8787`。

---

## 六、验收测试清单（上线前逐项打勾）

- [ ] `python -m unittest discover -s tests` → 56 项全绿
- [ ] `git config core.hooksPath .githooks` 已启用、`python scripts/check_secrets.py` 无命中；真实密钥只在本机 `.env`，仓库内 `.env.example` 全为空值
- [ ] `/api/health` 返回 `readonly=false`、`machine_linked=true`、`control_mode` 状态正确
- [ ] 连接非私网地址被拒绝并留下 `connect_rejected` 审计
- [ ] 只读连接下所有写指令返回拒绝（服务端总开关关闭 / 连接未勾选）
- [ ] 白名单外指令、急停软写、`FEED_OVERRIDE=999` 全部被拦截
- [ ] `FEED_HOLD / CYCLE_START / FEED_OVERRIDE` 真实写入成功且状态回读一致
- [ ] 倍率写后读回校验通过；同信号连点被限速
- [ ] G 代码下发：M30 校验、分块 FC16、COMMIT 回读字节/段数一致
- [ ] 编程链路：静态校验 0 错误、体素仿真 PASS、生成带哈希复核清单
- [ ] 幻觉注入演示：参数夹紧 6 项 + 代码拦截 6 项，第 2 轮修复后双关卡 PASS
- [ ] 视觉监控 145 帧 / 6 事件，零误报零漏报，截图与日志留存
- [ ] 闭环 18 条批准 / 1 条急停拦截，修订程序双关卡 PASS 且 SHA 变化
- [ ] 所有审计 JSONL 可回溯，产物快照按 task_id 隔离
- [ ] 断网 / 网关宕机时面板显示离线，不产生误写

---

## 七、回退与应急

| 场景 | 动作 |
|---|---|
| 面板异常下发 | 按机床**硬件急停** → `PANEL_ALLOW_WRITE=0` 并重启面板 → 导出审计定位 |
| 通信中断 | 面板显示离线；机床侧维持原状态；看门狗超时触发进给保持（需 PLC 侧配置） |
| 程序错误 | 清空程序缓冲区 → 切回 U 盘拷贝 + 人工复核流程 |
| 版本回退 | 切回上一镜像 / commit，恢复 `.env` 备份；产物目录不动，快照可对照 |
| 令牌泄露 | 立即更换 `PANEL_TOKEN` 并重启，检查审计中异常 IP |

> 看门狗/心跳丢失自动进给保持属于 **PLC 侧安全逻辑**，必须在阶段 2 前由电气工程师在 PLC 程序中实现；本软件只提供心跳寄存器读取与状态显示，不能替代安全 PLC。

---

## 八、已知限制与扩展点

- **机型规格**与寄存器地址是典型示例配置，投产前必须换成机床铭牌、厂商后置手册与电气图实测值。
- `iso_compatible` 机型（三菱 / Brother / Haas / Mazak / 精雕 / 广数）按 FANUC ISO 方言近似输出，程序头写明需核对 M 代码。
- 项目范围限定三轴 2.5D 铣削；五轴联动、复杂曲面仍需专业 CAM 与人工工艺评审（五轴 RTCP 能力被显式禁用）。
- 视觉：无 `.pt` 权重时回退离线像素启发式并如实标注；真实 YOLO / 摄像头接入走 CLI 的 `--frames camera --weights best.pt`。
- **OPC UA / MQTT 真实客户端**：`cncagent/vision/protocols.py` 已提供适配器骨架，需要 `pip install opcua` / `paho-mqtt`；面板内置的零依赖客户端目前只实现 Modbus TCP。接入前需在 `panel/server.py` 的 `MachineLink.connect()` 注册对应实现并复用同一套 `CommandGuard` 门控。
- 体素仿真是**安全-grade 的近似检查**，不是 CAM 级几何内核；不能替代机床厂仿真软件与首件检验。
- 大模型只输出结构化工艺 JSON，G 代码由确定性后处理器生成；即便如此，AI 输出仍不得跳过校验、仿真、人工复核三道关卡。

---

## 九、公网只读演示（可选）

如果确实需要对外的演示链接，单独部署一台**与机床网络隔离**的机器：

```bash
PANEL_MODE=readonly PANEL_ALLOW_WRITE=0 PANEL_TOKEN=<令牌> python panel/server.py
```

`readonly` 模式下服务端拒绝全部执行类 POST（编程 / 监控 / 闭环 / 机床指令 / 程序下发），只保留报告查看、产物下载与纯计算报警诊断。禁止把这台机器同时接入车间网络。

---

## 十、变更记录 · 2026-11-08 安全与正确性加固

本轮为代码审查后的缺陷修复，不改架构、不改 API 契约。接真机前请重新通读本节。

### 10.1 直连安全（fail-closed）

- **修订程序准入**：`closed_loop.py` 只在静态校验 + 体素仿真双关卡通过时才写 `revised_*.nc`，同时产出 `revised_*.approved.json`（含程序 SHA-256、双关卡结论、机型/方言/时间）。未通过时删除本机型产物；其它机型遗留的无凭证 `revised_*.nc` 会被改名为 `*.nc.unapproved.bak` 隔离（保留内容可回溯，但不再被下发的 `.nc` 列表扫到）。**下发前请核对凭证里的 SHA 与文件实际字节一致**。
- **鉴权**：不再接受 `?token=` URL 传参（会进浏览器历史 / Referer / 反代日志）；请求行日志对令牌脱敏；`/api/login` 按来源 IP 做 60 秒 8 次失败限速。
- **任务参数白名单**：`video` / `images` 限定在 `output/` 内、`weights` 限定在项目目录内，`device` 字段一律丢弃，防止借帧源参数读取任意本地文件。
- **Modbus 帧校验**：校验事务 ID / 协议 ID / 从站号 / 功能码 / 响应长度；异常响应至少 2 字节才取异常码；FC05/06/16 写响应必须回显地址与值。线圈指令与保持寄存器一样做**写后读回**。
- **断线状态**：遥测读失败立即回写 `connected=False` 并落审计，避免 `status()` 仍报「已连接可写」而控制门控基于陈旧状态放行。前端以遥测结果为准回写连接横幅。
- **协议适配器**：pymodbus 的 `isError()` / MQTT 的 `publish().rc` 现在会真实判定失败，不再无条件上报成功；无寄存器映射的本地工单明确标注「未写机床」。
- 闭环报告的 DRY-RUN 标注改为按实际 `dry_run` 渲染，不再硬编码。

### 10.2 G 代码链路一致性

- 三方言固定循环统一显式声明返回平面（FANUC/HNC 输出 `G99`，SIEMENS `CYCLE81` 的 RTP 设为 R 平面），后处理器 / 静态校验器 / 仿真器使用同一份约定，不再各自猜 `safe_z`。
- 修复 SIEMENS `CYCLE81` 参数解析：旧实现用 `re.findall` 把行号 `N150` 和循环名里的 `81` 混入数字序列，导致孔底深度取成 `0.0`、孔加工仿真完全没切削。
- `G80` 恢复循环前的运动模态（旧实现置 `-1`，把后续快移误判成切削、绕过撞刀检测）；仿真器固定循环结束后的刀具位置回到 R 平面而非孔底。
- 静态校验器按圆弧**实际扫掠角**采样（旧实现见到 I/J 就按整圆采样，靠近行程边界会虚报超行程）；支持 G54~G59 工件坐标系。
- 注释统一净化换行 / 括号 / `%` / 控制字符，工序阶段加入白名单；模型输出无法再借注释注入裸 G 代码程序段。
- 毛坯与特征尺寸、仿真分辨率做有限性与上界夹紧，网格总格数设 400 万上限并自适应降采样，避免不可信输入触发 OOM。
- `.env` 与配置数值读取容错；LLM 降级路径补捕获 `TypeError/IndexError/AttributeError`。

### 10.3 图纸编程兼容性

- DXF 按 `$DWGCODEPAGE` 选编码（中文 AutoCAD 的 GBK/ANSI_936），再按 utf-8 / gbk / cp1252 / latin-1 降级；中文深度、材料、毛坯、公差标注不再被 `replace` 成乱码。
- 读 `$INSUNITS` 把几何换算到 mm（英寸 25.4、厘米 10 等），单位非 mm 时显式告警；**标注文字里的尺寸单位仍需人工复核**。
- 词法解析改为按 group code 行驱动，段间空行不再让后续配对整体错位。
- 支持 LWPOLYLINE 的 code 42 凸度（圆弧离散 + 圆角半径估算）、`INSERT` 块参照展开（平移/缩放/旋转/矩形阵列/有限深度嵌套）、整圆 `ARC` 识别、弧段包围盒、中文/厂商图层名模糊匹配。

### 10.4 面板稳定性

- 所有请求默认 8 秒超时；遥测 / 审计 / 任务 / 日志轮询加 in-flight 闸门、页面隐藏暂停、404 三次后终止轮询；日志定时器纳入统一生命周期管理。
- 新增「后端断线 / 正在重连」常驻横幅；控制按钮加 loading 态与异常兜底；确认框改用可移除监听器，支持 ESC 取消，不再留下永不 resolve 的 Promise。

### 10.5 验证与运维注意

| 项目 | 结果 |
|---|---|
| 单元测试 | `56 tests, OK`（原 34 + 15 项加固回归 + 7 项密钥私密回归） |
| 面板自检 | `python panel/selftest.py --full --readonly` → **57/57 PASS** |
| 前端无头 | `node panel/headless_check.js` → **12/12 PASS** |
| 三方言双关卡 | FANUC 70 段 / SIEMENS 83 段 / HNC 68 段，校验 0 错、仿真 0 问题 |
| 图纸链路 | 4 特征 / 置信 0.90 / 130 段 / 体积误差 4.27% / 碰撞 0 |
| 闭环准入 | 修订双关卡 PASS，`approved.json` 的 SHA-256 与 `.nc` 实际字节一致 |

**面板服务已在本轮验收时重启**，`127.0.0.1:8787` 当前运行加固后代码，完整自检在新后端上重跑为 57/57 PASS，且闭环产出的 `.nc` 带 SHA 一致的准入凭证（静态前端文件实时读盘，无需重启）。后续若再修改后端，重启命令：

```bash
# 先停掉旧的 panel/server.py 进程，再执行
PANEL_MODE=demo PANEL_ALLOW_WRITE=1 python panel/server.py
```

### 10.6 仍然存在的边界

- 尚未接入真实机床、LLM API Key、YOLO 权重、FOCAS/OPC UA/MQTT 实际客户端；当前连接的是本地 `127.0.0.1:15020` 虚拟机床网关。
- 看门狗 / 心跳丢失自动进给保持属于 **PLC 侧安全逻辑**，软件只提供心跳读取与断线告警，不能替代安全 PLC。
- DXF 仍不支持 SPLINE / ELLIPSE / HATCH / 嵌套属性文字；真实图纸的原图尺寸基准、公差、图层语义必须人工复核后再下发。
- G83 啄钻在三方言中尚未启用，`planner` 的 `peck` 策略目前仍以后处理成 G81 / CYCLE81 落地。
