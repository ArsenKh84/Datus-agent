// End-to-end UI test against a live hwbp server. Clicks every control of the studio, the project page, the share page
// and the site pages; a second browser context acts as another user (X-User-Id) to accept an invitation.
// Usage: node scripts/e2e_ui.js http://127.0.0.1:8765 docs/e2e   (writes screenshots + results.json to the folder)
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const BASE = process.argv[2];
const OUT = process.argv[3];
const results = [];
const log = (name, ok, detail) => { results.push({ name, ok: !!ok, detail: String(detail ?? '').replace(/\s+/g, ' ').slice(0, 120) }); };
const shot = (page, name, full) => page.screenshot({ path: path.join(OUT, name), fullPage: !!full });

async function drawDevice(page) {
  // studio DRAW pad: device outline, a screen rectangle inside it and three round buttons
  const box = await (await page.$('#draw')).boundingBox();
  const sx = box.width / 480, sy = box.height / 360;
  const poly = async pts => {
    await page.mouse.move(box.x + pts[0][0] * sx, box.y + pts[0][1] * sy);
    await page.mouse.down();
    for (const [x, y] of pts.slice(1)) await page.mouse.move(box.x + x * sx, box.y + y * sy, { steps: 6 });
    await page.mouse.up();
  };
  await poly([[40, 40], [440, 40], [440, 320], [40, 320], [40, 40]]);
  await poly([[90, 70], [300, 70], [300, 220], [90, 220], [90, 70]]);
  for (const cx of [120, 200, 280]) {
    const pts = [];
    for (let a = 0; a <= 360; a += 20) pts.push([cx + 13 * Math.cos(a * Math.PI / 180), 272 + 13 * Math.sin(a * Math.PI / 180)]);
    await poly(pts);
  }
}

