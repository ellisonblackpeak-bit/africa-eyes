/* 无头渲染冒烟：确认页面能起来、口令页在、卡片标题全中文。 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const here = __dirname;
const html = fs.readFileSync(path.join(here, 'index.html'), 'utf8');
const dataJs = fs.readFileSync(path.join(here, 'data.js'), 'utf8');

const vc = new VirtualConsole();
const errs = [];
vc.on('jsdomError', e => errs.push(String(e.message).slice(0, 160)));

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
    win.eval(dataJs);                                      // 数据先塞进 window
    win.localStorage.setItem('ce.gate.v1', 'f3837c0d');    // 已通过口令
  }
});

setTimeout(() => {
  const d = dom.window.document;
  const btn = d.querySelector('#gate form button, #gate button');
  if (btn) btn.click();
}, 700);

setTimeout(() => {
  const d = dom.window.document;
  const cards = [...d.querySelectorAll('.card h3 a')].map(a => a.textContent.trim());
  const latin = cards.filter(t => t && !/[\u4e00-\u9fff]/.test(t));
  const sums = [...d.querySelectorAll('.card .sum')].map(p => p.textContent.trim());
  const latinSum = sums.filter(t => t && !/[\u4e00-\u9fff]/.test(t));
  console.log('页面标题      :', d.title);
  console.log('口令页已移除  :', !d.getElementById('gate'), '| body.locked =', d.body.classList.contains('locked'));
  console.log('渲染卡片数    :', cards.length);
  console.log('非中文标题    :', latin.length, latin.slice(0, 3));
  console.log('非中文摘要    :', latinSum.length, latinSum.slice(0, 2));
  console.log('英文原文行    :', d.querySelectorAll('.en-orig').length,
              '| 待译角标:', d.querySelectorAll('.tl-pending').length);
  console.log('JS 错误       :', errs.length ? errs.slice(0, 3) : '无');
  process.exit(0);
}, 2600);
