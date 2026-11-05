/**
 * 纸面主题体检脚本
 * ------------------------------------------------------------
 * 用法：在浏览器控制台执行本文件内容（或 node -e 里页面上下文执行）
 * 前提：页面已切到纸面模式（html[data-theme="paper"]）
 *
 * 返回：
 *   pink = 玫红系元素（R>150, G<130, B>90, R-G>60）
 *   dark = 深色背景（亮度 <100，纸面模式下需人工确认是否刻意保留）
 *   low  = 文字对比度 <4.5:1 且字号 <18px（WCAG AA 不达标）
 *
 * 判定标准：
 *   玫红不能只查 borderTopColor，四边 + 渐变 + 阴影都要查
 *   对比度按 WCAG 2.1 相对亮度公式算，不用目视
 */
(() => {
  const parse = (s) => {
    const m = (s || '').match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?/);
    if (!m) return null;
    return { r: +m[1], g: +m[2], b: +m[3], a: m[4] === undefined ? 1 : +m[4] };
  };

  // 玫红判定：R 高、G 明显低于 R、B 中等偏高（区别于纯红 B 很低）
  const isPink = (c) =>
    c && c.a > 0.12 && c.r > 140 && c.g < 140 && c.b > 70 && c.b < 230 &&
    (c.r - c.g) > 55 && (c.b - c.g) > 10;

  const relLum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };

  const contrast = (fg, bg) => {
    let l1 = relLum(fg), l2 = relLum(bg);
    if (l1 < l2) { const t = l1; l1 = l2; l2 = t; }
    return (l1 + 0.05) / (l2 + 0.05);
  };

  const res = { pink: [], dark: [], low: [], gradient: [], shadow: [] };
  const sp = new Set(), sd = new Set(), sg = new Set();

  const scope = document.querySelector('.spider-root') || document.body;

  scope.querySelectorAll('*').forEach((el) => {
    const cs = getComputedStyle(el);
    const cls = (el.className || '').toString().slice(0, 50) || el.tagName;
    const txt = (el.innerText || '').trim().slice(0, 18);

    // ① 粉色：背景 / 文字 / 四边边框
    [['bg', 'backgroundColor'], ['color', 'color'],
     ['bT', 'borderTopColor'], ['bR', 'borderRightColor'],
     ['bB', 'borderBottomColor'], ['bL', 'borderLeftColor']].forEach(([k, prop]) => {
      const c = parse(cs[prop]);
      if (isPink(c)) {
        const key = k + '|' + cls;
        if (!sp.has(key)) { sp.add(key); res.pink.push({ kind: k, cls, val: cs[prop], txt }); }
      }
    });

    // ② 渐变背景含粉色
    const bi = cs.backgroundImage;
    if (bi && bi.includes('gradient') && /rgba?\(\s*(19\d|2[0-5]\d)/.test(bi)) {
      const key = 'g|' + cls;
      if (!sg.has(key)) { sg.add(key); res.gradient.push({ cls, val: bi.slice(0, 110), txt }); }
    }

    // ③ 阴影含粉色
    const sh = cs.boxShadow;
    if (sh && sh !== 'none' && /rgba?\(\s*(19\d|2[0-5]\d)/.test(sh)) {
      const key = 's|' + cls;
      if (!sg.has(key)) { sg.add(key); res.shadow.push({ cls, val: sh.slice(0, 90), txt }); }
    }

    // ④ 深色背景
    const bg = parse(cs.backgroundColor);
    if (bg && bg.a > 0.5) {
      const lum = bg.r * 0.299 + bg.g * 0.587 + bg.b * 0.114;
      if (lum < 100) {
        const key = 'bg|' + cls;
        if (!sd.has(key)) { sd.add(key); res.dark.push({ cls, val: cs.backgroundColor, lum: Math.round(lum), txt }); }
      }
    }

    // ⑤ 低对比度文字（只查叶子节点，向上找实际背景色）
    if (el.children.length === 0 && txt) {
      const fg = parse(cs.color);
      let p = el, bgc = null;
      while (p && !bgc) {
        const c = parse(getComputedStyle(p).backgroundColor);
        if (c && c.a > 0.5) bgc = c;
        p = p.parentElement;
      }
      if (!bgc) bgc = { r: 238, g: 237, b: 232 };   // 纸底兜底
      if (fg && fg.a > 0.3) {
        const ratio = contrast(fg, bgc);
        const fs = parseFloat(cs.fontSize);
        if (ratio < 4.5 && fs < 18) {
          res.low.push({ cls, txt, color: cs.color, ratio: +ratio.toFixed(2), size: fs + 'px' });
        }
      }
    }
  });

  console.log(`玫红：${res.pink.length + res.gradient.length + res.shadow.length}`);
  console.log(`深色背景：${res.dark.length}`);
  console.log(`低对比度文字：${res.low.length}`);
  return res;
})();
