#!/usr/bin/env node
/*
 * Render-time checks for a bilingual reader built by build_html.py.
 *
 * audit_scan.py judges the model, which is where content defects live. Three
 * classes of defect only exist after layout, and the Mankiw job shipped all
 * three because nothing was looking:
 *
 *   overlap    two boxes painted on top of each other
 *   blank      a box reserving space with nothing in it (a stripped folio that
 *              did not get collapsed, an element emptied by a repair pass)
 *   collapse   a page whose columns stopped being side by side. The model can
 *              be perfect and the page still render as one tall run -- that is
 *              what a missed gutter does -- so the test is whether, at any
 *              given height, both columns hold something. If the two columns'
 *              top values never interleave, they are stacked, not paired.
 *
 * Usage:
 *   NODE_PATH=<puppeteer-core dir> node verify_html.js <file.html>
 *
 * Exit code is 1 when a hard check fails, 0 otherwise.
 */
const p = require('puppeteer-core');
const path = require('path');
const fs = require('fs');

const EDGE = process.env.EDGE_PATH ||
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const CHROME = process.env.CHROME_PATH ||
  'C:/Program Files/Google/Chrome/Application/chrome.exe';

(async () => {
  const target = process.argv[2];
  if (!target) {
    console.error('usage: verify_html.js <file.html>');
    process.exit(2);
  }
  const file = path.resolve(target);
  if (!fs.existsSync(file)) {
    console.error('not found: ' + file);
    process.exit(2);
  }
  const exe = fs.existsSync(EDGE) ? EDGE : CHROME;
  if (!fs.existsSync(exe)) {
    console.error('no browser found; set EDGE_PATH or CHROME_PATH');
    process.exit(2);
  }

  const b = await p.launch({ executablePath: exe, headless: 'new',
                             args: ['--allow-file-access-from-files'] });
  const pg = await b.newPage();
  await pg.setViewport({ width: 1200, height: 1600, deviceScaleFactor: 1 });

  const external = [], errs = [];
  pg.on('request', r => {
    const u = r.url();
    if (!u.startsWith('file://') && !u.startsWith('data:') && !u.startsWith('blob:'))
      external.push(u);
  });
  pg.on('pageerror', e => errs.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });

  await pg.goto('file:///' + file.replace(/\\/g, '/'),
                { waitUntil: 'networkidle0', timeout: 180000 });

  const r = await pg.evaluate(() => {
    const out = { counts: {}, overlap: [], blank: [], collapse: [] };
    const pages = [...document.querySelectorAll('.page')];
    out.counts = {
      pages: pages.length,
      sents: document.querySelectorAll('.sent').length,
      zh: document.querySelectorAll('.zh').length,
      words: document.querySelectorAll('[data-w]').length,
      images: document.querySelectorAll('.imgbox').length,
      scrollHeight: document.body.scrollHeight
    };

    pages.forEach((pe, pi) => {
      const pr = pe.getBoundingClientRect();
      const boxes = [...pe.querySelectorAll('.el')].map((el, ei) => {
        const b = el.getBoundingClientRect();
        return {
          id: el.dataset.id || ('#' + ei),
          cls: el.className,
          x: b.left - pr.left, y: b.top - pr.top,
          w: b.width, h: b.height,
          empty: !(el.textContent || '').trim(),
          isImage: el.classList.contains('imgbox'),
          fmt: el.classList.contains('formula')
        };
      });

      for (let a = 0; a < boxes.length; a++) {
        const A = boxes[a];
        if (!A.isImage && A.w > 0 && A.h > 1 && A.empty)
          out.blank.push({ page: pi + 1, id: A.id, cls: A.cls,
                           w: Math.round(A.w), h: Math.round(A.h) });
        if (A.isImage) continue;
        for (let c = a + 1; c < boxes.length; c++) {
          const B = boxes[c];
          if (B.isImage) continue;
          const ox = Math.min(A.x + A.w, B.x + B.w) - Math.max(A.x, B.x);
          const oy = Math.min(A.y + A.h, B.y + B.h) - Math.max(A.y, B.y);
          if (ox > 2 && oy > 2)
            out.overlap.push({ page: pi + 1, a: A.id, b: B.id,
                               ox: Math.round(ox), oy: Math.round(oy) });
        }
      }

      // Columns must be PAIRED, i.e. their vertical extents must overlap.
      //
      // The failure mode is not stacking. When the gutter goes missing each
      // block still advances a shared cursor, so on a 5-block page the two
      // columns alternate -- left, right, left, right, left -- and every one
      // of them sits at a different height. Checking only "does one column
      // start below the other's end" passes that page. What betrays it is that
      // the columns barely share any height: measured on the Mankiw page 1,
      // the collapsed version overlaps 31% and the correct one 94%.
      const body = boxes.filter(b => !b.isImage && !b.fmt && b.w > 0 && b.h > 10);
      if (body.length >= 4) {
        const mid = pr.width / 2;
        const cover = arr => {
          const iv = arr.map(b => [b.y, b.y + b.h]).sort((p, q) => p[0] - q[0]);
          const merged = [];
          for (const [a, z] of iv) {
            const last = merged[merged.length - 1];
            if (last && a <= last[1]) last[1] = Math.max(last[1], z);
            else merged.push([a, z]);
          }
          return { merged, total: merged.reduce((s, [a, z]) => s + (z - a), 0) };
        };
        const L = cover(body.filter(b => b.x < mid));
        const R = cover(body.filter(b => b.x >= mid));
        if (L.merged.length && R.merged.length) {
          let ov = 0;
          for (const [a, z] of L.merged)
            for (const [c, d] of R.merged)
              ov += Math.max(0, Math.min(z, d) - Math.max(a, c));
          const ratio = ov / Math.max(1, Math.min(L.total, R.total));
          if (ratio < 0.60)
            out.collapse.push({ page: pi + 1, ratio: Math.round(ratio * 100),
                                left: Math.round(L.total), right: Math.round(R.total) });
        }
      }
    });
    return out;
  });

  let clicked = null, popup = '';
  for (const c of await pg.$$('[data-w]')) {
    if (await pg.evaluate(el => el.dataset.w && el.dataset.w.length > 3, c)) {
      await c.click();
      clicked = await pg.evaluate(el => el.dataset.w, c);
      break;
    }
  }
  if (clicked) {
    await new Promise(r => setTimeout(r, 1200));
    popup = await pg.evaluate(() => {
      const pop = document.querySelector('#pop');
      return pop ? (pop.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 90) : 'no #pop';
    });
  }
  await b.close();

  let bad = 0;
  const say = (label, n, detail) => {
    console.log(`${n ? 'FAIL' : 'ok  '} ${label}: ${n}`);
    if (detail) console.log('     ' + detail);
    if (n) bad++;
  };
  console.log(JSON.stringify(r.counts));
  say('external requests', external.length, external.slice(0, 3).join(' '));
  say('console/page errors', errs.length, errs.slice(0, 3).join(' '));
  say('overlapping boxes', r.overlap.length,
      r.overlap.slice(0, 4).map(o => `p${o.page} ${o.a}~${o.b}`).join(' '));
  say('blank boxes', r.blank.length,
      r.blank.slice(0, 4).map(o => `p${o.page} ${o.id} ${o.w}x${o.h}`).join(' '));
  say('columns stacked instead of paired', r.collapse.length,
      r.collapse.slice(0, 3).map(c => `p${c.page} overlap ${c.ratio}% (L${c.left}/R${c.right}pt)`).join(' '));
  say('word lookup', clicked && popup && popup !== 'no #pop' ? 0 : 1,
      clicked ? `${clicked} -> ${popup}` : 'no clickable word found');

  process.exit(bad ? 1 : 0);
})();
