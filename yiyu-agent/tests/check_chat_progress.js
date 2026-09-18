// NODE_PATH=<existing Playwright node_modules> node tests/check_chat_progress.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE });
  try {
    const page = await browser.newPage();
    await page.clock.install();
    await page.clock.pauseAt(new Date(Date.now() + 1000));
    await page.setContent('<div id="route-view"></div><div id="topbar"></div>');
    await page.addStyleTag({ content: fs.readFileSync('web/css/chat.css', 'utf8') });
    await page.evaluate(() => {
      window.Yiyu = {
        data: { user: { name: '测试' } },
        md: { normalizeRef: String, render: text => ({ html: text }) },
        store: { getCognition: () => [] },
        api: {
          isAuthed: () => true, isDemo: () => false,
          streamChat: ({ onEvent }) => new Promise((resolve, reject) => {
            window.emit = onEvent; window.endStream = resolve; window.rejectStream = reject;
          }),
        },
      };
    });
    await page.addScriptTag({ content: fs.readFileSync('web/js/pages/chat.js', 'utf8').replace(
      'Yiyu.chat = { render, reset };', 'Yiyu.chat = { render, reset, runResearch };',
    ) });
    const start = () => page.evaluate(() => {
      Yiyu.chat.render(document.querySelector('#route-view'), document.querySelector('#topbar'));
      window.research = Yiyu.chat.runResearch('分析智谱');
    });
    const emit = (type, metadata = {}, content = '') => page.evaluate(
      event => window.emit(event), { type, metadata, content },
    );
    const visibleStages = () => page.locator('.stage:visible').count();
    const sub = index => page.locator(`[data-stage="${index}"] .stage-sub`).textContent();
    await start();
    assert.equal(await visibleStages(), 1);
    for (let seconds = 0; seconds <= 6; seconds++) {
      if (seconds) await page.clock.runFor(1000);
      assert.match(await sub(0), new RegExp(`已进行 ${seconds} 秒`));
    }
    await emit('preloop_progress', { elapsed_sec: 0 });
    assert.match(await sub(0), /已进行 6 秒/);
    const plan = { focus_items: [
      { title: '生意质量', summary: '研究需求' },
      { title: '竞争优势', summary: '研究壁垒' },
      { title: '主要风险', summary: '<script>bad()</script>' },
    ] };
    await emit('preloop', plan);
    assert.equal(await visibleStages(), 2);
    assert.equal(await sub(0), '已明确分析重点');
    assert.equal(await page.locator('.plan-panel').count(), 0);
    await emit('progress', { stage: 'synthesizing' });
    assert.equal(await visibleStages(), 3);
    assert.equal(await page.locator('.focus-item:visible').count(), 0);
    for (let count = 1; count <= 3; count++) {
      await page.clock.runFor(250);
      assert.equal(await page.locator('.focus-item:visible').count(), count);
    }
    await emit('plan', plan);
    assert.equal(await page.locator('.focus-item:visible').count(), 3);
    await emit('progress', { stage: 'evidence_check' });
    assert.equal(await page.locator('[data-stage="1"]').getAttribute('data-status'), 'done');
    await page.clock.runFor(1250);
    assert.match(await sub(2), /已进行 2 秒/);
    await emit('answer_delta', {}, '正文片段');
    assert.equal(await page.locator('.md-stream').textContent(), '正文片段');
    await emit('final_answer', {}, '最终报告');
    await page.evaluate(() => { endStream({ ok: true }); return research; });
    assert.equal(await page.locator('.stage[data-status="running"]').count(), 0);
    assert.equal(await page.locator('.thinking.collapsed').count(), 1);
    assert.equal(await page.locator('#send-btn').isEnabled(), true);
    const completed = await sub(2);
    await page.clock.runFor(5000);
    assert.equal(await sub(2), completed);

    for (const ending of ['error', 'disconnect', 'throw']) {
      await start();
      await page.clock.runFor(1000);
      if (ending === 'error') await emit('error', {}, '研究失败');
      await page.evaluate(ending => {
        if (ending === 'throw') rejectStream(new Error('network'));
        else endStream({ ok: ending !== 'disconnect', error: '连接中断' });
        return research;
      }, ending);
      assert.equal(await page.locator('.thinking-title').textContent(), '分析已停止');
      assert.equal(await page.locator('.stage[data-status="running"]').count(), 0);
      assert.equal(await page.locator('#send-btn').isEnabled(), true);
      await page.clock.runFor(5000);
      assert.equal(await sub(0), '分析已停止');
    }
    console.log('chat progress browser regression: ok');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
