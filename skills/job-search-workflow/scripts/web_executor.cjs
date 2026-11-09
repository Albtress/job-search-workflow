// Deterministic form adapter on the bundled Microsoft Playwright SDK. No model calls.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

function ensure(test, message) { if (!test) throw new Error(message); }
function allowed(url, origins) { return origins.includes(new URL(url).origin); }

async function run(mode, input, output) {
  const spec = JSON.parse(fs.readFileSync(input, 'utf8'));
  const { chromium } = require(spec.playwright_module);
  const payload = spec.payload;
  const adapter = payload.specification;
  const origins = adapter.allowed_origins;
  ensure(allowed(adapter.application_url, origins), '目标网址未授权');
  let context, page, submitStarted = false, interacted = false;
  let result = {status: 'not_sent', reason: '尚未操作表单'};
  try {
    context = await chromium.launchPersistentContext(spec.profile_dir, {
      channel: 'chrome', headless: mode === 'login' ? false : !spec.headed, acceptDownloads: false,
    });
    context.setDefaultTimeout(12000);
    page = context.pages()[0] || await context.newPage();
    await page.route('**/*', async route => {
      const req = route.request();
      if (req.isNavigationRequest() && req.frame() === page.mainFrame() && !allowed(req.url(), origins)) return route.abort();
      return route.continue();
    });
    await page.goto(adapter.application_url, {waitUntil: 'domcontentloaded', timeout: 45000});
    ensure(allowed(page.url(), origins), '跳到了未授权域名');
    if (mode === 'login') {
      process.stdout.write('请在浏览器内自行完成登录/验证，不发送申请。完成后回到本窗口按回车保存独立会话。\n');
      await new Promise(resolve => { process.stdin.resume(); process.stdin.once('data', resolve); });
      result = {status:'login_session_saved', url:page.url(), submitted:false,
        note:'仅保存用户手动登录后的独立会话，不代表已验证表单或投递成功'};
      return result;
    }
    const visibleText = await page.locator('body').innerText();
    const challenge = /请完成.*验证|安全验证|滑块验证|验证码|captcha|sign in|请先登录/i.test(visibleText);
    if (challenge) {
      result = {status: 'blocked', reason: '检测到登录/验证，须用户接管，未填写或上传', url: page.url()};
    } else {
      for (const check of adapter.identity_checks) {
        const item = page.locator(check.selector);
        ensure(await item.count() === 1, '公司/岗位校验元素不唯一');
        ensure((await item.innerText()).includes(check.contains), '当前表单不是准备的公司/岗位');
      }
      const form = page.locator(adapter.form_selector || 'form');
      ensure(await form.count() === 1, '表单不唯一，需要站点适配');
      const mapped = new Set(adapter.fields.map(x => x.selector));
      const required = await form.locator('input[required],textarea[required],select[required]').evaluateAll(items => items.map(e => ({id:e.id, type:e.type, value:e.value, checked:e.checked, name:e.name})));
      for (const item of required) {
        if (item.type === 'file') continue;
        if (item.type === 'checkbox' || item.type === 'radio') {
          ensure(item.checked, '存在未完成的必选声明，须用户接管'); continue;
        }
        ensure(item.value || (item.id && mapped.has('#' + item.id)), '存在未知必填字段，未填写');
      }
      for (const field of adapter.fields) {
        ensure(['fill','select'].includes(field.kind || 'fill'), '不自动勾选声明或处理验证');
        const control = page.locator(field.selector);
        ensure(await control.count() === 1, '字段定位不唯一');
        ensure(await control.getAttribute('type') !== 'password' && await control.getAttribute('autocomplete') !== 'one-time-code', '密码/验证码由用户手动输入');
        interacted = true;
        if (field.kind === 'select') await control.selectOption(field.value);
        else await control.fill(field.value);
      }
      const upload = page.locator(adapter.upload_selector);
      ensure(await upload.count() === 1, '附件上传框不唯一');
      if (payload.pdf_sha256) ensure(crypto.createHash('sha256').update(fs.readFileSync(payload.pdf)).digest('hex') === payload.pdf_sha256, '上传前PDF发生变化');
      interacted = true;
      await upload.setInputFiles(payload.pdf);
      const filenames = await upload.evaluate(e => Array.from(e.files || []).map(f => f.name));
      ensure(filenames.includes(path.basename(payload.pdf)), '网页附件名与绑定PDF不符');
      const invalid = await form.locator(':invalid').count();
      ensure(invalid === 0, '表单校验未通过，不能提交');
      if (spec.screenshot) await page.screenshot({path:spec.screenshot, fullPage:true});
      if (mode === 'fill' || spec.fill_only) {
        result = {status:'filled_not_submitted', reason:'已填表并上传；可能触发网站保存，未点击提交', evidence:{url:page.url(), attachment:path.basename(payload.pdf)}};
      } else {
        const button = page.locator(adapter.submit_selector);
        ensure(await button.count() === 1 && await button.isEnabled(), '提交按钮不可唯一确认');
        // A success string already present before clicking is not submission evidence.
        const success = page.locator(adapter.success_check.selector);
        ensure(!(await success.isVisible().catch(() => false)), '成功标记在点击前已存在，适配规则无效');
        submitStarted = true;
        await button.click({noWaitAfter:true});
        await success.waitFor({state:'visible',timeout:25000});
        const message = await success.innerText();
        ensure(message.includes(adapter.success_check.contains), '没有找到该动作的成功提示');
        ensure(allowed(page.url(),origins), '提交后目标域名变化，待核实');
        result = {status:'web_confirmed', evidence:{url:page.url(),success_text:message,job:payload.job,attachment:path.basename(payload.pdf)}};
        if (spec.screenshot) await page.screenshot({path:spec.screenshot,fullPage:true});
      }
    }
  } catch (error) {
    result = {status:submitStarted ? 'unknown' : interacted ? 'blocked' : 'not_sent',
      reason:String(error.message).slice(0,300), interacted, submit_started:submitStarted};
  } finally {
    if (context) await context.close().catch(() => {});
    fs.writeFileSync(output, JSON.stringify(result,null,2),'utf8');
  }
  return result;
}

if (require.main === module) {
  run(process.argv[2],process.argv[3],process.argv[4]).then(result => {
    process.stdout.write(JSON.stringify(result)+'\n');
    process.exitCode = ['web_confirmed','filled_not_submitted','login_session_saved'].includes(result.status) ? 0 : 1;
  }).catch(error => { process.stderr.write(JSON.stringify({status:'not_sent',reason:error.message})+'\n'); process.exitCode=1; });
}
module.exports = { run };
