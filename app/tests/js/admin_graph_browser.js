// Browser check for /admin/graph (run by app/tests/py/test_admin_graph_browser.py with the global playwright).
// usage: node admin_graph_browser.js <baseUrl> <cookieName> <cookieValue> <searchText> <expectedNeighbours>
const { chromium } = require('playwright');

const [base, cookieName, cookieValue, query, expected] = process.argv.slice(2);

async function check(browser, viewport, hasTouch) {
  const ctx = await browser.newContext({ viewport, hasTouch, isMobile: hasTouch });
  await ctx.addCookies([{ name: cookieName, value: cookieValue, url: base }]);
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto(base + '/admin/graph');
  await page.waitForFunction(() => document.getElementById('gstats').textContent.includes('nodes'));
  // no horizontal overflow
  const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  if (over > 1) throw new Error(`horizontal overflow ${over}px at ${viewport.width}`);
  // tap targets >= 44 px
  const small = await page.evaluate(() => [...document.querySelectorAll('nav a, button, #gq, #gkinds label')]
    .filter((n) => n.offsetParent && n.getBoundingClientRect().height < 43).map((n) => n.outerHTML.slice(0, 60)));
  if (small.length) throw new Error('small tap targets: ' + small.join(' | '));
  await page.fill('#gq', query);
  await page.press('#gq', 'Enter');
  await page.waitForSelector('#gresults button');
  await page.click('#gresults button');
  await page.waitForFunction((n) => window.djGraph && window.djGraph.cy.nodes().length >= n, Number(expected));
  const counts = await page.evaluate(() => ({ n: window.djGraph.cy.nodes().length, e: window.djGraph.cy.edges().length }));
  if (counts.n !== Number(expected)) throw new Error(`nodes ${counts.n} != ${expected}`);
  // tap a neighbour: its details open and its neighbours load (the second hop adds edges)
  const before = counts.e;
  const pos = await page.evaluate(() => {
    const cy = window.djGraph.cy, n = cy.nodes().filter((x) => !x.hasClass('center'))[0], p = n.renderedPosition();
    const r = document.getElementById('gcy').getBoundingClientRect();
    return { x: r.left + p.x, y: r.top + p.y, id: n.id() };
  });
  await page.mouse.click(pos.x, pos.y);
  await page.waitForFunction(() => !document.getElementById('gpanel').hidden && document.getElementById('gpanel').textContent.includes('Library'));
  await page.waitForFunction((id) => window.djGraph.cy.getElementById(id).hasClass('done'), pos.id);
  const after = await page.evaluate(() => window.djGraph.cy.edges().length);
  if (after < before) throw new Error('edges dropped after expand');
  // kind filter hides edges
  await page.evaluate(() => { document.querySelector('#gkinds input[value=measured]').click(); });
  const shown = await page.evaluate(() => window.djGraph.cy.edges('[kind = "measured"]').filter((e) => !e.hasClass('hidden')).length);
  if (shown !== 0) throw new Error('measured edges still visible after unchecking');
  if (errors.length) throw new Error('page errors: ' + errors.join('; '));
  await ctx.close();
}

(async () => {
  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  try {
    await check(browser, { width: 360, height: 740 }, true);
    await check(browser, { width: 1280, height: 800 }, false);
    console.log('ok');
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e.stack || String(e)); process.exit(1); });
