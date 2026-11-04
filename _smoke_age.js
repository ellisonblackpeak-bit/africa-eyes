/* 前端年龄闸门的端到端检查。
 *
 * 造一份「3 条新鲜 + 2 条超期」的数据喂给 index.html，看渲染出来的卡片
 * 到底有几张。build_data.py 那边已经拦了一层，这里验的是第二层：
 * 手机挂机不刷新时，页面自己会不会把过期卡片去掉。
 *
 * 用法： NODE_PATH=... node _smoke_age.js   期望「通过」。
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const here = __dirname;
const html = fs.readFileSync(path.join(here, 'index.html'), 'utf8');
const NOW = Date.now() / 1000;
const D = 86400;

function mk(id, daysAgo, titleZh) {
  return {
    url: 'https://example.com/' + id,
    title: 'Headline ' + id,
    title_zh: titleZh,
    summary_zh: '这是一段用于测试的中文摘要，长度足够显示。',
    snippet_zh: '这是一段用于测试的中文摘要。',
    body: 'Body text for testing purposes only.',
    body_zh: '这是一段用于测试的中文正文。',
    bodyLevel: 'full',
    published: new Date((NOW - daysAgo * D) * 1000).toISOString(),
    _dt: NOW - daysAgo * D,
    ageDays: daysAgo,
    country: ['ng'],
    tier: 1,
    cnHit: false,
    usHit: false,
    terrHit: false,
    riskHit: false,
    topic: ['政治'],
    score: 10,
    source: 'Test Wire',
    domain: 'example.com',
    tag: 'ng_extra',
    actor: '',
  };
}

const FRESH = [
  mk('a', 0.5, '新鲜条目甲：尼日利亚与中国签署港口协议'),
  mk('b', 1.5, '新鲜条目乙：加纳通过新的矿业税收法案'),
  mk('c', 2.5, '新鲜条目丙：马里军方宣布北部安全行动'),
];
const STALE = [
  mk('d', 4.0, '过期条目丁：四天前的燃油短缺报道'),
  mk('e', 10.0, '过期条目戊：十天前的油井井喷'),
];

const payload = {
  generatedAt: new Date().toISOString(),
  appName: 'China Eyes',
  window: '近 3 日',
  windowDays: 3,
  news: FRESH.concat(STALE),
  social: [],
  people: [],
  sources: [],
  topics: [],
  stats: {},
};
const dataJs = 'window.SITE_DATA = ' + JSON.stringify(payload) + ';';

const vc = new VirtualConsole();
const errs = [];
vc.on('jsdomError', e => errs.push(String(e.message).slice(0, 120)));

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://localhost/index.html',
  virtualConsole: vc,
  beforeParse(win) {
    const ctx = new Proxy({}, {
      get: (t, k) => {
        if (k === 'canvas') return { width: 360, height: 360 };
        if (k === 'createLinearGradient' || k === 'createRadialGradient')
          return () => ({ addColorStop() {} });
        if (k === 'measureText') return () => ({ width: 10 });
        return () => {};
      },
      set: () => true
    });
    win.HTMLCanvasElement.prototype.getContext = () => ctx;
    win.matchMedia = win.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {} }));
    win.speechSynthesis = { speak() {}, cancel() {}, getVoices: () => [] };
    win.SpeechSynthesisUtterance = function () {};
    win.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 16);
    win.eval(dataJs);
    win.localStorage.setItem('ce.gate.v1', 'f3837c0d');
  }
});

setTimeout(() => {
  const b = dom.window.document.querySelector('#gate button');
  if (b) b.click();
}, 600);

setTimeout(() => {
  const d = dom.window.document;
  const btn = d.querySelector('#tabs button[data-v="africa"]');
  if (btn) btn.click();

  setTimeout(() => {
    const titles = [...dom.window.document.querySelectorAll('.card h3 a')]
      .map(a => a.textContent.trim());
    const keptFresh = FRESH.filter(x => titles.some(t => t.indexOf(x.title_zh.slice(0, 6)) >= 0));
    const leakedStale = STALE.filter(x => titles.some(t => t.indexOf(x.title_zh.slice(0, 6)) >= 0));

    console.log('渲染卡片数:', titles.length);
    console.log('标题:', titles.join(' | ') || '(无)');
    console.log('');
    let fail = 0;
    function chk(label, ok) {
      console.log('%s  %s', ok ? 'PASS' : 'FAIL', label);
      if (!ok) fail++;
    }
    chk('3 条新鲜条目全部保留 (' + keptFresh.length + '/3)', keptFresh.length === 3);
    chk('4 天前条目未出现', !leakedStale.some(x => x.url.endsWith('/d')));
    chk('10 天前条目未出现', !leakedStale.some(x => x.url.endsWith('/e')));
    chk('卡片总数恰为 3（不是 5）', titles.length === 3);
    const jsErrs = errs.filter(e => !/fetch is not defined/.test(e));
    chk('无 JS 错误', jsErrs.length === 0);
    if (jsErrs.length) console.log('  ', jsErrs.slice(0, 2));

    console.log('\n' + (fail ? fail + ' 项失败' : '全部通过'));
    process.exit(fail ? 1 : 0);
  }, 400);
}, 1600);
