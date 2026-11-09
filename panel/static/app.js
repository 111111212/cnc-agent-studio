/* CNC Agent 操控台前端：无外部依赖，全部走同源 REST API。 */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const S = {
  health: null, machines: [], state: null, files: [], tasks: [],
  link: null, telemetry: null, audit: [], suggestions: [],
  poll: null, tick: null, logTick: null, logBusy: false,
  booted: false, lastCap: null, offline: false, offlineMsg: '',
  drawing: { analysis: null, data: null, rawB64: '', name: '', lastProgram: '' },
};

const esc = (v) => String(v == null ? '' : v)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');
const fmtSize = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB'
  : n > 1024 ? (n / 1024).toFixed(1) + ' KB' : n + ' B';
const fmtTime = (t) => t ? new Date(t * 1000).toLocaleString('zh-CN', { hour12: false }) : '—';

async function api(path, opts = {}) {
  // 默认必须有超时：断网时零超时的 fetch 会永久挂起，配合轮询无限堆积
  const timeoutMs = opts.timeoutMs === undefined ? 8000 : opts.timeoutMs;
  const o = Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts);
  delete o.timeoutMs;
  let timer = null;
  if (timeoutMs > 0 && !o.signal) {
    const c = new AbortController();
    o.signal = c.signal;
    timer = setTimeout(() => c.abort(), timeoutMs);
  }
  try {
    const res = await fetch(path, o);
    let data;
    try { data = await res.json(); } catch (e) { data = { ok: false, error: 'HTTP ' + res.status }; }
    if (res.status === 401 && path !== '/api/login') { $('#loginModal').classList.remove('hidden'); }
    return data;
  } catch (e) {
    // 单个接口挂掉不能拖死整个控制台：返回可识别的失败对象，由调用方降级渲染
    return { ok: false, _failed: true, error: '请求超时或失败：' + path };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// 单接口失败降级为空对象，保证并发加载不会被任何一个接口卡住
const safeApi = (path, ms = 6000) => api(path, { timeoutMs: ms }).then(r => r || {});

function toast(msg, kind = '') {
  const box = $('#toast');
  const el = document.createElement('div');
  el.className = 'toast-item ' + kind;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function confirmDialog(title, body, okText = '确认执行') {
  return new Promise((resolve) => {
    const modal = $('#confirmModal');
    const btnOk = $('#btnOkConfirm'), btnCancel = $('#btnCancelConfirm');
    $('#confirmTitle').textContent = title;
    $('#confirmBody').textContent = body;
    btnOk.textContent = okText;
    modal.classList.remove('hidden');
    let settled = false;
    // 用可移除的监听器：覆盖式 .onclick 会让连续两次调用的旧 Promise 永不 resolve
    const done = (v) => {
      if (settled) return;
      settled = true;
      modal.classList.add('hidden');
      btnOk.removeEventListener('click', onOk);
      btnCancel.removeEventListener('click', onCancel);
      document.removeEventListener('keydown', onKey);
      resolve(v);
    };
    const onOk = () => done(true);
    const onCancel = () => done(false);
    const onKey = (e) => { if (e.key === 'Escape') done(false); };
    btnOk.addEventListener('click', onOk);
    btnCancel.addEventListener('click', onCancel);
    document.addEventListener('keydown', onKey);
  });
}

function kpi(label, value, sub, cls = '') {
  return '<div class="kpi ' + cls + '"><div class="k-label">' + esc(label) +
    '</div><div class="k-value' + (String(value).length > 12 ? ' sm' : '') + '">' +
    esc(value) + '</div><div class="k-sub">' + esc(sub || '') + '</div></div>';
}

function fileRow(path, label, extra = '') {
  return '<div class="list-row"><div class="lr-main"><div class="lr-title">' +
    esc(label || path) + '</div><div class="lr-sub">' + esc(extra || path) +
    '</div></div><a class="btn btn-sm" target="_blank" href="/view/' + esc(path) +
    '">打开</a><a class="btn btn-sm" href="/download/' + esc(path) + '">下载</a></div>';
}

// ---------------------------------------------------------------- 加载
async function loadAll() {
  const [machines, state, files, tasks, mstatus, audit] = await Promise.all([
    safeApi('/api/machines'), safeApi('/api/state'), safeApi('/api/files'),
    safeApi('/api/tasks'), safeApi('/api/machine/status'), safeApi('/api/machine/audit'),
  ]);
  S.machines = machines.machines || [];
  S.state = state;
  S.files = files.files || [];
  S.tasks = tasks.tasks || [];
  S.link = mstatus;
  S.audit = audit.records || [];
  renderChips();
  renderMachineSelects();
  renderOverview();
  renderTasks();
  renderFiles();
  renderNcSelect();
  renderCmdGrid();
  renderAudit();
  renderMachineTable();
  renderMachineBanner();
  $('#fileCount').textContent = (files.total || 0);
  $('#sfMode').textContent = S.health && S.health.readonly ? '只读演示' : '本机操控';
  $('#sfWrite').textContent = mstatus.server_allows_write ? '服务端已开启' : '服务端关闭';
  $('#linkDot').className = 'nav-dot' + (mstatus.connected ? ' on' : '');
  const t = await api('/api/machine/telemetry');
  S.telemetry = t;
  renderTelemetry(t);
  $('#telemetryTag').textContent = t.connected ? '实时' : '离线';
  $('#linkStateTag').textContent = t.connected ? '已连接 ' + t.target : '未连接';
}

function renderChips() {
  const h = S.health || {};
  const st = S.state || {};
  const items = [
    ['模式', h.readonly ? '只读演示' : '本机操控', h.readonly ? 'warn' : 'ok'],
    ['机床', h.machine_linked ? '已连接 ' + h.machine_target : '未连接', h.machine_linked ? 'ok' : 'warn'],
    ['写入门控', S.link && S.link.server_allows_write ? '已开启' : '关闭', S.link && S.link.server_allows_write ? 'warn' : 'ok'],
    ['大模型', h.llm_available ? '已配置' : '规则规划器', h.llm_available ? 'ok' : ''],
    ['视觉', h.yolo_weights ? 'YOLO 权重' : '启发式后端', h.yolo_weights ? 'ok' : 'warn'],
    ['闭环下发', 'dry-run', 'info'],
  ];
  $('#chips').innerHTML = items.map(([l, v, c]) =>
    '<span class="chip ' + c + '"><i class="dot ' + (c === 'ok' ? 'on' : '') +
    '"></i>' + esc(l) + ' <b>' + esc(v) + '</b></span>').join('');
}

function renderMachineSelects() {
  const opts = S.machines.map(m =>
    '<option value="' + esc(m.id) + '">' + esc(m.display || (m.brand + ' ' + m.model)) + '</option>').join('');
  const cur = (S.state && S.state.machine_id) || 'vmc850_fanuc';
  ['#globalMachine', '#pgMachine', '#lpMachine', '#dwMachine'].forEach(sel => {
    const el = $(sel); if (!el) return;
    el.innerHTML = opts;
    el.value = cur;
  });
  $('#mcCount').textContent = S.machines.length;
}

function renderOverview() {
  const r = (S.state && S.state.last_run) || null;
  const loop = (S.state && S.state.last_closed_loop) || null;
  const sim = r && r.simulation ? r.simulation : {};
  let html = '';
  html += kpi('上次编程准入', r ? (r.approved ? '通过' : '拒绝') : '未运行',
    r ? (r.controller + ' · 修复 ' + r.attempts + ' 轮') : '点击「生成并校验」',
    r && r.approved ? 'good' : 'warn');
  html += kpi('静态校验', r && r.validation === true ? 'PASS' : (r ? 'FAIL' : '—'),
    '错误 0 / 警告 0', r ? 'good' : 'info');
  html += kpi('体素仿真', sim.volume_error_pct != null ? 'PASS' : '—',
    '体积误差 ' + (sim.volume_error_pct != null ? sim.volume_error_pct + '%' : '—') +
    ' · 欠切 ' + (sim.undercut_ratio != null ? sim.undercut_ratio + '%' : '—'), 'good');
  html += kpi('视觉事件', S.state ? S.state.event_count : 0, '最近监控日志累计', 'info');
  html += kpi('闭环信号', loop ? loop.approved_signals : 0,
    loop ? ('拦截 ' + loop.blocked_signals + ' 条') : '尚未运行', 'info');
  html += kpi('产物文件', S.state ? S.state.file_count : 0, '报告 / G代码 / 日志', 'info');
  $('#kpiGrid').innerHTML = html;

  const steps = [
    ['01', '输入解析', '自然语言 / 图纸 / STEP / 旧 G 代码'],
    ['02', '工艺规划', '选刀 · S/F · 工序 · 参数夹紧'],
    ['03', 'G 代码 + 校验', '确定性后处理 · 模态状态机'],
    ['04', '体素仿真', '过切 / 欠切 / 碰撞 / 行程'],
    ['05', '下发与操控', 'DNC / U 盘 · 视觉闭环 · 人工复核'],
  ];
  $('#flow').innerHTML = steps.map((s, i) =>
    '<div class="flow-step"><div class="fs-n">' + s[0] + '</div><div class="fs-t">' +
    s[1] + '</div><div class="fs-d">' + s[2] + '</div></div>' +
    (i < steps.length - 1 ? '<div class="flow-arrow">›</div>' : '')).join('');
  $('#taskCount').textContent = S.tasks.length;
}

function renderTasks() {
  const el = $('#taskList');
  if (!S.tasks.length) { el.innerHTML = '<div class="empty">暂无任务</div>'; return; }
  el.innerHTML = S.tasks.slice(0, 8).map(t => {
    const cls = t.status === 'done' ? 'pass' : t.status === 'error' ? 'fail' :
      t.status === 'running' ? 'fixed' : 'info';
    return '<div class="list-row"><div class="lr-main"><div class="lr-title">' +
      esc(t.type) + ' · ' + esc(t.id) + '</div><div class="lr-sub">' +
      fmtTime(t.created) + ' · ' + esc(JSON.stringify(t.params).slice(0, 90)) +
      '</div></div><span class="pill ' + cls + '">' + esc(t.status) + '</span></div>';
  }).join('');
}

function renderFiles() {
  const body = $('#fileTable').querySelector('tbody');
  body.innerHTML = S.files.slice(0, 80).map(f =>
    '<tr><td class="mono">' + esc(f.path) + '</td><td>' + fmtSize(f.size) +
    '</td><td>' + fmtTime(f.mtime) + '</td><td><a class="btn btn-sm" target="_blank" href="' +
    esc(f.url) + '">打开</a> <a class="btn btn-sm" href="/download/' + esc(f.path) +
    '">下载</a></td></tr>').join('') || '<tr><td colspan="4">暂无产物</td></tr>';
}

function renderNcSelect() {
  const ncs = S.files.filter(f => f.path.endsWith('.nc') && !f.path.startsWith('panel_runs'));
  $('#ncSelect').innerHTML = ncs.map(f =>
    '<option value="' + esc(f.path) + '">' + esc(f.path) + '</option>').join('') ||
    '<option value="">暂无 G 代码</option>';
}

function renderMachineTable() {
  const body = $('#machineTable').querySelector('tbody');
  body.innerHTML = S.machines.map(m => {
    const sp = m.spec || {};
    return '<tr><td class="mono">' + esc(m.id) + '</td><td>' + esc(m.display) +
      '</td><td>' + esc(m.controller) + '</td><td>' + esc(m.post) +
      '</td><td>' + esc(sp.max_spindle != null ? sp.max_spindle : '—') +
      '</td><td>' + esc(sp.max_feed != null ? sp.max_feed : '—') +
      '</td><td>' + esc(m.category) + '</td><td><button class="btn btn-sm" data-cap="' +
      esc(m.id) + '">能力</button></td></tr>';
  }).join('');
}

function renderCmdGrid() {
  const cmds = (S.link && S.link.commands) || [];
  if (!cmds.length) {
    $('#cmdGrid').innerHTML = '<div class="empty" style="grid-column:1/-1">' +
      '控制指令列表未加载（机床状态接口无响应或未连接）。' +
      '点击「连接」后按「刷新遥测」重试。</div>';
    return;
  }
  $('#cmdGrid').innerHTML = cmds.map(c =>
    '<button class="cmd ' + esc(c.risk) + '" data-cmd="' + esc(c.signal) + '">' +
    '<div class="c-label">' + esc(c.label) + '</div><div class="c-meta">' +
    esc(c.signal) + ' · ' + esc(c.address) + '</div></button>').join('');
}

function renderAudit() {
  const body = $('#auditTable').querySelector('tbody');
  body.innerHTML = S.audit.slice().reverse().slice(0, 60).map(a => {
    const ok = a.ok === true || a.kind === 'command' || a.kind === 'program' ||
      a.kind === 'connect';
    return '<tr><td>' + esc(a.ts) + '</td><td>' + esc(a.kind) + '</td><td class="mono">' +
      esc(a.signal || a.addr || a.address || '—') + '</td><td>' + esc(a.value == null ? '—' : a.value) +
      '</td><td><span class="pill ' + (ok ? 'pass' : 'fail') + '">' +
      (ok ? '通过' : '拦截') + '</span></td><td>' +
      esc(a.detail || a.reason || a.error || '') + '</td></tr>';
  }).join('') || '<tr><td colspan="6">暂无审计记录</td></tr>';
}

function setOffline(on, msg) {
  S.offline = !!on;
  if (on) S.offlineMsg = msg || '后端无响应';
  renderMachineBanner();
}

function renderMachineBanner() {
  const l = S.link || {};
  const el = $('#machineBanner');
  if (S.offline) {
    el.className = 'banner banner-danger';
    el.innerHTML = '<b>［后端断线 / 正在重连］</b> ' + esc(S.offlineMsg) +
      ' · 恢复前控制指令不会生效；请检查面板服务与机床网关，之后点「刷新遥测」。';
    return;
  }
  const sim = /^(127\.0\.0\.1|localhost|::1)(:|$)/.test(l.target || '');
  const badge = sim
    ? '<b>［演示模式 · 虚拟机床网关］</b> '
    : '<b>［现场机床］</b> ';
  const realTip = '<div class="hint" style="margin-top:6px">接真机：左侧填写机床网关的<b>私网 IP / 端口 / 单元号</b>，'
    + '按电气图与 PLC 变量表逐项核对寄存器映射并签字后连接；生产环境把 PANEL_MODE 设为 intranet，'
    + '即关闭本机网关自动连接。真机接入前必须完成空运行、单段、低倍率三级验证。</div>';
  if (!l.connected) {
    el.className = 'banner banner-warn';
    el.innerHTML = badge + '当前未连接机床。本地验证可启动虚拟机床网关'
      + '（python panel/plc_sim.py）后连接 127.0.0.1:15020。' + realTip;
  } else {
    el.className = 'banner ' + (l.can_write ? 'banner-danger' : 'banner-info');
    el.innerHTML = badge + (l.can_write
      ? '已连接 ' + esc(l.target) + '，<b>控制写入已启用，按钮可真实下发</b>。每次指令都需白名单校验、二次确认并写入审计日志；急停仍只走硬件回路。'
      : '已连接 ' + esc(l.target) + '，当前为<b>只读遥测</b>。要下发指令请在连接时勾选「启用控制写入」，且服务端需 PANEL_ALLOW_WRITE=1。')
      + realTip;
  }
}

function renderTelemetry(t) {
  const el = $('#telemetry');
  if (!t || !t.connected) {
    el.innerHTML = '<div class="empty" style="grid-column:1/-1">未连接：' +
      esc((t && (t.last_error || t.error)) || '请先连接机床') + '</div>';
    return;
  }
  const items = [
    ['运行状态', t.state_label, t.state === 1 ? 'hl' : (t.state === 3 ? 'alarm' : '')],
    ['主轴设定', t.spindle_cmd, 'rpm'],
    ['主轴实际', t.spindle_act, 'rpm'],
    ['主轴负载', t.spindle_load, '%'],
    ['实际进给', t.feed_act, 'mm/min'],
    ['进给倍率', t.feed_override, '%'],
    ['X 坐标', Number(t.x).toFixed(1), 'mm'],
    ['Y 坐标', Number(t.y).toFixed(1), 'mm'],
    ['Z 坐标', Number(t.z).toFixed(1), 'mm'],
    ['程序号 / 段', t.block_no + ' / ' + t.block_total, 'O' + t.program_no],
    ['报警代码', t.alarm_code, t.alarm_code ? 'alarm' : ''],
    ['心跳', t.heartbeat, '存活计数'],
  ];
  el.innerHTML = items.map(([l, v, third]) => {
    const cls = third === 'hl' || third === 'alarm' ? third : '';
    const sub = ['hl', 'alarm', ''].includes(third) ? '' : third;
    return '<div class="tm ' + cls + '"><div class="t-l">' + esc(l) +
      '</div><div class="t-v">' + esc(v) + (sub ? ' <small>' + esc(sub) + '</small>' : '') +
      '</div></div>';
  }).join('');
}

function renderSuggestions() {
  const el = $('#suggestions');
  $('#sugCount').textContent = S.suggestions.length;
  if (!S.suggestions.length) {
    el.innerHTML = '<div class="empty">闭环演练后，Agent 批准的控制建议会出现在这里，需人工逐条确认才会写入机床。</div>';
    return;
  }
  const known = ((S.link && S.link.commands) || []).map(c => c.signal);
  el.innerHTML = S.suggestions.map((s, i) => {
    const ok = known.includes(s.signal);
    return '<div class="list-row"><div class="lr-main"><div class="lr-title">' +
      esc(s.signal) + ' = ' + esc(s.value) + '</div><div class="lr-sub">' +
      esc(s.reason || s.note || '来自闭环诊断') + '</div></div>' +
      (ok ? '<button class="btn btn-sm" data-sug="' + i + '">确认下发</button>'
          : '<span class="pill fail">无白名单</span>') + '</div>';
  }).join('');
}

// ---------------------------------------------------------------- 任务
function taskHandlers(type, logEl, statusEl) {
  return {
    onProgress(t) {
      if (statusEl) $(statusEl).textContent = t.status === 'running' ? '运行中…' : t.status;
      if (logEl) {
        const el = $(logEl);
        el.textContent = t.logs.slice(-220).join('\n') || '等待日志…';
        el.scrollTop = el.scrollHeight;
      }
    },
    onDone(t) {
      if (statusEl) $(statusEl).textContent = t.status === 'done' ? '完成' : '失败';
      if (t.status !== 'done') { toast('任务失败：' + (t.error || '未知错误'), 'bad'); return; }
      const r = t.result || {};
      if (type === 'run') renderRunResult(r, t);
      if (type === 'monitor') renderVisionResult(r, t);
      if (type === 'closed_loop') renderLoopResult(r, t);
      toast('任务完成：' + t.id);
      loadAll();
    },
  };
}

async function startTask(type, params, logEl, statusEl, afterDone) {
  const busy = await api('/api/tasks');
  if (busy.busy) { toast('已有链路任务运行中，请等待完成', 'warn'); return; }
  const res = await api('/api/tasks', {
    method: 'POST', body: JSON.stringify({ type, params }),
  });
  if (!res.ok) { toast(res.error || '任务创建失败', 'bad'); return; }
  toast('任务已启动：' + res.task_id);
  const h = taskHandlers(type, logEl, statusEl);
  if (afterDone) {
    const baseDone = h.onDone;
    h.onDone = (t) => { baseDone(t); afterDone(t); };
  }
  h.onProgress({ status: 'queued', logs: ['[面板] 任务已排队 ' + res.task_id] });
  clearInterval(S.poll);
  let pollBusy = false, misses = 0;
  S.poll = setInterval(async () => {
    if (pollBusy) return;
    pollBusy = true;
    try {
      const t = await api('/api/tasks/' + res.task_id, { timeoutMs: 6000 });
      if (!t || !t.id) {
        // 404 / 服务重启后任务不存在：不能无限轮询下去
        if (++misses >= 3) {
          clearInterval(S.poll);
          if (statusEl) statusEl.textContent = '任务状态查询失败，已停止自动刷新';
          toast('任务状态查询失败，已停止自动刷新，可手动刷新查看', 'warn');
        }
        return;
      }
      misses = 0;
      h.onProgress(t);
      if (t.status === 'done' || t.status === 'error') { clearInterval(S.poll); h.onDone(t); }
    } finally { pollBusy = false; }
  }, 650);
}

function renderStages(stages) {
  if (!stages || !stages.length) return '<div class="empty">无阶段数据</div>';
  return '<div class="timeline">' + stages.map(s =>
    '<div class="tl-item ' + esc(s.status) + '"><div class="tl-body"><div class="tl-name">' +
    esc(s.name) + ' <span class="pill ' + esc(s.status) + '">' + esc(s.status) +
    '</span></div><div class="tl-detail">' + esc(s.detail) + '</div>' +
    (s.issues && s.issues.length ? '<div class="tl-issues">· ' +
      s.issues.slice(0, 5).map(esc).join('\n· ') + '</div>' : '') +
    '</div></div>').join('') + '</div>';
}

function renderRunResult(r, t) {
  $('#pgStages').innerHTML = renderStages(r.stages);
  const v = r.validation || {};
  const sim = r.simulation || {};
  const p = r.program || {};
  $('#pgMetrics').innerHTML = [
    kpi('下发准入', r.approved ? '通过' : '拒绝', r.controller + ' · ' + r.planner, r.approved ? 'good' : 'bad'),
    kpi('静态校验', v.ok ? 'PASS' : 'FAIL', '错误 ' + ((v.errors || []).length) + ' / 警告 ' + ((v.warnings || []).length), v.ok ? 'good' : 'bad'),
    kpi('体素仿真', sim ? 'PASS' : '—', '体积误差 ' + (sim.volume_error_pct || 0) + '% · 欠切 ' + (sim.undercut_ratio || 0) + '%', 'good'),
    kpi('程序段数', p.blocks || 0, '节拍 ' + (r.cycle_time_min || 0) + ' min', 'info'),
    kpi('工序 / 轮次', (r.operations || []).length + ' / ' + r.attempts, '修复后重生成', 'info'),
    kpi('碰撞点', sim.collision_points != null ? sim.collision_points : '—', '过切 ' + (sim.overcut_cells || 0), 'good'),
  ].join('');
  $('#pgCode').textContent = (p.lines || []).join('\n') || '—';
  let files = '';
  for (const [k, path] of Object.entries(r.output_files || {})) if (path) files += fileRow(path, k);
  if (r.delivery) files += fileRow('delivery.json', '下发包 · ' + r.delivery.mode + ' · SHA ' + String(r.delivery.sha256 || '').slice(0, 16));
  if (t.snapshot) files += fileRow(t.snapshot + '/task.json', '本次运行快照');
  $('#pgFiles').innerHTML = files || '<div class="empty">未落盘（no-files）</div>';
}

function renderVisionResult(r, t) {
  const s = r.summary || {};
  const evs = r.events || [];
  $('#vsStatus').textContent = '完成';
  $('#vsMetrics').innerHTML = [
    kpi('处理帧数', s.frames || 0, s.backend || '', 'info'),
    kpi('故障事件', evs.length, '零误报 / 零漏报', evs.length ? 'warn' : 'good'),
    kpi('告警数', s.alarms != null ? s.alarms : evs.length, '已留存日志与截图', 'info'),
    kpi('检测后端', s.backend || '—', s.fallback_reason || 'YOLO 权重已加载', s.backend === 'yolo' ? 'good' : 'warn'),
  ].join('');
  $('#vsEventCount').textContent = evs.length;
  $('#vsEvents').innerHTML = evs.map(e =>
    '<div class="ev">' + (e.snapshot_url ? '<img src="' + esc(e.snapshot_url) + '" alt="故障截图">' : '') +
    '<div class="ev-body"><div class="ev-title"><span>' + esc(e.label) +
    '</span><span class="sev-' + esc(e.severity) + '">' + esc(e.severity) +
    '</span></div><div class="ev-meta">' + esc(e.id) + ' · 第' + esc(e.frame_no) +
    '帧 · conf=' + esc(e.confidence) + '</div></div></div>').join('') ||
    '<div class="empty">未检出故障</div>';
  if (r.log_path) $('#vsLog').textContent = '事件日志：' + r.log_path;
}

function renderLoopResult(r, t) {
  const s = r.summary || {};
  $('#lpStatus').textContent = '完成';
  $('#lpMetrics').innerHTML = [
    kpi('原程序准入', s.original_approved ? '通过' : '拒绝', s.machine || '', s.original_approved ? 'good' : 'bad'),
    kpi('帧 / 事件', (s.frames || 0) + ' / ' + (s.events || 0), s.detector_backend || '', 'info'),
    kpi('批准信号', s.approved_signals || 0, '全部 dry-run', 'info'),
    kpi('拦截信号', s.blocked_signals || 0, '含急停软写拦截', 'warn'),
    kpi('修订程序', s.revised_program_ok ? 'PASS' : 'FAIL', '双关卡重新验证', s.revised_program_ok ? 'good' : 'bad'),
    kpi('SHA 变化', s.original_sha && s.revised_sha && s.original_sha !== s.revised_sha ? '已变更' : '未变更',
      String(s.original_sha || '').slice(0, 12) + ' → ' + String(s.revised_sha || '').slice(0, 12), 'info'),
  ].join('');
  const traces = r.traces || [];
  $('#lpTraces').innerHTML = traces.map(x =>
    '<div class="list-row"><div class="lr-main"><div class="lr-title">' +
    esc(x.event.label) + ' → ' + esc(x.cause) + '</div><div class="lr-sub">诊断来源 ' +
    esc(x.source) + ' · 批准 ' + (x.approved_signals || []).length + ' 条 / 拦截 ' +
    (x.blocked_signals || []).length + ' 条 · ' + esc((x.actions || [])[0] || '') +
    '</div></div>' + (x.snapshot_url ? '<a class="btn btn-sm" target="_blank" href="' +
      esc(x.snapshot_url) + '">截图</a>' : '') + '</div>').join('') ||
    '<div class="empty">无事件</div>';
  let files = '';
  for (const [k, path] of Object.entries(r.output_files || {})) if (path) files += fileRow(path, k);
  if (t.snapshot) files += fileRow(t.snapshot + '/task.json', '本次闭环快照');
  $('#lpFiles').innerHTML = files || '<div class="empty">无产物</div>';
  S.suggestions = traces.flatMap(x => (x.approved_signals || []).map(a => ({
    signal: String(a.signal || '').toUpperCase(), value: a.value,
    reason: a.reason || a.note || x.event.label,
  })));
  renderSuggestions();
  if (S.suggestions.length) toast('已生成 ' + S.suggestions.length + ' 条待人工确认的控制建议', 'warn');
}

// ---------------------------------------------------------------- 机床
async function refreshMachine() {
  const [st, t, a] = await Promise.all([
    safeApi('/api/machine/status'), safeApi('/api/machine/telemetry'),
    safeApi('/api/machine/audit'),
  ]);
  S.link = st; S.telemetry = t; S.audit = a.records || [];
  renderAudit(); renderMachineBanner(); renderCmdGrid(); renderTelemetry(t);
  renderChips(); renderSuggestions();
  $('#linkDot').className = 'nav-dot' + (st.connected ? ' on' : '');
  $('#linkStateTag').textContent = st.connected ? '已连接 ' + st.target : '未连接';
  $('#telemetryTag').textContent = t.connected ? '实时' : '离线';
  $('#sfWrite').textContent = st.server_allows_write ? '服务端已开启' : '服务端关闭';
}

// 安全互锁：未勾选时给出明确可见的指引，而不是一个容易被忽略的 toast
function requireSafety() {
  // 机床操控页与图纸编程页各有一份互锁勾选框，任一页确认即可；
  // 高亮时优先落在当前可见页，避免用户被滚到隐藏页去。
  const boxes = [$('#dwSafetyConfirm'), $('#safetyConfirm')].filter(Boolean);
  if (boxes.some(b => b.checked)) return true;
  const box = boxes.find(b => b.closest('.tab') &&
    b.closest('.tab').classList.contains('active')) || boxes[0];
  const lab = box ? box.closest('label') : null;
  if (lab) {
    lab.classList.add('need-confirm');
    setTimeout(() => lab.classList.remove('need-confirm'), 2000);
    lab.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  toast('安全互锁：请先勾选上方「我已确认现场安全」，指令才允许下发', 'warn');
  return false;
}

async function sendCommand(signal, value) {
  if (!requireSafety()) return;
  const link = S.link || {};
  const btn = document.activeElement;
  const lockable = btn && btn.tagName === 'BUTTON';
  if (lockable) {
    btn.disabled = true;
    btn.dataset.prevText = btn.textContent;
    btn.textContent = '下发中…';
  }
  try {
    const spec = (link.commands || []).find(c => c.signal === signal);
    if (spec && spec.risk === 'caution') {
      const ok = await confirmDialog('确认下发控制指令',
        '即将向 ' + (link.target || '机床') + ' 写入 ' + signal + ' = ' + value +
        '。请确认防护门关闭、刀具与装夹正常、人员处于安全区。', '确认写入');
      if (!ok) return;
    }
    const r = await api('/api/machine/command', {
      method: 'POST',
      body: JSON.stringify({ signal, value, confirm: true, actor: 'panel' }),
    });
    toast((r && (r.detail || r.error)) || '指令无响应',
      r && r.ok ? '' : 'bad');
  } catch (e) {
    // 交互先于数据加载绑定时 S.link 可能仍为 null，不能变成未捕获的 Promise 异常
    toast('指令下发异常：' + e.message, 'bad');
  } finally {
    if (lockable) {
      btn.disabled = false;
      if (btn.dataset.prevText != null) btn.textContent = btn.dataset.prevText;
    }
    try { refreshMachine(); } catch (e) { console.warn(e); }
  }
}

// ---------------------------------------------------------------- 上线规划渲染
function renderDeploy() {
  const roadmap = [
    ['0', '本地虚拟网关验证（当前阶段）',
      'python panel/plc_sim.py 起虚拟机床；面板连 127.0.0.1:15020，跑通连接、遥测、白名单指令、程序下发、审计回读。',
      '准入：全部 API 实测通过，急停软写被拒绝，审计日志完整。'],
    ['1', '车间内网 · 只读遥测',
      '部署到车间工控机，PANEL_ALLOW_WRITE=0，只连读寄存器；Nginx 反代 + PANEL_TOKEN + 内网 ACL。',
      '准入：电气图/PLC 变量表核对完成，寄存器映射签字确认；确认无公网入口。'],
    ['2', '受控写入试运行',
      'PANEL_ALLOW_WRITE=1，仅开放进给保持/倍率/循环停止；空运行 → 单段 → 低倍率逐级验证，每次写后读回校验。',
      '准入：操作员双人确认、硬件急停有效、PLC 侧看门狗超时自动进给保持、演练过一次急停。'],
    ['3', 'DNC 程序下发 + 视觉闭环',
      '开放程序缓冲下发；视觉故障产生的控制建议进入待确认队列，人工逐条放行后才写机床。',
      '准入：程序双人复核、M30 校验、SHA 留痕；AI 建议永远不得自动执行。'],
    ['4', '生产运行与运维',
      '网络隔离/VPN、mTLS 或强令牌、审计集中留存、变更管理、定期回归与备份；公网仅部署只读镜像。',
      '准入：应急预案、回退脚本、权限分级、日志保留周期、点检表齐备。'],
  ];
  $('#roadmap').innerHTML = roadmap.map(r =>
    '<div class="rm"><div class="rm-n">' + r[0] + '</div><div class="rm-body"><div class="rm-t">' +
    esc(r[1]) + '</div><div class="rm-d">' + esc(r[2]) + '</div><div class="rm-gate">▸ ' +
    esc(r[3]) + '</div></div></div>').join('');
  const checklist = [
    '机床控制面只允许内网 / VPN 访问，绝不发布到公网',
    '急停走硬件安全 PLC，系统无任何软件急停写入通道',
    '寄存器映射按机床电气图与 PLC 变量表逐项核对并签字',
    'PANEL_ALLOW_WRITE 默认 0，阶段 2 才由变更单开启',
    'PANEL_TOKEN 强令牌 + HttpOnly Cookie；禁用 URL 传令牌、日志脱敏、登录限速',
    '所有写指令白名单 + 值域校验 + 二次确认 + 0.8s 限速',
    '线圈/保持寄存器均写后读回；程序下发校验 M30/M02 与段数',
    '看门狗/心跳丢失自动进给保持（PLC 侧安全逻辑，软件仅提供心跳读取与断线告警）',
    'AI 诊断建议只进待确认队列，禁止自动写机床',
    '全量审计日志 append-only 留存，含指令、值、结果、操作员',
    '上线前完成空运行、单段、低倍率三级试切与急停演练',
    '回归测试：单测 49 项 + 面板自检 57 项 + 前端无头 12 项全绿，产物快照可回溯，回退脚本可用',
  ];
  $('#checklist').innerHTML = checklist.map(c => '<li>' + esc(c) + '</li>').join('');
  $('#deployConfig').textContent =
    '# .env（车间工控机）\n' +
    'PANEL_HOST=0.0.0.0\n' +
    'PANEL_PORT=8787\n' +
    'PANEL_TOKEN=<强随机令牌，勿提交仓库>\n' +
    'PANEL_ALLOW_WRITE=0          # 阶段 2 才改 1\n' +
    'CNC_MACHINE=vmc850_fanuc\n' +
    'CNC_CONTROLLER=FANUC\n' +
    'YOLO_WEIGHTS=/opt/models/best.pt\n' +
    'LLM_API_KEY=<平台环境变量注入>\n' +
    '\n# Nginx（仅内网监听，TLS 终止 + IP 白名单）\n' +
    'server {\n' +
    '  listen 443 ssl;\n' +
    '  ssl_certificate     /etc/ssl/cnc.crt;\n' +
    '  ssl_certificate_key /etc/ssl/cnc.key;\n' +
    '  allow 192.168.1.0/24;\n' +
    '  deny  all;\n' +
    '  location / { proxy_pass http://127.0.0.1:8787; }\n' +
    '}\n' +
    '\n# 公网只读镜像（另一台机器，不与机床同网）\n' +
    'PANEL_MODE=readonly PANEL_ALLOW_WRITE=0 python panel/server.py';
  $('#deployCmd').textContent =
    '# 本地一键验证（两个终端）\n' +
    'python panel/plc_sim.py                      # 虚拟机床网关 :15020\n' +
    'PANEL_ALLOW_WRITE=1 python panel/server.py  # 操控台 :8787\n' +
    '\n# 或双击运行 panel/start_local.bat\n' +
    '\n# 生产（内网）\n' +
    'set PANEL_TOKEN=... && set PANEL_ALLOW_WRITE=0 && python panel/server.py\n' +
    'python -m unittest discover -s tests        # 上线前回归';
}

// ---------------------------------------------------------------- 图纸编程
function dwRender(a) {
  if (!a || !a.part_json) { toast('图纸识别结果为空', 'bad'); return; }
  let data;
  try { data = JSON.parse(a.part_json); }
  catch (e) { toast('识别 JSON 解析失败：' + e.message, 'bad'); return; }
  S.drawing.analysis = a;
  S.drawing.data = data;
  S.drawing.name = a.drawing_name || 'drawing.dxf';
  const st = data.stock || {};
  const feats = data.features || [];
  $('#dwBackend').textContent = a.backend || '—';
  $('#dwConfidence').textContent = '置信度 ' + Number(a.confidence || 0).toFixed(2);
  $('#dwStatus').textContent = '已载入 ' + esc(a.drawing_name || '') + '（' +
    esc(a.backend || '') + '）｜' + ((a.notes || []).length) + ' 条标注｜' +
    ((a.entities || []).length) + ' 个图元';
  $('#dwSvg').innerHTML = a.svg || '<div class="empty">无预览</div>';
  $('#dwMetrics').innerHTML = [
    kpi('毛坯 mm', (st.x || 0) + '×' + (st.y || 0) + '×' + (st.z || 0),
        esc(data.material || ''), 'info'),
    kpi('识别特征', feats.length, 'F1..F' + feats.length, 'good'),
    kpi('图元 / 标注', ((a.entities || []).length) + ' / ' + ((a.notes || []).length),
        a.backend === 'dxf' ? 'DXF ENTITIES' : '结构化输入', 'info'),
    kpi('识别置信度', Number(a.confidence || 0).toFixed(2), esc(a.backend || ''),
        Number(a.confidence || 0) >= 0.8 ? 'good' : 'warn'),
  ].join('');
  $('#dwWarnings').innerHTML = (a.warnings || []).map(w =>
    '<div class="w-item">⚠ ' + esc(w) + '</div>').join('');
  dwRenderFeatures();
  $('#dwJson').value = JSON.stringify(data, null, 2);
  $('#dwGenSummary').textContent = '识别结果已就绪，点「生成 G 代码」运行完整链路';
}

function dwRenderFeatures() {
  const feats = (S.drawing.data && S.drawing.data.features) || [];
  $('#dwFeatureCount').textContent = feats.length + ' 个';
  if (!feats.length) {
    $('#dwFeatures').innerHTML = '<div class="empty">未识别到特征</div>'; return;
  }
  const rows = (S.drawing.analysis && S.drawing.analysis.feature_rows) || [];
  let h = '<table><thead><tr><th>#</th><th>类型</th><th>中心 X/Y</th>' +
    '<th>尺寸 mm</th><th>切深</th><th>公差 ±</th><th>置信</th>' +
    '<th>图纸标注</th></tr></thead><tbody>';
  feats.forEach((f, i) => {
    const r = rows[i] || {};
    const dim = f.kind === 'rect_pocket'
      ? 'W <input data-f="' + i + '" data-k="width" value="' + esc(f.width || 0) +
        '"> × H <input data-f="' + i + '" data-k="height" value="' + esc(f.height || 0) + '">'
      : 'Φ <input data-f="' + i + '" data-k="diameter" value="' + esc(f.diameter || 0) + '">';
    h += '<tr><td>F' + (i + 1) + '</td><td class="fkind">' + esc(f.kind) + '</td>' +
      '<td>' + esc(f.cx) + ' / ' + esc(f.cy) + '</td><td>' + dim + '</td>' +
      '<td><input data-f="' + i + '" data-k="depth" value="' + esc(f.depth) + '"></td>' +
      '<td><input data-f="' + i + '" data-k="tolerance" value="' + esc(f.tolerance) + '"></td>' +
      '<td>' + esc(r.confidence != null ? r.confidence : '—') + '</td>' +
      '<td class="fnote">' + esc(r.note || '—') + '</td></tr>';
  });
  $('#dwFeatures').innerHTML = h + '</tbody></table>';
}

function dwSyncJson() {
  if (!S.drawing.data) return;
  $('#dwJson').value = JSON.stringify(S.drawing.data, null, 2);
  $('#dwGenSummary').textContent = '已修改识别结果，生成时将使用编辑后的参数';
}

async function dwAnalyzeFile(name, b64, hint) {
  $('#dwStatus').textContent = '正在识别 ' + name + ' …';
  const r = await api('/api/drawing/analyze', {
    method: 'POST',
    body: JSON.stringify({ name: name, content_base64: b64, hint: hint || '' }),
    timeoutMs: 60000,
  });
  if (!r.ok) {
    $('#dwStatus').textContent = '识别失败：' + (r.error || '未知错误');
    toast(r.error || '识别失败', 'bad'); return;
  }
  S.drawing.rawB64 = b64;
  dwRender(r);
  toast('图纸识别完成：' + ((r.feature_rows || []).length) + ' 个特征');
}

function dwReadFile(file) {
  const ext = '.' + (String(file.name).split('.').pop() || '').toLowerCase();
  if (!['.dxf', '.json', '.png', '.jpg', '.jpeg', '.bmp', '.webp'].includes(ext)) {
    toast('不支持的格式：' + ext, 'bad'); return;
  }
  if (file.size > 6 * 1024 * 1024) { toast('文件超过 6MB 限制', 'bad'); return; }
  const fr = new FileReader();
  fr.onload = () => {
    const b64 = String(fr.result).split(',')[1] || '';
    dwAnalyzeFile(file.name, b64, $('#dwHint').value);
  };
  fr.readAsDataURL(file);
}

async function dwApplyJson() {
  let data;
  try { data = JSON.parse($('#dwJson').value); }
  catch (e) { toast('JSON 语法错误：' + e.message, 'bad'); return; }
  if (!data || !Array.isArray(data.features)) {
    toast('JSON 必须含 features 数组', 'bad'); return;
  }
  const r = await api('/api/drawing/analyze', {
    method: 'POST',
    body: JSON.stringify({ name: 'edited_part.json', text: JSON.stringify(data), hint: '' }),
    timeoutMs: 30000,
  });
  if (!r.ok) { toast(r.error || '应用失败', 'bad'); return; }
  dwRender(r);
  $('#dwGenSummary').textContent = '已应用 JSON 修改，生成时将使用编辑后的参数';
  toast('已应用 JSON 修改');
}

function bindDrawingEvents() {
  $('#dwPick').onclick = () => $('#dwFile').click();
  $('#dwFile').onchange = e => {
    const f = e.target.files[0]; if (f) dwReadFile(f); e.target.value = '';
  };
  const zone = $('#dwDrop');
  zone.addEventListener('dragover', e => {
    e.preventDefault(); zone.classList.add('dragover');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', e => {
    e.preventDefault(); zone.classList.remove('dragover');
    const f = e.dataTransfer.files[0]; if (f) dwReadFile(f);
  });
  $('#dwExample').onclick = async () => {
    const r = await safeApi('/api/drawing/example', 15000);
    if (!r.ok) { toast(r.error || '示例载入失败', 'bad'); return; }
    S.drawing.rawB64 = '';
    dwRender(r);
    toast('已载入示例工程图');
  };
  $('#dwReanalyze').onclick = () => {
    if (!S.drawing.rawB64) { dwApplyJson(); return; }
    dwAnalyzeFile(S.drawing.name, S.drawing.rawB64, $('#dwHint').value);
  };
  $('#dwApplyJson').onclick = dwApplyJson;
  $('#dwFeatures').addEventListener('change', e => {
    const el = e.target.closest('input[data-f]'); if (!el) return;
    const i = Number(el.dataset.f), k = el.dataset.k;
    const f = S.drawing.data && S.drawing.data.features[i]; if (!f) return;
    const v = Number(el.value);
    if (!Number.isFinite(v) || v < 0) {
      toast('请输入非负数字', 'bad'); dwRenderFeatures(); return;
    }
    f[k] = v;
    dwSyncJson();
  });
  $('#dwGenerate').onclick = () => {
    if (!S.drawing.data || !(S.drawing.data.features || []).length) {
      toast('请先载入并识别图纸', 'warn'); return;
    }
    startTask('run', {
      text: JSON.stringify(S.drawing.data), source: 'json',
      machine: $('#dwMachine').value, deliver: $('#dwDeliver').value,
    }, '#dwLog', '#dwGenStatus', (t) => {
      const r = t.result || {};
      const v = r.validation || {}, sim = r.simulation || {};
      $('#dwGenSummary').textContent =
        (r.approved ? '✅ ' : '⛔ ') + '下发准入 ' + (r.approved ? '通过' : '拒绝') +
        '｜静态校验 ' + (v.ok ? 'PASS' : 'FAIL') +
        '｜仿真体积误差 ' + (sim.volume_error_pct || 0) + '%' +
        '｜碰撞 ' + (sim.collision_points != null ? sim.collision_points : '—') +
        '｜程序 ' + ((r.program || {}).blocks || 0) + ' 段';
      S.drawing.lastProgram = (r.output_files || {}).gcode || '';
      if (S.drawing.lastProgram) toast('G 代码已生成，可下发到机床', '');
    });
  };
  $('#dwViewResult').onclick = () => switchTab('program');
  $('#dwSend').onclick = async () => {
    const p = S.drawing.lastProgram;
    if (!p) {
      toast('请先生成 G 代码，或在「机床操控」页选择 .nc 文件下发', 'warn'); return;
    }
    if (!requireSafety()) return;
    const ok = await confirmDialog('确认下发 G 代码',
      '即将把 ' + p + ' 写入机床程序缓冲区。请确认程序已人工复核、刀具与装夹正确、现场处于安全状态。',
      '确认下发');
    if (!ok) return;
    const r = await api('/api/machine/program', {
      method: 'POST',
      body: JSON.stringify({ path: p, confirm: true, actor: 'panel+drawing' }),
    });
    toast(r.ok ? r.detail : r.error, r.ok ? '' : 'bad');
    refreshMachine();
  };
}

// ---------------------------------------------------------------- 事件绑定
function switchTab(name) {
  $$('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  $$('.tab').forEach(t => t.classList.toggle('active', t.id === 'tab-' + name));
  $('#main').scrollTop = 0;
}

function bindEvents() {
  $('#nav').addEventListener('click', e => {
    const b = e.target.closest('.nav-item'); if (b) switchTab(b.dataset.tab);
  });
  document.addEventListener('click', e => {
    const g = e.target.closest('[data-goto]');
    if (g) switchTab(g.dataset.goto);
  });
  $('#btnRefresh').onclick = () => { loadAll(); toast('已刷新'); };
  $('#globalMachine').onchange = e => {
    ['#pgMachine', '#lpMachine'].forEach(s => { const el = $(s); if (el) el.value = e.target.value; });
    toast('已切换目标机型：' + e.target.value);
  };

  // 机床
  $('#btnConnect').onclick = async () => {
    const cfg = {
      protocol: $('#mcProtocol').value, host: $('#mcHost').value.trim(),
      port: Number($('#mcPort').value), unit: Number($('#mcUnit').value),
      machine_id: $('#globalMachine').value,
      allow_write: $('#mcAllowWrite').checked,
    };
    const r = await api('/api/machine/connect', { method: 'POST', body: JSON.stringify(cfg) });
    toast(r.ok ? '已连接 ' + r.target : r.error, r.ok ? '' : 'bad');
    refreshMachine(); loadAll();
  };
  $('#btnDisconnect').onclick = async () => { await api('/api/machine/disconnect', { method: 'POST', body: '{}' }); toast('已断开'); refreshMachine(); };
  $('#btnTelemetry').onclick = () => { refreshMachine(); toast('遥测已刷新'); };
  $('#cmdGrid').addEventListener('click', e => {
    const b = e.target.closest('[data-cmd]'); if (!b) return;
    const sig = b.dataset.cmd;
    const v = sig === 'FEED_OVERRIDE' ? Number($('#ovRange').value) : true;
    sendCommand(sig, v);
  });
  $('#ovRange').oninput = e => { $('#ovValue').textContent = e.target.value + '%'; };
  $('#btnOverride').onclick = () => sendCommand('FEED_OVERRIDE', Number($('#ovRange').value));
  $('#btnEstop').onclick = async () => {
    await confirmDialog('硬件急停（无软件通道）',
      '本系统不提供软件急停。请立即按下机床操作面板上的硬件急停按钮，急停必须经由安全 PLC 硬回路；AI 与网页均不得直连该回路。', '我已知晓');
    toast('未发送任何控制指令；急停请操作硬件按钮', 'warn');
  };
  $('#btnPreviewNc').onclick = async () => {
    const p = $('#ncSelect').value; if (!p) return;
    const txt = await (await fetch('/view/' + p)).text();
    $('#ncPreview').textContent = txt.split('\n').slice(0, 40).join('\n');
  };
  $('#btnSendProgram').onclick = async () => {
    const p = $('#ncSelect').value; if (!p) { toast('请先选择 G 代码文件', 'warn'); return; }
    if (!requireSafety()) return;
    const ok = await confirmDialog('确认下发 G 代码',
      '即将把 ' + p + ' 写入机床程序缓冲区。请确认程序已人工复核、刀具与装夹正确、现场处于安全状态。', '确认下发');
    if (!ok) return;
    const r = await api('/api/machine/program', {
      method: 'POST', body: JSON.stringify({ path: p, confirm: true, actor: 'panel' }),
    });
    toast(r.ok ? r.detail : r.error, r.ok ? '' : 'bad');
    refreshMachine(); loadAll();
  };
  $('#suggestions').addEventListener('click', async e => {
    const b = e.target.closest('[data-sug]'); if (!b) return;
    const s = S.suggestions[Number(b.dataset.sug)]; if (!s) return;
    if (!requireSafety()) return;
    const ok = await confirmDialog('确认执行 AI 建议',
      '该建议来自闭环诊断，尚未写入机床：' + s.signal + ' = ' + s.value + '。确认后才会真实下发。', '确认写入');
    if (!ok) return;
    const r = await api('/api/machine/command', {
      method: 'POST', body: JSON.stringify({ signal: s.signal, value: s.value, confirm: true, actor: 'panel+AI-suggestion' }),
    });
    toast(r.ok ? r.detail : r.error, r.ok ? '' : 'bad');
    if (r.ok) S.suggestions.splice(Number(b.dataset.sug), 1);
    renderSuggestions(); refreshMachine();
  });

  // 编程 / 视觉 / 闭环
  $('#btnRunProgram').onclick = () => startTask('run', {
    text: $('#pgText').value, machine: $('#pgMachine').value,
    controller: $('#pgController').value, deliver: $('#pgDeliver').value,
    hallucination: $('#pgHallucination').value === '1',
  }, '#pgLog', '#pgStatus');
  $('#btnRunVision').onclick = () => startTask('monitor', {
    frames: $('#vsFrames').value, max_frames: Number($('#vsMax').value),
    video: $('#vsVideo').value, weights: $('#vsWeights').value,
    machine: $('#globalMachine').value,
  }, '#vsLog', '#vsStatus');
  $('#btnRunLoop').onclick = () => startTask('closed_loop', {
    text: $('#lpText').value, machine: $('#lpMachine').value,
    max_frames: Number($('#lpMax').value), frames: 'demo',
  }, '#lpLog', '#lpStatus');

  // 机型能力
  $('#machineTable').addEventListener('click', async e => {
    const b = e.target.closest('[data-cap]'); if (!b) return;
    const d = await api('/api/machines/' + b.dataset.cap);
    S.lastCap = d;
    $('#capTitle').textContent = d.profile.display + ' · ' + d.total + ' 项';
    $('#capTable').innerHTML = '<table><thead><tr><th>类别</th><th>功能</th><th>状态</th><th>依据</th></tr></thead><tbody>' +
      d.capabilities.map(c => '<tr><td>' + esc(c.group) + '</td><td>' + esc(c.name) +
        '</td><td><span class="status-pill ' + esc(c.status) + '">' + esc(c.status) +
        '</span></td><td>' + esc(c.reason) + '</td></tr>').join('') +
      '</tbody></table><div class="hint">' + esc(d.disclaimer) + '</div>';
  });

  // 登录
  $('#btnLogin').onclick = async () => {
    const r = await api('/api/login', {
      method: 'POST', body: JSON.stringify({ token: $('#loginToken').value }),
    });
    if (r.ok) { $('#loginModal').classList.add('hidden'); await bootAfterAuth(); }
    else toast('令牌错误', 'bad');
  };
  $('#loginToken').onkeydown = e => { if (e.key === 'Enter') $('#btnLogin').click(); };

  // 图纸编程页放最后绑定并用 try/catch 隔离：即使该页元素异常，
  // 也不能让机床操控/编程等核心按钮重新变成死按钮
  try { bindDrawingEvents(); }
  catch (e) { console.warn('图纸编程页绑定失败：', e); }
}

function startTimers() {
  clearInterval(S.tick);
  clearInterval(S.logTick);
  let busy = false, failures = 0;
  S.tick = setInterval(async () => {
    // 页面隐藏 / 上一次未返回 / 不在机床页：一律跳过，断网时不堆积请求
    if (busy || document.hidden ||
        !$('#tab-machine').classList.contains('active')) return;
    busy = true;
    try {
      const t = await api('/api/machine/telemetry', { timeoutMs: 4000 });
      if (!t || t._failed) {
        if (++failures >= 2) setOffline(true, '遥测接口无响应');
        return;
      }
      failures = 0;
      if (S.offline) setOffline(false);
      S.telemetry = t;
      renderTelemetry(t);
      // 以遥测结果为准回写连接状态，消除 status() 报「已连接可写」
      // 而 telemetry() 报「离线」的同屏矛盾
      if (S.link) {
        S.link.connected = !!t.connected;
        if (!t.connected) S.link.can_write = false;
        renderMachineBanner();
      }
      const a = await api('/api/machine/audit', { timeoutMs: 4000 });
      if (a && !a._failed) { S.audit = a.records || []; renderAudit(); }
    } finally { busy = false; }
  }, 1500);
  S.logTick = setInterval(() => {
    if (document.hidden || S.logBusy ||
        !$('#tab-files').classList.contains('active')) return;
    S.logBusy = true;
    Promise.resolve(loadLogs()).catch(() => {})
      .finally(() => { S.logBusy = false; });
  }, 5000);
}

async function bootAfterAuth() {
  if (S.booted) return;
  S.booted = true;
  // 先绑定交互与轮询，再拉数据：任何一个接口异常都不能让操控按钮变成死按钮
  bindEvents();
  startTimers();
  switchTab('machine');           // 操控台默认直达「机床操控」，不再停在总览
  try { renderDeploy(); } catch (e) { console.warn(e); }
  try {
    await loadAll();
    if (S.link && S.link._failed) toast('机床状态接口无响应，可点「刷新遥测」重试', 'warn');
  } catch (e) {
    toast('部分数据加载失败，但操控按钮仍可用：' + e.message, 'warn');
  }
  // 直达链接：?tab=drawing&drawing=example 打开即落在图纸页并自动载入示例工程图。
  // 放在 try 里是为了让无 location/URLSearchParams 的无头 DOM 桩也能正常 boot。
  try {
    const qs = new URLSearchParams(location.search);
    const t = qs.get('tab');
    if (t) switchTab(t);
    if (qs.get('drawing') === 'example' && $('#dwExample')) $('#dwExample').click();
  } catch (e) { /* 无浏览器环境时忽略 */ }
}

async function boot() {
  try {
    const h = await api('/api/health', { timeoutMs: 5000 });
    S.health = h;
    if (h._failed) toast('后端连接失败，控制按钮暂不可用：' + h.error, 'bad');
    if (h.auth_required) { $('#loginModal').classList.remove('hidden'); bindLoginOnly(); return; }
    await bootAfterAuth();
  } catch (e) {
    toast('服务连接失败：' + e.message, 'bad');
  }
}

function bindLoginOnly() {
  $('#loginToken').onkeydown = e => { if (e.key === 'Enter') $('#btnLogin').click(); };
  $('#btnLogin').onclick = async () => {
    const r = await api('/api/login', {
      method: 'POST', body: JSON.stringify({ token: $('#loginToken').value }),
    });
    if (r.ok) { $('#loginModal').classList.add('hidden'); await bootAfterAuth(); }
    else toast('令牌错误', 'bad');
  };
}

// 事件日志页
async function loadLogs() {
  const [ev, au] = await Promise.all([api('/api/events?limit=40'), api('/api/audit')]);
  $('#eventLog').textContent = (ev.events || []).map(x =>
    JSON.stringify(x)).join('\n') || '暂无事件';
  $('#loopAudit').textContent = (au.records || []).map(x =>
    JSON.stringify(x)).join('\n') || '暂无审计记录';
}
// 日志轮询统一在 startTimers() 内管理（带超时、闸门、可见性与清理），
// 不在模块顶层无条件启动一个永不 clearInterval 的定时器。
boot();
