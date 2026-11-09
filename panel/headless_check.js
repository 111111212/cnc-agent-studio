/* 前端可操作性无头验收：用最小 DOM 桩真实执行 app.js 的 boot 流程，
   走真实 HTTP 拉后端数据，验证操控按钮确实被绑定、指令网格确实渲染。
   不依赖 jsdom / puppeteer，只需 Node 18+。
   用法：node panel/headless_check.js [http://127.0.0.1:8787]
*/
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const BASE = process.argv[2] || 'http://127.0.0.1:8787';
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'panel/static/index.html'), 'utf8');
const appjs = fs.readFileSync(path.join(root, 'panel/static/app.js'), 'utf8');

class ClassList {
  constructor() { this.s = new Set(); }
  add(x) { this.s.add(x); }
  remove(x) { this.s.delete(x); }
  toggle(x, f) { f ? this.s.add(x) : this.s.delete(x); }
  contains(x) { return this.s.has(x); }
}
class El {
  constructor(id = '') {
    this.id = id; this.classList = new ClassList(); this.dataset = {};
    this.style = {}; this.checked = false; this.value = ''; this.textContent = '';
    this.innerHTML = ''; this.scrollTop = 0; this._ev = {}; this.onclick = null;
    this.onchange = null; this.oninput = null; this.onkeydown = null;
  }
  addEventListener(t, f) { this._ev[t] = f; }
  querySelector() { return new El('sub'); }
  querySelectorAll() { return []; }
  appendChild() {}
  remove() {}
  scrollIntoView() {}
  closest() { return new El('label'); }
}

const els = {};
for (const m of html.matchAll(/id="([A-Za-z0-9_-]+)"/g)) els[m[1]] = new El(m[1]);
const tabNames = [...html.matchAll(/data-tab="([a-z_-]+)"/g)].map(m => m[1]);
const navItems = tabNames.map(n => { const e = new El('nav-' + n); e.dataset.tab = n; return e; });
const tabSections = Object.values(els).filter(e => e.id.startsWith('tab-'));

const document = {
  querySelector(s) {
    if (s.startsWith('#')) return els[s.slice(1)] || null;
    return new El(s);
  },
  querySelectorAll(s) {
    if (s === '.nav-item') return navItems;
    if (s === '.tab') return tabSections;
    return [];
  },
  addEventListener() {},
  createElement() { return new El('created'); },
};

const netFetch = (p, o) => fetch(BASE + p, o);
const sandbox = {
  document, console, Date, Number, String, Object, Array, JSON, Math, Promise,
  Set, Map, RegExp, Error, isNaN, parseInt, parseFloat,
  AbortController, setTimeout, clearTimeout, setInterval, clearInterval,
  fetch: netFetch,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const results = [];
function check(name, ok, detail) {
  results.push(ok);
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ' | ' + detail : ''}`);
}

vm.runInContext(appjs, sandbox, { filename: 'app.js' });

setTimeout(async () => {
  console.log(`前端无头验收 (${BASE})`);
  // 主动点一次「载入示例工程图」：真实调用 /api/drawing/example 并渲染，
  // 证明图纸页不只是静态 HTML，而是确实能识别并出图
  try { await els.dwExample.onclick(); }
  catch (e) { console.log('  [warn] 示例图纸载入：' + e.message); }
  check('boot 完成（交互已绑定）', typeof els.btnConnect.onclick === 'function' &&
        typeof els.btnDisconnect.onclick === 'function');
  check('连接/断开/遥测按钮可用',
        ['btnConnect', 'btnDisconnect', 'btnTelemetry', 'btnOverride',
         'btnSendProgram', 'btnPreviewNc', 'btnEstop', 'btnRunProgram']
          .every(id => typeof els[id].onclick === 'function'));
  check('控制指令网格事件委托已绑定', typeof els.cmdGrid._ev.click === 'function');
  check('AI 建议队列事件委托已绑定', typeof els.suggestions._ev.click === 'function');
  check('默认落地「机床操控」页',
        els['tab-machine'].classList.contains('active') &&
        navItems.find(n => n.dataset.tab === 'machine').classList.contains('active'));
  const grid = String(els.cmdGrid.innerHTML);
  check('控制按钮已真实渲染', grid.includes('data-cmd="FEED_HOLD"') &&
        grid.includes('data-cmd="CYCLE_START"'), `${(grid.match(/data-cmd=/g) || []).length} 个按钮`);
  check('遥测面板已渲染数据', String(els.telemetry.innerHTML).includes('运行状态'));
  let S = {};
  try { S = vm.runInContext('S', sandbox); } catch (e) { /* 忽略 */ }
  check('后端数据已真实拉取', Array.isArray(S.machines) && S.machines.length > 1 &&
        Array.isArray(S.files) && S.files.length > 0 && !!S.link,
        `${(S.machines || []).length} 机型 / ${(S.files || []).length} 产物 / ` +
        `连接=${!!(S.link || {}).connected}`);
  check('任务列表接口未拖住 boot',
        !S.tasks || Array.isArray(S.tasks), `${(S.tasks || []).length} 条任务`);
  check('图纸编程页按钮已绑定',
        ['dwPick', 'dwExample', 'dwGenerate', 'dwSend', 'dwApplyJson',
         'dwReanalyze', 'dwViewResult'].every(id => typeof els[id].onclick === 'function'));
  check('示例工程图已识别并渲染 SVG + 特征表',
        String(els.dwSvg.innerHTML).includes('<svg') &&
        String(els.dwFeatures.innerHTML).includes('circle_pocket') &&
        String(els.dwFeatures.innerHTML).includes('rect_pocket'),
        `${(String(els.dwFeatures.innerHTML).match(/<tr>/g) || []).length} 行特征`);
  check('识别结果已填充可编辑 JSON',
        String(els.dwJson.value).includes('"features"') &&
        String(els.dwJson.value).includes('drawing_plate'),
        `${String(els.dwJson.value).length} 字符`);
  const failed = results.filter(x => !x).length;
  console.log(`\n结果：${results.length - failed}/${results.length} PASS`);
  process.exit(failed ? 1 : 0);
}, 2500);
