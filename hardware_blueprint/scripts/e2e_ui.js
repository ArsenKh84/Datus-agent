// End-to-end UI test against a live hwbp server: studio -> draw -> generate -> project page -> features -> share.
const { chromium } = require('playwright');
const path = require('path');
const BASE = process.argv[2];
const OUT = process.argv[3];
const results = [];
const log = (name, ok, detail) => { results.push({ name, ok, detail }); };

(async () => {
  const proxy = process.env.HTTPS_PROXY ? { server: process.env.HTTPS_PROXY, bypass: '127.0.0.1,localhost' } : undefined;
  const browser = await chromium.launch({ proxy, args: ['--use-gl=swiftshader', '--enable-webgl', '--ignore-gpu-blocklist'] });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('dialog', d => { errors.push('dialog: ' + d.message()); d.dismiss(); });
  try {
    // ---- studio
    await page.goto(BASE + '/', { waitUntil: 'networkidle' });
    const statsText = await page.textContent('#stats');
    log('studio: stats render', statsText.includes('projects created'), statsText.trim().slice(0, 60));
    await page.click('#ideabtn');
    await page.fill('#interests', 'robotics');
    await page.click('#ideago');
    await page.waitForSelector('[data-idea]');
    log('studio: need-an-idea suggests projects', (await page.$$('[data-idea]')).length > 0, await page.textContent('#ideas b'));
    await page.click('#drawbtn');
    const box = await (await page.$('#draw')).boundingBox();
    await page.mouse.move(box.x + 60, box.y + 60);
    await page.mouse.down();
    for (const [dx, dy] of [[300, 60], [300, 220], [60, 220], [60, 60]]) {
      await page.mouse.move(box.x + dx, box.y + dy, { steps: 12 });
    }
    await page.mouse.up();
    await page.click('#drawuse');
    await page.waitForSelector('#imgwrap:not([hidden])');
    log('studio: drawing attached as input image', (await page.textContent('#imginfo')).startsWith('drawing'), await page.textContent('#imginfo'));
    const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#tomesh')]);
    log('studio: drawing -> 3D STL download', (await dl.suggestedFilename()) === 'part.stl', await page.textContent('#imginfo'));
    await page.fill('#prompt', 'Portable radar with a touchscreen display, battery powered');
    await page.click('#go');
    await page.waitForSelector('#result a', { timeout: 30000 });
    const doneCount = (await page.$$('.phase.done')).length;
    log('studio: generation phases reach done', doneCount === 7, doneCount + '/7 phases');
    await page.screenshot({ path: path.join(OUT, '1_studio.png') });
    const href = await page.getAttribute('#result a', 'href');

    // ---- project page
    await page.goto(BASE + href, { waitUntil: 'networkidle' });
    log('project: hero image on Info', !!(await page.$('#tab-info .hero svg')), href);
    await page.click('nav button[data-tab="parts"]');
    await page.click('tr.part');
    const detailText = await page.textContent('tr.detail');
    log('project: part detail panel opens', detailText.includes('Where to buy'), detailText.slice(0, 60).replace(/\s+/g, ' '));
    await page.screenshot({ path: path.join(OUT, '2_parts_detail.png') });
    await page.click('nav button[data-tab="wiring"]');
    const simpleNodes = await page.$$eval('#wiring svg g rect[rx="8"]', n => n.length);
    await page.click('[data-mode="enriched"]');
    const enrichedNodes = await page.$$eval('#wiring svg g rect[rx="8"]', n => n.length);
    log('project: wiring enriched mode adds rail nodes', enrichedNodes > simpleNodes, simpleNodes + ' -> ' + enrichedNodes + ' nodes');
    const pathsBefore = await page.$$eval('#wiring svg path', n => n.length);
    await page.uncheck('[data-edge="power"]');
    const pathsAfter = await page.$$eval('#wiring svg path', n => n.length);
    log('project: POWER toggle hides power edges', pathsAfter < pathsBefore, pathsBefore + ' -> ' + pathsAfter + ' edges');
    const vb0 = await page.getAttribute('#wiring svg', 'viewBox');
    await page.click('[data-zoom="in"]');
    const vb1 = await page.getAttribute('#wiring svg', 'viewBox');
    log('project: wiring zoom changes viewBox', vb0 !== vb1, vb0 + ' -> ' + vb1);
    await page.screenshot({ path: path.join(OUT, '3_wiring_enriched.png') });
    await page.click('nav button[data-tab="mech"]');
    await page.waitForTimeout(1200);
    await page.screenshot({ path: path.join(OUT, '4_mech.png') });
    await page.click('#explode');
    await page.waitForTimeout(600);
    log('project: mech explode toggles', (await page.getAttribute('#explode', 'aria-pressed')) === 'true', 'aria-pressed=true');
    await page.screenshot({ path: path.join(OUT, '5_mech_exploded.png') });
    await page.click('nav button[data-tab="guide"]');
    await page.click('#tab-guide details summary');
    const det = await page.textContent('#tab-guide details[open]');
    log('project: instruction details show tip', det.includes('Tip:'), det.replace(/\s+/g, ' ').slice(0, 80));

    // ---- chat approve flow
    await page.click('#openchat');
    await page.fill('#chatinput', 'add a buzzer');
    await page.click('#chatform button');
    await page.waitForSelector('[data-op="approve"]', { timeout: 30000 });
    const chatText = await page.textContent('#chatlog');
    log('project: chat edit produces a plan awaiting approval', chatText.includes('awaiting_approval'), chatText.replace(/\s+/g, ' ').slice(0, 90));
    await page.screenshot({ path: path.join(OUT, '6_chat_preview.png') });
    await Promise.all([page.waitForNavigation(), page.click('[data-op="approve"]')]);
    await page.click('nav button[data-tab="parts"]');
    log('project: approved edit applied (buzzer in parts)', (await page.textContent('#partrows')).toLowerCase().includes('buzzer'), 'after reload');

    // ---- share page: star + copy
    const pid = href.split('/').pop();
    const pub = await page.evaluate(async (projectId) => {
      const r = await fetch('/api/publish', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ projectId }) });
      return r.json();
    }, pid);
    await page.goto(BASE + pub.url, { waitUntil: 'networkidle' });
    await page.click('#star');
    await page.waitForFunction(() => document.querySelector('#star').textContent.includes('Starred'));
    log('share: star button', true, await page.textContent('#star'));
    await Promise.all([page.waitForNavigation(), page.click('#copy')]);
    log('share: copy creates my project', page.url().includes('/p/'), page.url().replace(BASE, ''));

    // ---- studio shows community + my projects
    await page.goto(BASE + '/', { waitUntil: 'networkidle' });
    await page.waitForSelector('#community a.proj');
    log('studio: community feed lists the published project', (await page.$$('#community a.proj')).length >= 1, await page.textContent('#community a.proj b'));
    log('studio: my projects list', (await page.$$('#mine a')).length >= 2, (await page.$$('#mine a')).length + ' projects');
    await page.screenshot({ path: path.join(OUT, '7_studio_after.png'), fullPage: true });

    // ---- mobile
    const m = await browser.newPage({ viewport: { width: 375, height: 800 } });
    for (const u of ['/', href]) {
      await m.goto(BASE + u, { waitUntil: 'networkidle' });
      const over = await m.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      log('mobile 375px: no horizontal overflow on ' + (u === '/' ? 'studio' : 'project'), !over, String(over));
    }
  } catch (e) {
    log('E2E aborted', false, String(e).slice(0, 300));
  }
  log('no JS errors / alerts', errors.length === 0, errors.slice(0, 3).join(' | '));
  console.log(JSON.stringify(results, null, 1));
  await browser.close();
})();
