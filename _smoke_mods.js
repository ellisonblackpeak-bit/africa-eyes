/* 五个模块 tab 的无头渲染检查：每个模块都要能渲染、不报错、标题全中文。 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const here = __dirname;
const html = fs.readFileSync(path.join(here, 'index.html'), 'utf8');
const dataJs = fs.readFileSync(path.join(here, 'data.js'), 'utf8');

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

const TABS = ['overview', 'china', 'us', 'africa', 'periphery', 'eu', 'people', 'sources'];

setTimeout(() => {
  const b = dom.window.document.querySelector('#gate button');
  if (b) b.click();
}, 600);

let i = 0;
function step() {
  const d = dom.window.document;
  if (i >= TABS.length) {
    console.log('\nJS 错误:', errs.filter(e => !/fetch is not defined/.test(e)).length
      ? errs.filter(e => !/fetch is not defined/.test(e)).slice(0, 3) : '无');
    process.exit(0);
  }
  const v = TABS[i];
  const btn = d.querySelector('#tabs button[data-v="' + v + '"]');
  if (!btn) {
    console.log('%s  ✗ tab 不存在', v.padEnd(10));
    i++; return setTimeout(step, 120);
  }
  btn.click();
  setTimeout(() => {
    const d2 = dom.window.document;
    const cards = [...d2.querySelectorAll('.card h3 a')].map(a => a.textContent.trim());
    const latin = cards.filter(t => t && !/[\u4e00-\u9fff]/.test(t));
    const sec = d2.querySelector('.sec-title');
    const empty = d2.querySelector('.empty');
    console.log('%s  卡片=%-3d 非中文=%-2d %s',
      v.padEnd(10), cards.length, latin.length,
      sec ? sec.textContent.trim().slice(0, 34) : (empty ? '（空模块）' : ''));
    if (latin.length) console.log('           非中文样例:', latin.slice(0, 2));
    i++; setTimeout(step, 260);
  }, 240);
}
setTimeout(step, 1500);
