/* Run with AGENT_BUS_TEST_PYTHON and PLAYWRIGHT_MODULE (or installed playwright).
   Starts its own temporary project/hub. Never touches the developer's runtime. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { mkdtempSync, readFileSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const assert = require('node:assert/strict');
const net = require('node:net');
const root = path.resolve(__dirname, '..');
const project = mkdtempSync(path.join(tmpdir(), 'agent-bus-console-'));
const python = process.env.AGENT_BUS_TEST_PYTHON || path.join(root, '.venv/bin/python');
const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => !k.startsWith('AGENT_BUS_') && k !== 'AGENT_ID'));
env.PYTHONPATH = path.join(root, 'src');
function cli(...args) {
  const r = spawnSync(python, ['-m', 'agent_bus.cli.main', '--project', project, ...args], { env, encoding: 'utf8', timeout: 30000 });
  assert.equal(r.status, 0, r.stderr); return r.stdout;
}
async function port() {
  const s = net.createServer(); await new Promise(r => s.listen(0, '127.0.0.1', r));
  const p = s.address().port; await new Promise(r => s.close(r)); return p;
}
(async () => {
  let hub, browser;
  try {
    const url = `http://127.0.0.1:${await port()}`;
    cli('init', '--bus-url', url);
    cli('auth', 'create', '--agent', 'human', '--role', 'admin');
    cli('auth', 'create', '--agent', 'backend');
    const credential = id => JSON.parse(readFileSync(path.join(project, '.agent-bus/runtime/credentials', `${id}.json`)));
    const human = credential('human'), backend = credential('backend');
    hub = spawn(python, ['-m', 'agent_bus.cli.main', '--project', project, 'serve'], { env, stdio: 'ignore' });
    let ready = false;
    for (let i = 0; i < 100; i++) {
      try { if ((await fetch(url + '/health')).ok) { ready = true; break; } } catch (_) {}
      await new Promise(r => setTimeout(r, 100));
    }
    assert(ready, 'Hub did not start');
    async function request(route, data, who = human) {
      const r = await fetch(url + route, { method: data ? 'POST' : 'GET',
        headers: { Authorization: `Bearer ${who.token}`, 'Content-Type': 'application/json' },
        body: data ? JSON.stringify(data) : undefined });
      assert(r.ok, `${route}: ${r.status}`); return r.json();
    }
    await request('/register', { agent_id: 'backend', display_name: 'Backend' }, backend);
    await request('/room/api/tasks', { task_id: 'DEMO', title: 'Entrega con evidencia',
      description: 'Descripción completa para revisar antes de decidir.', acceptance_criteria: ['La prueba debe pasar'], test_cmd: ['pytest', '-q'] });
    await request('/locks/acquire', { file_path: path.join(project, 'demo.py'), reason: 'edición', ttl_seconds: 300 }, backend);
    for (let i = 0; i < 6; i++) await request('/messages', { to_agent: 'human', related_task: 'DEMO', reply_needed: true,
      body: { text: `Solicitud ${i}: ${'contexto '.repeat(25)}FIN-CONTEXTO`, validation_source: 'reported_by_agent', validation_summary: 'Prueba de ejemplo aprobada' } }, backend);
    browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
    const page = await context.newPage(); page.setDefaultTimeout(10000); const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    assert.equal(await (await fetch(url + '/room')).text(), await (await fetch(url + '/console')).text());
    await page.goto(url + '/room');
    await page.getByLabel('Token de sesión administrativa').fill(backend.token);
    await page.getByRole('button', { name: 'Entrar', exact: true }).click();
    await page.getByText('Esta consola requiere una sesión administrativa').waitFor();
    await page.getByLabel('Token de sesión administrativa').fill(human.token);
    await page.getByRole('button', { name: 'Entrar', exact: true }).click();
    await page.getByRole('heading', { name: 'Solicitudes pendientes (6)', exact: true }).waitFor();
    await page.getByText('Conectado', { exact: true }).waitFor();
    assert.equal(await page.locator('.approval').count(), 6);
    assert((await page.locator('.approval').first().textContent()).includes('FIN-CONTEXTO'));
    await page.getByPlaceholder('ID (ej. T-30)').fill('BROWSER');
    await page.getByPlaceholder('Título', { exact: true }).fill('Creada desde el navegador');
    await page.getByRole('button', { name: 'Crear', exact: true }).click();
    await page.locator('.task').filter({ hasText: 'BROWSER: Creada desde el navegador' }).waitFor();
    const task = page.locator('.task').filter({ hasText: 'DEMO: Entrega con evidencia' });
    await task.getByLabel('Reasignar DEMO', { exact: true }).selectOption('backend');
    await task.getByText('owner: backend', { exact: true }).waitFor();
    await task.getByText('Ver detalle', { exact: true }).click();
    await page.getByText('La prueba debe pasar', { exact: true }).waitFor();
    await page.locator('.task-detail .evt').first().waitFor();
    assert.equal((await request('/room/api/pending-approvals')).length, 6, 'Reading detail must not ACK');
    await page.screenshot({ path: '/tmp/agent-bus-console-task.png' });
    await page.getByRole('button', { name: 'Cerrar detalle', exact: true }).click();
    await page.locator('.approval').first().locator('summary').first().click();
    await page.locator('.approval').first().getByLabel('Decisión', { exact: true }).selectOption('respond');
    assert(await page.locator('.approval').first().getByRole('button', { name: 'Enviar decisión' }).isDisabled());
    await page.locator('.approval').first().getByLabel('Observación').fill('Revisa el caso límite antes de continuar.');
    await page.locator('.approval').first().getByRole('button', { name: 'Enviar decisión' }).click();
    await page.getByRole('heading', { name: 'Solicitudes pendientes (5)', exact: true }).waitFor();
    const replies = await request('/inbox/backend/messages', undefined, backend);
    assert(replies.messages.some(m => m.body.note === 'Revisa el caso límite antes de continuar.'));
    await context.setOffline(true);
    await page.getByText('Reconectando…', { exact: true }).waitFor({ timeout: 15000 });
    await request('/messages', { to_agent: 'human', body: { text: 'RECUPERADO-TRAS-CORTE' } }, backend);
    await context.setOffline(false);
    await page.getByText('Conectado', { exact: true }).waitFor({ timeout: 15000 });
    await page.locator('.feed').getByText('RECUPERADO-TRAS-CORTE', { exact: true }).waitFor();
    await page.getByPlaceholder('Buscar actividad').fill('SIN-COINCIDENCIAS');
    await page.getByText('Sin coincidencias.', { exact: true }).waitFor();
    await page.getByPlaceholder('Buscar actividad').fill('');
    await page.screenshot({ path: '/tmp/agent-bus-console-desktop.png', fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'Mobile overflow');
    await page.screenshot({ path: '/tmp/agent-bus-console-mobile.png', fullPage: true });
    await page.getByRole('button', { name: 'Salir', exact: true }).click();
    await page.getByRole('button', { name: 'Entrar', exact: true }).waitFor();
    assert.equal(await page.locator('.task').count(), 0);
    assert.equal(await page.evaluate(() => window.ConsoleAPI.getToken()), null);
    assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
    // A late success from a previous login must never enter the next session.
    const stale = await page.evaluate(async () => {
      const original = window.fetch; let release;
      window.ConsoleAPI.setToken('old-session');
      window.fetch = () => new Promise(resolve => { release = resolve; });
      const waiting = window.ConsoleAPI.api('/delayed').then(() => 'accepted', e => e.name);
      window.ConsoleAPI.clearToken(); window.ConsoleAPI.setToken('new-session');
      release(new Response(JSON.stringify({ private: true }), { status: 200 }));
      const result = await waiting;
      window.fetch = original; window.ConsoleAPI.clearToken(); return result;
    });
    assert.equal(stale, 'AbortError');
    assert.deepEqual(errors, []);
    console.log('PASS: admin login, six full approvals, task creation/assignment/history, reply, offline replay, filter, mobile, logout, stale session isolation');
  } finally {
    if (browser) await browser.close();
    if (hub) { hub.kill('SIGTERM'); await new Promise(resolve => { hub.once('exit', resolve); setTimeout(() => { hub.kill('SIGKILL'); resolve(); }, 4000).unref(); }); }
    rmSync(project, { recursive: true, force: true });
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
