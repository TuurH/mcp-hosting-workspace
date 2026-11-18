// Exercise real DOM updates against changing, server-rendered fixture pages.
// No production browser, Docker engine, credentials or app state is involved.
const {chromium} = require('playwright');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');
const root = path.resolve(process.env.UI_ARTIFACT_DIR || 'ui-artifacts');
const fixture = name => fs.readFileSync(path.join(root, name + '.html'), 'utf8');
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    const errors = [], posts = [];
    page.on('pageerror', error => errors.push(error.message));
    let html = fixture('app-idle'), finished = false, unavailable = false, gets = 0;
    await page.route('http://admin.test/**', async route => {
      const request = route.request(), url = new URL(request.url());
      if (request.method() === 'POST') { posts.push(request.postData()); return route.fulfill({status: 200, body: html}); }
      if (url.pathname === '/api/notices') { return route.fulfill({json: {latest: 1, notices: []}}); }
      if (url.pathname.includes('/operations/')) {
        return route.fulfill({json: {status: finished ? 'succeeded' : 'running', phase: 'Building the image',
          text: url.searchParams.get('offset') === '0' ? 'live build output\n' : '', offset: Number(url.searchParams.get('offset')),
          next_offset: 18, size: 18, elapsed_seconds: 7}});
      }
      gets++;
      return route.fulfill({status: unavailable ? 503 : 200, contentType: 'text/html', body: html});
    });
    await page.clock.install();
    await page.goto('http://admin.test/apps/weather');
    assert.equal(await page.locator('[data-live-operation]').count(), 0);
    assert.equal(await page.getByRole('button', {name: 'Release pending version', exact: true}).count(), 0);
    const secret = page.locator('form[action*="/secrets?"] input[name="value"]');
    await secret.fill('unsaved-secret-for-fixture-only');
    await page.locator('#development details').first().evaluate(node => { node.open = true; });
    html = fixture('app-ready');
    await page.clock.runFor(5500);
    await page.getByRole('button', {name: 'Release pending version', exact: true}).waitFor();
    assert(await page.getByRole('button', {name: 'Release pending version', exact: true}).isEnabled());
    assert.equal(await secret.inputValue(), 'unsaved-secret-for-fixture-only');
    assert(await page.locator('#development details').first().evaluate(node => node.open));
    assert.equal(await secret.evaluate(node => document.activeElement === node), true);

    // A newly started operation gets a poller without navigating the page.
    html = fixture('app');
    await page.clock.runFor(5500);
    await page.locator('[data-live-log]').getByText('live build output').waitFor();
    await page.locator('#image-builds input[name="no_cache"]').check();
    await page.locator('#image-builds input[name="refresh_base"]').check();
    // Same operation is not duplicated or restarted on the next page refresh.
    await page.clock.runFor(5500);
    assert.equal(await page.locator('[data-live-log]').textContent(), 'live build output\n');
    assert(await page.locator('#image-builds input[name="no_cache"]').isChecked());
    assert(await page.locator('#image-builds input[name="refresh_base"]').isChecked());
    finished = true; html = fixture('app-ready');
    await page.clock.runFor(5500);
    await page.waitForFunction(() => !document.querySelector('[data-live-operation]'));
    assert.equal(await secret.inputValue(), 'unsaved-secret-for-fixture-only');

    // New asks, action grants and requests resolved in another tab are merged.
    html = fixture('app-asks');
    await page.clock.runFor(5500);
    await page.locator('#log-access #ask-6').waitFor();
    html = fixture('app-ready');
    await page.clock.runFor(5500);
    await page.waitForFunction(() => !document.querySelector('#ask-6'));
    assert.equal(await secret.inputValue(), 'unsaved-secret-for-fixture-only');
    assert.equal(await page.locator('#permission-settings form[action*="/settings?"]').count(), 0);
    assert.equal(await page.locator('#log-access form[action*="/policy?"]').count(), 0);

    // Saving the log form must not silently discard edits in the permission card.
    await page.locator('#permission-settings input[name="restart"]').check();
    let prompted = '';
    const rejectDialog = async dialog => { prompted = dialog.message(); await dialog.dismiss(); };
    page.on('dialog', rejectDialog);
    await page.getByRole('button', {name: 'Save protection and log access', exact: true}).click();
    assert(prompted.includes('unsaved changes in another form'));
    assert.equal(posts.length, 0);
    page.off('dialog', rejectDialog);

    // A changed review token must not silently retarget an edited approval.
    html = fixture('app-ready').replaceAll('value="reviewed-state"', 'value="new-review-state"');
    await page.clock.runFor(5500);
    await page.locator('#permission-settings form[data-stale-form]').waitFor();
    assert(await page.locator('#permission-settings input[name="restart"]').isChecked());
    assert.equal(await page.locator('#permission-settings input[name="revision"]').inputValue(), 'reviewed-state');
    await page.getByRole('button', {name: 'Save environment and AI permissions', exact: true}).click();
    await page.locator('.toast').getByText('This form changed on the server.').waitFor();
    assert.equal(posts.length, 0);

    // Failure/login responses keep the admin DOM and edited credential intact.
    unavailable = true;
    await page.clock.runFor(16500);
    await page.locator('.toast').getByText('Live updates are unavailable.').waitFor();
    assert.equal(await secret.inputValue(), 'unsaved-secret-for-fixture-only');
    unavailable = false; html = '<html><body>Login required</body></html>';
    await page.clock.runFor(5500);
    assert.equal(await secret.inputValue(), 'unsaved-secret-for-fixture-only');
    html = fixture('app-ready');
    await page.clock.runFor(5500);
    await page.waitForFunction(() => !Array.from(document.querySelectorAll('.toast')).some(node => node.textContent.includes('Live updates are unavailable.')));
    // Daily CSRF rotation must not duplicate an edited form or lose its values.
    html = fixture('app-ready').replaceAll('_csrf=preview', '_csrf=next-day').replace('name="csrf-token" content="preview"', 'name="csrf-token" content="next-day"');
    await page.clock.runFor(5500);
    assert.equal(await page.locator('form[action*="/secrets?"]').count(), 1);
    assert.equal(await secret.inputValue(), 'unsaved-secret-for-fixture-only');
    assert(gets >= 10);
    assert.deepEqual(errors, []);
    await page.screenshot({path: path.join(root, 'app-live-preserved-edits.png'), fullPage: true});
    await page.locator('.app-settings').screenshot({path: path.join(root, 'permission-log-cards.png')});
    // Settings without review-token inputs still detect conflicting server edits.
    html = fixture('app-ready');
    await page.goto('http://admin.test/apps/weather');
    const logForm = page.locator('#log-access form[action*="/settings?"]');
    const protect = logForm.locator('input[name="protected"]');
    const mode = logForm.locator('select[name="log_access"]');
    await protect.uncheck();
    assert.equal(await logForm.locator('[data-unsaved-note]').count(), 1);
    // Refreshed prose in the form does not change the saved settings.
    html = html.replace('Effective access: masked.', 'Effective access: off.');
    await page.clock.runFor(5500);
    assert.equal(await logForm.getAttribute('data-stale-form'), null);
    assert.equal(await protect.isChecked(), false);
    await protect.check();
    assert.equal(await logForm.locator('[data-unsaved-note]').count(), 0);
    await mode.selectOption('full');
    await mode.selectOption('masked');
    assert.equal(await logForm.locator('[data-unsaved-note]').count(), 0);
    await mode.selectOption('full');
    html = fixture('app-ready').replace('name="protected" checked', 'name="protected" ');
    await page.clock.runFor(5500);
    await page.locator('#log-access form[data-stale-form]').waitFor();
    assert.equal(await mode.inputValue(), 'full');
    await page.getByRole('button', {name: 'Save protection and log access', exact: true}).click();
    assert.equal(posts.length, 0);
    await mode.selectOption('masked');
    assert.equal(await logForm.getAttribute('data-stale-form'), null);
    assert.equal(await logForm.locator('[data-unsaved-note]').count(), 0);
    assert.equal(await protect.isChecked(), false);
    console.log('Live pending releases, new operations, completion, asks, preserved forms/focus/details, stale reviews, separate saves and reconnects passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
