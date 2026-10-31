// Real four-second refreshes against generated local home data, never a live API.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { connect } = require('./dashboard_polish_chrome.cjs');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main(settings) {
  const origin = new URL(settings.baseUrl);
  assert.equal(origin.hostname, 'localhost');
  const pages = await (await fetch(`http://127.0.0.1:${settings.debugPort}/json/list`)).json();
  const cdp = await connect(pages.find(page => page.type === 'page').webSocketDebuggerUrl, origin.origin);
  const evidence = { cases: [], controls: [], requests: [], exceptions: [], remoteRequests: [] };
  const literal = JSON.stringify;
  const legacy = settings.mode === 'legacy';
  const draftField = legacy ? 'question-text' : 'hub-question-text';
  async function waitFor(expression, label) {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      if (await cdp.evaluate(expression)) return;
      await delay(60);
    }
    throw new Error('Dashboard did not reach expected state: ' + label);
  }
  const click = id => cdp.evaluate(`document.getElementById(${literal(id)}).click()`);
  async function key(key, code = 'Key' + key.toUpperCase()) {
    await cdp.call('Input.dispatchKeyEvent', { type: 'keyDown', key, code });
    await cdp.call('Input.dispatchKeyEvent', { type: 'keyUp', key, code });
  }
  async function screenshot(name) {
    const { data } = await cdp.call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(settings.evidence, name + '.png'), Buffer.from(data, 'base64'));
  }
  async function checkScroll(label, horizontal) {
    await cdp.evaluate(`(() => {
      document.getElementById(${literal(draftField)}).focus({preventScroll:true});
      const field=document.activeElement;
      field.value='Unsaved scroll regression draft';
      field.dispatchEvent(new Event('input',{bubbles:true}));
      field.setSelectionRange(3,19);
      window.__oldColumns=Array.from(document.querySelectorAll('#tasks .board-column'));
      window.__scrollNodes=() => Array.from(document.querySelectorAll('*')).filter(node => {
        if (!node.getClientRects().length || node===document.scrollingElement) return false;
        const css=getComputedStyle(node);
        return (/auto|scroll/.test(css.overflowY) && node.scrollHeight>node.clientHeight+1) ||
          (/auto|scroll/.test(css.overflowX) && node.scrollWidth>node.clientWidth+1);
      });
      window.__scrollPath=node => {
        const children=[];
        while(!node.id && node.parentElement) {
          children.unshift(Array.from(node.parentElement.children).indexOf(node));
          node=node.parentElement;
        }
        return {id:node.id,children};
      };
      window.__scrollPosition=() => ({x:window.scrollX,y:window.scrollY,
        containers:window.__scrollNodes().map(node=>({...window.__scrollPath(node),top:node.scrollTop,left:node.scrollLeft}))});
      for(const node of window.__scrollNodes()) {
        if(node.scrollHeight>node.clientHeight+1) node.scrollTop=Math.min(227,Math.floor((node.scrollHeight-node.clientHeight)/2));
        if(node.scrollWidth>node.clientWidth+1) node.scrollLeft=Math.min(331,Math.floor((node.scrollWidth-node.clientWidth)/2));
      }
      window.scrollTo(0,1500);
    })()`);
    // Let native CSS scroll snapping settle before recording its stable coordinate.
    await delay(350);
    const refreshPath = legacy ? '/v1/dashboard/state' : '/api/state';
    const beforeRequests = cdp.requests.filter(request => request.path === refreshPath).length;
    await cdp.evaluate(`(() => {
      const baseline=window.__scrollPosition();
      window.__scrollLayout=()=>({cssRules:Array.from(document.styleSheets,sheet=>sheet.cssRules.length),
        htmlMinHeight:getComputedStyle(document.documentElement).minHeight,bodyMinHeight:getComputedStyle(document.body).minHeight});
      window.__scrollReport={baseline,layout:window.__scrollLayout(),samples:[],drift:[],started:performance.now()};
      window.__scrollSampling=setInterval(()=>{
        const current=window.__scrollPosition(), elapsed=performance.now()-window.__scrollReport.started;
        window.__scrollReport.samples.push({elapsed,...current});
        if(JSON.stringify(current)!==JSON.stringify(baseline)) window.__scrollReport.drift.push({elapsed,...current});
      },125);
    })()`);
    await screenshot('scroll-' + label + '-before');
    await delay(12500);
    const report = await cdp.evaluate(`(() => {
      clearInterval(window.__scrollSampling);
      return {...window.__scrollReport,elapsed:performance.now()-window.__scrollReport.started,
        final:window.__scrollPosition(),
        finalLayout:window.__scrollLayout(),
        recreatedColumns:window.__oldColumns.length && window.__oldColumns.every(node=>!node.isConnected),
        field:{id:document.activeElement.id,value:document.activeElement.value,start:document.activeElement.selectionStart,end:document.activeElement.selectionEnd},
        discussionOpen:!!document.querySelector('#huddle-list .talk'),
        detailsOpen:document.querySelector('#tasks details')?.open,
        transcript:document.querySelector('#messages .message:last-child')?.textContent,
        inlineStyles:document.querySelectorAll('[style]').length,
        consoleErrors:window.__scrollConsoleErrors,cspViolations:window.__scrollCspViolations};
    })()`);
    report.label = label;
    report.refreshes = cdp.requests.filter(request => request.path === refreshPath).length - beforeRequests;
    evidence.cases.push(report);
    await screenshot('scroll-' + label + '-after');
    assert.ok(report.elapsed >= 12000, 'waits at least twelve seconds of real polling');
    assert.ok(report.refreshes >= 3, 'at least three real four-second refreshes');
    assert.equal(report.baseline.y, 1500, 'the page is actually scrolled to about 1500px');
    assert.deepEqual(report.final, report.baseline, label + ' preserves window and every visible scrollable top/left');
    assert.deepEqual(report.drift, [], label + ' has no intermediate scroll jump between refreshes');
    assert.deepEqual(report.finalLayout, report.layout, 'temporary CSSOM rules and page min-height are fully cleaned up');
    assert.ok(report.baseline.containers.some(row => row.id === 'messages' && row.top > 0), 'transcript is partway down');
    for (const id of ['news', 'patches', 'audit', 'claims', 'questions']) {
      assert.ok(report.baseline.containers.some(row => row.id === id && row.top > 0), id + ' is partway down');
    }
    if (horizontal) {
      for (const id of ['timeline-track', 'section-nav']) {
        assert.ok(report.baseline.containers.some(row => row.id === id && row.left > 0), id + ' is horizontally scrolled');
      }
    } else {
      assert.equal(report.recreatedColumns, true, 'anonymous board column nodes were really replaced');
      assert.equal(report.baseline.containers.filter(row => row.id === 'tasks' && row.top > 0).length, 4, 'all four board columns are partway down');
    }
    assert.deepEqual(report.field, {id:draftField,value:'Unsaved scroll regression draft',start:3,end:19}, 'preserveView keeps draft, focus and selection');
    if (legacy) assert.equal(report.detailsOpen, true, 'native task disclosure stays open through redraws');
    else assert.equal(report.discussionOpen, true, 'open huddle discussion survives redraws');
    assert.ok(report.transcript.includes('Refreshed local transcript version'), 'the live transcript content really changes');
    assert.equal(report.inlineStyles, 0, 'no inline style attributes');
    assert.deepEqual(report.consoleErrors, [], 'no console errors');
    assert.deepEqual(report.cspViolations, [], 'no CSP violations');
  }

  try {
    await cdp.call('Page.enable');
    await cdp.call('Runtime.enable');
    await cdp.call('Network.enable');
    await cdp.call('Fetch.enable', {patterns:[{urlPattern:'*'}]});
    await cdp.call('Emulation.setDeviceMetricsOverride', {width:1440,height:900,deviceScaleFactor:1,mobile:false});
    await cdp.call('Browser.setDownloadBehavior', {behavior:'allow',downloadPath:settings.downloads});
    await cdp.call('Page.addScriptToEvaluateOnNewDocument', {source:`
      window.__scrollConsoleErrors=[];window.__scrollCspViolations=[];
      const original=console.error;
      console.error=function(...args){window.__scrollConsoleErrors.push(args.map(String).join(' '));return original.apply(this,args);};
      document.addEventListener('securitypolicyviolation',event=>window.__scrollCspViolations.push(event.violatedDirective));
    `});
    await cdp.call('Page.navigate', {url:origin.href});
    await waitFor(`document.readyState==='complete' && document.getElementById('access-key') && !document.getElementById('access-key').disabled && !document.getElementById('auth').hidden`, 'local sign-in');
    await cdp.evaluate(`document.getElementById('access-key').value=${literal(settings.credential)};document.getElementById('access-form').requestSubmit()`);
    await waitFor(`document.getElementById('auth').hidden && document.querySelectorAll('#tasks .task-card').length===${settings.tasks} && document.querySelectorAll('#messages .message').length>60`, 'long local dashboard');
    evidence.controls.push((legacy ? 'legacy agent' : 'member') + ' sign-in');
    if (legacy) await cdp.evaluate(`document.querySelector('#tasks details').open=true`);
    else {
      await cdp.evaluate(`Array.from(document.querySelectorAll('#huddle-list button')).find(button=>button.textContent.startsWith('Show discussion')).click()`);
      await waitFor(`!!document.querySelector('#huddle-list .talk')`, 'open huddle discussion');
    }
    await checkScroll('board', false);

    if (!legacy) {
      await cdp.call('Emulation.setDeviceMetricsOverride', {width:1000,height:900,deviceScaleFactor:1,mobile:false});
      await click('timeline-view');
      await waitFor(`!document.getElementById('timeline-panel').hidden && document.querySelectorAll('.timeline-entry').length>60`, 'horizontal timeline');
      await checkScroll('timeline', true);
    }

    await click('board-view');
    await cdp.evaluate(`document.querySelector('#tasks .task-card').focus()`);
    const first = await cdp.evaluate('document.activeElement.id');
    await key('j');
    assert.notEqual(await cdp.evaluate('document.activeElement.id'), first, 'j changes the focused card');
    await key('k');
    assert.equal(await cdp.evaluate('document.activeElement.id'), first, 'k returns to the previous card');
    await key('/', 'Slash');
    assert.equal(await cdp.evaluate('document.activeElement.id'), 'global-search', '/ intentionally focuses search');
    await key('j');
    assert.equal(await cdp.evaluate('document.activeElement.id'), 'global-search', 'shortcuts do not interrupt typing');
    await cdp.evaluate(`const field=document.getElementById('global-search');field.value='Scroll fixture open task 9';field.dispatchEvent(new Event('input',{bubbles:true}))`);
    await waitFor(`!!document.querySelector('#search-results a[data-kind="task"]')`, 'search result');
    await cdp.evaluate(`document.querySelector('#search-results a[data-kind="task"]').click()`);
    assert.ok(await cdp.evaluate(`document.activeElement.textContent.includes('Scroll fixture open task 9')`), 'search intentionally reveals and focuses its task');
    await cdp.evaluate('document.activeElement.blur()');
    await key('?', 'Slash');
    assert.equal(await cdp.evaluate(`document.getElementById('shortcuts').open`), true);
    await key('Escape', 'Escape');
    assert.equal(await cdp.evaluate(`document.getElementById('shortcuts').open`), false);
    await key(']', 'BracketRight');
    assert.equal(await cdp.evaluate(`document.querySelector('#section-nav [aria-current="page"]').dataset.section`), 'transcript');
    await key('[', 'BracketLeft');
    assert.equal(await cdp.evaluate(`document.querySelector('#section-nav [aria-current="page"]').dataset.section`), 'board');
    evidence.controls.push('j/k, search focus, typing guard, help, section group shortcuts, search reveal');

    const sections = await cdp.evaluate(`Array.from(document.querySelectorAll('#section-nav [data-section-link]'),node=>node.dataset.section)`);
    assert.equal(sections.length, 13);
    for (const section of sections) {
      await cdp.evaluate(`document.querySelector('#section-nav [data-section="${section}"]').click()`);
      assert.ok(await cdp.evaluate(`(() => {const link=document.querySelector('#section-nav [aria-current="page"]');return link?.dataset.section===${literal(section)} && link.getAttribute('aria-controls').split(' ').some(id=>document.getElementById(id)?.getClientRects().length);})()`), section + ' remains navigable');
    }
    evidence.controls.push('all thirteen sections');
    for (const button of ['export-board', 'export-transcript']) {
      const previous = new Set(fs.readdirSync(settings.downloads));
      await click(button);
      const deadline = Date.now() + 10000;
      let downloaded;
      while (Date.now() < deadline) {
        downloaded = fs.readdirSync(settings.downloads).find(name => !previous.has(name) && name.endsWith('.md'));
        if (downloaded) break;
        await delay(50);
      }
      assert.ok(downloaded, button + ' downloads Markdown');
      const markdown = fs.readFileSync(path.join(settings.downloads, downloaded), 'utf8');
      assert.ok(markdown.includes(button === 'export-board' ? 'Scroll fixture open task' : 'Refreshed local transcript version'));
      assert.ok(!markdown.includes(settings.credential));
      fs.copyFileSync(path.join(settings.downloads, downloaded), path.join(settings.evidence, button + '.md'));
    }
    evidence.controls.push('board and transcript exports contain local loaded data');
    await click('sign-out');
    await waitFor(`!document.getElementById('auth').hidden && document.getElementById('workspace').hidden`, 'sign-out');
    evidence.controls.push('sign-out');
    assert.deepEqual(cdp.exceptions, [], 'no uncaught browser exceptions');
    assert.deepEqual(cdp.remoteRequests, [], 'no external hosts');
    assert.deepEqual(await cdp.evaluate('window.__scrollConsoleErrors'), []);
    assert.deepEqual(await cdp.evaluate('window.__scrollCspViolations'), []);
    console.log('dashboard-scroll-chrome-ok: real 4s refreshes, window/all container top+left, changing transcript, recreated columns, preserved drafts/focus/selection/discussion, shortcuts/search/exports/sections/sign-in');
  } catch (error) {
    evidence.error = String(error.stack || error).replaceAll(settings.credential, '[redacted]');
    await screenshot('scroll-failure').catch(() => {});
    throw error;
  } finally {
    evidence.requests = cdp.requests;
    evidence.exceptions = cdp.exceptions;
    evidence.remoteRequests = cdp.remoteRequests;
    fs.writeFileSync(path.join(settings.evidence, 'scroll-evidence.json'), JSON.stringify(evidence, null, 2) + '\n');
    cdp.close();
  }
}

(async () => {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  await main(JSON.parse(input));
})().catch(error => {console.error(error);process.exitCode=1;});