(async () => {
  const proxy = process.env.HTTPS_PROXY ? { server: process.env.HTTPS_PROXY, bypass: '127.0.0.1,localhost' } : undefined;
  const browser = await chromium.launch({ proxy, args: ['--use-gl=swiftshader', '--enable-webgl', '--ignore-gpu-blocklist'] });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 }, acceptDownloads: true });
  const page = await ctx.newPage();
  const errors = [];
  const watch = (p, who) => {
    p.on('pageerror', e => errors.push(who + ' pageerror: ' + e.message));
    p.on('console', m => { if (m.type() === 'error') errors.push(who + ' console: ' + m.text()); });
    p.on('dialog', d => { errors.push(who + ' dialog: ' + d.message()); d.dismiss(); });
  };
  watch(page, 'owner');
  try {
    // ================================================================ studio: picture only -> "what should it do?"
    await page.goto(BASE + '/', { waitUntil: 'networkidle' });
    log('studio: stats render', (await page.textContent('#stats')).includes('projects created'), await page.textContent('#stats'));
    await page.click('#ideabtn');
    await page.fill('#interests', 'robotics');
    await page.click('#ideago');
    await page.waitForSelector('[data-idea]');
    log('studio: need-an-idea suggests projects', (await page.$$('[data-idea]')).length > 0, await page.textContent('#ideas b'));
    await page.click('#ideabtn');
    await page.click('#drawbtn');
    await drawDevice(page);
    await page.click('#drawuse');
    await page.waitForSelector('#attachwrap:not([hidden])');
    await page.waitForFunction(() => !document.querySelector('#imgsummary').textContent.startsWith('Reading'));
    const chips = await page.textContent('#imgchips');
    log('studio: drawing analysed (screen + 3 buttons found)', chips.includes('display window') && chips.includes('3 push buttons'), chips);
    const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#tomesh')]);
    log('studio: drawing -> 3D STL download', dl.suggestedFilename() === 'part-extrude.stl', await page.textContent('#meshout'));
    await shot(page, '1_studio_picture.png');
    await page.click('#go');
    await page.waitForSelector('#fngo', { timeout: 30000 });
    const opts = await page.$$eval('input[name=fn]', i => i.map(x => x.value));
    log('studio: picture-only run asks what the device should do', opts.length === 5 && opts[0].includes('3 buttons'), opts[1]);
    await shot(page, '2_studio_question.png');
    await page.check(`input[name=fn][value="${opts[1]}"]`);
    await page.click('#fngo');
    await page.waitForSelector('#result a', { timeout: 30000 });
    log('studio: generation phases reach done', (await page.$$('.phase.done')).length === 7, (await page.$$('.phase.done')).length + '/7');
    const href = await page.getAttribute('#result a', 'href');
    const pid = href.split('/').pop();

    // ================================================================ project page
    await page.goto(BASE + href + '?tab=parts', { waitUntil: 'networkidle' });
    const partsText = (await page.textContent('#partrows')).toLowerCase();
    log('project: picture became parts (display + buttons)', partsText.includes('display') && partsText.includes('button'), (await page.textContent('#title')));
    log('project: big drawn screen -> no 0.96in OLED', !partsText.includes('0.96in'), (partsText.match(/[0-9.]+in [a-z-]+/) || [''])[0]);
    await page.click('tr.part');
    log('project: part detail panel opens', (await page.textContent('tr.detail')).includes('Where to buy'), '');
    log('project: parts table has a total row', (await page.textContent('#ptotal')).includes('$'), await page.textContent('#ptotal'));
    await page.fill('#bqty', '10');
    await page.click('#bqgo');
    await page.waitForFunction(() => document.querySelector('#bqout').textContent.includes('10 build'));
    log('project: build quantity pricing', true, await page.textContent('#bqout'));
    await shot(page, '3_parts.png');

    await page.click('nav button[data-tab="info"]');
    await page.waitForFunction(() => { const i = document.querySelector('#heroimg'); return i && i.complete && i.naturalWidth > 0; });
    log('project: hero image on Info', true, href);
    const before = await page.$$eval('#costtree .tr[hidden]', r => r.length);
    await page.click('#costtree .l0 .tog');
    const after = await page.$$eval('#costtree .tr[hidden]', r => r.length);
    log('project: cost tree collapses category -> type -> part', after > before, before + ' -> ' + after + ' hidden rows');
    await page.fill('#notestext', 'Print the lid in white PETG.');
    await page.click('#notesave');
    await page.waitForFunction(() => document.querySelector('#notestate').textContent.startsWith('Saved'));
    log('project: notes save', true, await page.textContent('#notestate'));
    await page.click('#reroll');
    await page.waitForSelector('#useimg');
    await page.click('#useimg');
    await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('image updated'));
    log('project: new hero image applied', true, await page.getAttribute('#heroimg', 'src'));
    await shot(page, '4_info.png');

    await page.click('nav button[data-tab="wiring"]');
    const simpleNodes = await page.$$eval('#wiring svg g rect[rx="8"]', n => n.length);
    await page.click('[data-mode="enriched"]');
    const enrichedNodes = await page.$$eval('#wiring svg g rect[rx="8"]', n => n.length);
    log('project: wiring enriched mode adds rail nodes', enrichedNodes > simpleNodes, simpleNodes + ' -> ' + enrichedNodes);
    const p0 = await page.$$eval('#wiring svg path', n => n.length);
    await page.uncheck('#wtools [data-edge="power"]');
    log('project: POWER toggle hides power edges', (await page.$$eval('#wiring svg path', n => n.length)) < p0, '');
    const vb0 = await page.getAttribute('#wiring svg', 'viewBox');
    await page.click('[data-zoom="in"]');
    log('project: wiring zoom', vb0 !== (await page.getAttribute('#wiring svg', 'viewBox')), '');
    await page.click('#wside [data-focus]');
    log('project: electrical-parts sidebar focuses a part', (await page.$$eval('#wiring svg g[opacity="0.25"]', n => n.length)) > 0, (await page.$$('#wside [data-focus]')).length + ' parts listed');
    await shot(page, '5_wiring.png');

    await page.click('nav button[data-tab="mech"]');
    await page.waitForTimeout(1200);
    const axes = await page.$$eval('#axislabels span', s => s.map(x => x.textContent));
    log('project: 3D dimension axes X/Y/Z', axes.length === 3 && axes[0].startsWith('X '), axes.join(', '));
    await page.click('[data-eye]');
    log('project: per-part eye toggle hides a part', (await page.$$('#mtree .row.off')).length === 1, '');
    await page.click('[data-eye]');
    const cad0 = await page.textContent('#cadcount');
    await page.click('[data-gen]');
    await page.waitForFunction(c => document.querySelector('#cadcount').textContent !== c, cad0);
    log('project: per-part CAD generation', (await page.textContent('#cadcount')).startsWith('CAD 1/'), cad0 + ' -> ' + (await page.textContent('#cadcount')));
    await page.click('.cadslot summary');
    log('project: CAD file menu offers STEP', (await page.$$eval('.cadslot details[open] a', a => a.map(x => x.textContent))).some(t => t.includes('STEP')), '');
    await page.click('#mhome');
    await page.click('#explode');
    await page.waitForTimeout(500);
    log('project: mech explode toggles', (await page.getAttribute('#explode', 'aria-pressed')) === 'true', '');
    await shot(page, '6_mech.png');

    await page.click('nav button[data-tab="guide"]');
    const g0 = await page.textContent('#gdone');
    await page.click('[data-step]');
    log('project: instructions progress counter', (await page.textContent('#gdone')) !== g0, g0 + ' -> ' + (await page.textContent('#gdone')));
    log('project: part cards in instructions', (await page.$$('.pcard')).length > 0, (await page.$$('.pcard')).length + ' cards');
    await page.click('[data-more]');
    await page.waitForFunction(() => document.querySelector('.moreout').textContent.length > 20);
    log('project: instruction "View details" shows a tip', (await page.textContent('.moreout')).includes('Tip:'), '');
    await shot(page, '7_instructions.png');

    await page.click('#histbtn');
    await page.waitForSelector('#dlgbody .li');
    const versions = (await page.$$('#dlgbody .li')).length;
    log('project: history lists versions', versions >= 2, versions + ' versions');
    await Promise.all([page.waitForNavigation(), page.click('[data-restore]')]);
    await page.click('#histbtn');
    await page.waitForSelector('#dlgbody .li');
    log('project: restore creates a new version', (await page.$$('#dlgbody .li')).length === versions + 1, '');
    await page.click('#dlgclose');

    await page.click('#exportmenu summary');
    const [cadZip] = await Promise.all([page.waitForEvent('download'), page.click('[data-cad="step"]')]);
    log('project: export CAD (STEP zip)', cadZip.suggestedFilename().endsWith('-cad-step.zip'), cadZip.suggestedFilename());
    await page.click('#exportmenu summary');
    const [guide] = await Promise.all([page.waitForEvent('download'), page.click('[data-file="guide.md"]')]);
    log('project: export build guide', guide.suggestedFilename().endsWith('guide.md'), guide.suggestedFilename());

    await page.click('#openchat');
    await page.waitForSelector('#pchips button');
    log('project: chat suggested prompts', (await page.$$('#pchips button')).length > 0, await page.textContent('#pchips'));
    await page.fill('#chatinput', 'add a buzzer');
    await page.click('#chatform button');
    await page.waitForSelector('[data-op="approve"]', { timeout: 30000 });
    await shot(page, '8_chat.png');
    await Promise.all([page.waitForNavigation(), page.click('[data-op="approve"]')]);
    await page.click('nav button[data-tab="parts"]');
    log('project: approved chat edit applied (buzzer)', (await page.textContent('#partrows')).toLowerCase().includes('buzzer'), '');

    // ================================================================ share dialog + second user
    await page.click('#sharebtn');
    await page.waitForSelector('#pubbtn');
    await page.click('#pubbtn');
    await page.waitForSelector('#unpub');
    const pubUrl = await page.$eval('#dlgbody input[readonly]', i => i.value);
    log('share: publish to community', pubUrl.includes('/s/'), pubUrl.replace(BASE, ''));
    await page.fill('#invemail', 'friend@example.com');
    await page.click('#invform button');
    await page.waitForFunction(() => document.querySelector('#invout').textContent.includes('invitations#token='));
    const inviteUrl = (await page.textContent('#invout')).match(/(\/invitations#token=\S+)/)[1];
    log('share: invite by email gives a link', true, inviteUrl.slice(0, 30));
    await page.click('#mklink');
    await page.waitForFunction(() => document.querySelector('#linkout').textContent.includes('join#token='));
    log('share: create share link', true, (await page.textContent('#linkout')).slice(0, 40));
    await shot(page, '9_share.png');
    await page.click('#dlgclose');

    const ctx2 = await browser.newContext({ viewport: { width: 1280, height: 860 }, extraHTTPHeaders: { 'X-User-Id': 'friend' } });
    const friend = await ctx2.newPage();
    watch(friend, 'friend');
    await friend.goto(BASE + inviteUrl, { waitUntil: 'networkidle' });
    await friend.waitForSelector('#box a');
    log('invitations page: second user accepts', (await friend.textContent('#box')).includes('viewer'), await friend.textContent('#box'));
    await friend.goto(BASE + '/p/' + pid, { waitUntil: 'networkidle' });
    log('project as viewer: no Share button, notes read-only', !(await friend.$('#sharebtn')) && (await friend.getAttribute('#notestext', 'readonly')) !== null, '');
    await page.click('#sharebtn');
    await page.waitForSelector('[data-member]');
    log('share: members list shows the new collaborator', (await page.textContent('#dlgbody')).includes('friend'), '');
    await page.selectOption('[data-member="friend"]', 'editor');
    await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('Role updated'));
    log('share: change member role', true, 'friend -> editor');
    await page.click('#dlgclose');

    // ================================================================ share page, community, site pages
    await page.goto(pubUrl, { waitUntil: 'networkidle' });
    await page.click('#star');
    await page.waitForFunction(() => document.querySelector('#star').textContent.includes('Starred'));
    log('share page: star with count', (await page.textContent('#starn')) === '1', await page.textContent('#star'));
    await Promise.all([page.waitForNavigation(), page.click('#copy')]);
    log('share page: copy creates my project', page.url().includes('/p/'), page.url().replace(BASE, ''));

    await page.goto(BASE + '/community', { waitUntil: 'networkidle' });
    await page.waitForSelector('#grid a.proj');
    log('community page: lists the published project with author', (await page.textContent('#grid a.proj')).includes('local-user'), await page.textContent('#grid a.proj b'));
    await page.fill('#q', 'zzzz-no-match');
    await page.waitForFunction(() => document.querySelector('#grid').textContent.includes('No published projects match'));
    log('community page: search', await page.isHidden('#more'), 'Load more hidden when nothing matches');
    log('community page: projects-created counter counts picture runs', !(await page.textContent('#stats')).startsWith('0'), await page.textContent('#stats'));
    await shot(page, '10_community.png');

    await page.goto(BASE + '/projects', { waitUntil: 'networkidle' });
    await page.waitForSelector('#mine a');
    log('my projects page: projects + starred', (await page.$$('#mine a')).length >= 2 && (await page.$$('#starred a.proj')).length === 1, (await page.$$('#mine a')).length + ' projects');

    await page.goto(BASE + '/pricing', { waitUntil: 'networkidle' });
    await page.waitForSelector('[data-bundle]');
    const w0 = await page.textContent('#wallet');
    await page.click('[data-bundle="small"]');
    await page.waitForFunction(w => document.querySelector('#wallet').textContent !== w, w0);
    log('pricing: buy CAD credits (simulated)', true, w0 + ' -> ' + (await page.textContent('#wallet')));
    await page.click('[data-plan]');
    await page.waitForFunction(() => document.querySelector('#toast').textContent.includes('Plan changed'));
    log('pricing: switch plan (simulated)', true, await page.textContent('.plan.current b'));
    await shot(page, '11_pricing.png');

    await page.goto(BASE + '/account', { waitUntil: 'networkidle' });
    await page.click('#autoname');
    await page.waitForFunction(() => document.querySelector('#uname').value.length >= 3);
    const uname = await page.inputValue('#uname');
    await page.click('#prof button.primary');
    await page.waitForFunction(() => document.querySelector('#profout').textContent === 'Saved');
    await page.reload({ waitUntil: 'networkidle' });
    log('account: username saved and shown in header', (await page.textContent('#acct')).includes(uname), uname);
    await page.uncheck('#prefs input[name=projectActivity]');
    await page.click('#prefs button');
    await page.waitForFunction(() => document.querySelector('#prefout').textContent === 'Saved');
    log('account: email preferences saved', true, '');

    await page.goto(BASE + '/faq', { waitUntil: 'networkidle' });
    log('faq page', (await page.$$('details')).length >= 5, (await page.$$('details')).length + ' questions');
    await page.goto(BASE + '/about', { waitUntil: 'networkidle' });
    await page.fill('#leademail', 'someone@example.com');
    await page.click('#lead button');
    await page.waitForFunction(() => document.querySelector('#leadout').textContent.includes('Thanks'));
    log('about page: contact form', true, '');

    // ================================================================ unfinished picture run -> resume from My projects
    const runId = await page.evaluate(async () => {
      const c = document.createElement('canvas'); c.width = 200; c.height = 150; const g = c.getContext('2d');
      g.fillStyle = '#fff'; g.fillRect(0, 0, 200, 150); g.fillStyle = '#111'; g.fillRect(30, 30, 140, 90);
      const up = await (await fetch('/api/generation-input-images', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image: c.toDataURL('image/png') }) })).json();
      const run = await (await fetch('/api/generation-runs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ inputImages: [up.id] }) })).json();
      return run.id;
    });
    await page.goto(BASE + '/projects', { waitUntil: 'networkidle' });
    await page.waitForSelector('#runs a');
    await Promise.all([page.waitForNavigation(), page.click('#runs a')]);
    await page.waitForSelector('#fngo');
    log('resume: unfinished picture run reopens at the question', page.url().includes(runId), '');

    // ================================================================ phone width
    const m = await browser.newPage({ viewport: { width: 375, height: 800 } });
    watch(m, 'mobile');
    for (const u of ['/', href, '/community', '/pricing', '/account']) {
      await m.goto(BASE + u, { waitUntil: 'networkidle' });
      const over = await m.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      log('375px: no horizontal overflow on ' + u, !over, String(over));
    }
    await m.goto(BASE + href, { waitUntil: 'networkidle' });
    await shot(m, '12_mobile_project.png');
  } catch (e) {
    log('E2E aborted', false, String(e).slice(0, 300));
  }
  log('no JS errors / alerts', errors.length === 0, errors.slice(0, 3).join(' | '));
  fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify(results, null, 1));
  console.log(JSON.stringify(results, null, 1));
  await browser.close();
})();
