"""官网/邮箱投递执行器。prepare仅本地；execute必须有具体计划授权和已通过的岗位/成品。"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from workflow import Workflow, DEFAULT_ROOT, day, now, dumps, sha, file_sha, need, read, city_name

SCHEMA = '''
CREATE TABLE IF NOT EXISTS delivery_plans (
 id TEXT PRIMARY KEY, job TEXT NOT NULL, identity TEXT NOT NULL, channel TEXT NOT NULL,
 payload TEXT NOT NULL, policy_hash TEXT NOT NULL, created TEXT NOT NULL, authorization TEXT);
CREATE TABLE IF NOT EXISTS delivery_attempts (
 id INTEGER PRIMARY KEY, plan TEXT NOT NULL UNIQUE, run TEXT NOT NULL,
 job TEXT NOT NULL, identity TEXT NOT NULL, day TEXT NOT NULL, state TEXT NOT NULL,
 created TEXT NOT NULL, result TEXT);
'''


def origin(url):
    p = urlsplit(url)
    need(p.scheme == 'https' or (p.scheme == 'http' and p.hostname in ('localhost', '127.0.0.1')), '只允许HTTPS或本机测试页面')
    need(not p.username and not p.password and p.hostname, 'URL不能含认证信息')
    return p.scheme + '://' + p.netloc


class Delivery:
    def __init__(self, root):
        self.w = Workflow(root)
        self.root, self.db = root.resolve(), self.w.db
        self.db.executescript(SCHEMA)

    def policy_hash(self):
        p = self.w.prefs
        # Connection/authorization changes do not invalidate the reviewed job materials.
        return sha(dumps({k:p[k] for k in ('profile','search','resume_root')} | {
            'limits': {k:p['execution'][k] for k in ('max_per_run','max_per_day','max_runs_per_day')}}).encode())

    def materials(self, key):
        row = self.w.job(key)
        need(row['binding'], '须先绑定这个岗位已交付并核验的PDF')
        b = json.loads(row['binding'])
        need(b['jd_hash'] == row['jd_hash'], '岗位更新后须重新绑定材料')
        for path, expected in b['files'].items():
            need(Path(path).is_file() and file_sha(path) == expected, '成品/检查记录已变化：' + str(path))
        need(Path(b['pdf']).suffix.lower() == '.pdf', '投递附件须为已绑定PDF')
        return row, json.loads(row['body']), b

    def screening_errors(self, row):
        a = json.loads(row['assessment']) if row['assessment'] else {}
        errors = []
        if self.w.prefs['search'].get('setup_confirmed') is not True: errors.append('请先确认求职条件')
        if a.get('decision') != 'recommend': errors.append('岗位尚未通过完整评估')
        if a.get('jd_hash') != row['jd_hash']: errors.append('评估不属于当前JD')
        if a.get('profile_hash') != sha(dumps(self.w.prefs['profile']).encode()): errors.append('个人条件变化后须重评')
        index = Path(self.w.prefs['evidence_index'])
        if not index.is_file() or a.get('source_index_sha256') != file_sha(index): errors.append('素材更新后须重评')
        try:
            age = datetime.fromisoformat(now()) - datetime.fromisoformat(a['assessed_at'])
            if age > timedelta(hours=self.w.prefs['execution']['assessment_max_age_hours']): errors.append('评估过期')
        except (KeyError, ValueError): errors.append('缺少有效核验时间')
        body = json.loads(row['body'])
        if body.get('deadline'):
            try:
                if datetime.fromisoformat(str(body['deadline'])[:10]).date() < datetime.fromisoformat(now()).date(): errors.append('已超过招聘截止日期')
            except ValueError: errors.append('招聘截止日期需核实')
        if city_name(body['city']) not in [city_name(x) for x in self.w.prefs['search'].get('allowed_cities', [])]: errors.append('不在当前找岗城市范围')
        pay = self.w.salary_screen(body)['status']
        if pay not in ('posted_meets_floor','fixed_meets_floor','not_configured'): errors.append('工资门槛尚未确认')
        if any(r.get('status') != 'met' for r in a.get('hard_requirements', [])) or not a.get('hard_requirements'): errors.append('任职硬条件仍有缺口/未知')
        return errors

    def prepare(self, key, channel, specification):
        need(channel in ('email','web'), '仅支持email/web')
        row, body, binding = self.materials(key)
        payload = {'job':key,'company':body['company'],'role':body['role'],'city':body['city'],
                   'jd_hash':row['jd_hash'],'pdf':binding['pdf'],'pdf_sha256':file_sha(binding['pdf']),
                   'binding_hash':sha(row['binding'].encode()),'source_url':body['source_url'],
                   'channel':channel,'salary':body.get('salary',{}),'location_note':body.get('location_note',''),'related_roles':specification.get('related_roles',[]),'specification':specification}
        if channel == 'email':
            from mail_transport import build_message
            settings = self.w.prefs['submission_channels']['email']
            need(specification.get('recipient_source_url'), '收件邮箱必须保留招聘方公布的来源页')
            specification['sender'] = settings['sender']
            specification.pop('message_id', None)
            payload['smtp_transport'] = {k:settings[k] for k in ('host','port','sender')}
            # Stable per concrete job, recipient and attachment; it is a correlation ID, not server-side dedup.
            token = sha(dumps(payload).encode())[:32]
            specification['message_id'] = '<job-' + token + '@' + settings['sender'].split('@')[1] + '>'
            build_message(specification, Path(payload['pdf']))  # Validate before writing a draft.
        else:
            target = specification['application_url']
            need(origin(target) in specification['allowed_origins'], '官网投递目标不在已核实站点列表')
            need(specification.get('entry_source_url'), '必须保留企业公布投递入口的出处')
            need(len(specification.get('identity_checks', [])) >= 2, '必须分别校验当前公司及岗位')
            need(specification.get('submit_selector') and specification.get('success_check'), '缺少提交按钮或成功核验规则')
            for field in specification.get('fields', []):
                need(field.get('kind','fill') in ('fill','select'), '声明/协议/验证码不自动勾选或填写')
                need(field.get('source_id') and field.get('selector') and isinstance(field.get('value'),str), '字段必须有真实素材出处、定位和已确认值')
                index=read(self.w.prefs['evidence_index'])
                need(field['source_id'] in {x['id'] for x in index['items']}, '网申字段的素材ID不存在')
            need(specification.get('upload_selector'), '缺少简历上传框')
        plan_id = uuid.uuid4().hex[:16]
        folder = self.root / 'outbox' / plan_id
        folder.mkdir(parents=True, exist_ok=False)
        if channel == 'email':
            from mail_transport import build_message, write_draft
            write_draft(build_message(specification, Path(payload['pdf'])), folder/'application.eml')
            payload['draft_sha256'] = file_sha(folder/'application.eml')
        (folder/'plan.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
        with self.db:
            self.db.execute('INSERT INTO delivery_plans VALUES(?,?,?,?,?,?,?,NULL)',
                            (plan_id,key,row['identity'],channel,dumps(payload),self.policy_hash(),now()))
        return {'plan_id':plan_id,'channel':channel,'state':'prepared_local','folder':str(folder),
                'blockers':self.screening_errors(row),'external_actions':0}

    def plan(self, plan_id):
        row = self.db.execute('SELECT * FROM delivery_plans WHERE id=?',(plan_id,)).fetchone()
        need(row is not None, '找不到该投递计划')
        return dict(row)

    def authorize(self, plan_id, note, allow_review=False):
        p = self.plan(plan_id)
        need(isinstance(note,str) and len(note.strip()) >= 8, '须记录用户对具体岗位、收件人/站点及附件的实际授权')
        row=self.w.job(p['job']);assessment=json.loads(row['assessment']) if row['assessment'] else {}
        waivers=[]
        if allow_review:
            need(assessment.get('decision')=='review','例外只用于已评估的待核实岗位')
            need(not any(r.get('status')=='unmet' for r in assessment.get('hard_requirements',[])),'不允许例外放行明确不符的硬条件')
            career=assessment.get('career_assessment',{})
            need(career.get('chance_tier') in ('main','try') and career.get('confidence') in ('high','medium'),'须有可信的相关性判断')
            need(len({dumps(x.get('jd_task')) for x in career.get('task_matches',[])})>=2,'至少两项核心任务须有真实依据')
            allowed={'岗位尚未通过完整评估','工资门槛尚未确认','任职硬条件仍有缺口/未知'}
            errors=self.screening_errors(row)
            need(set(errors)<=allowed,'存在不能例外放行的错误：'+'；'.join(set(errors)-allowed))
            waivers=errors
        # This command records intent; it is not a substitute for actually obtaining user intent.
        with self.db:
            self.db.execute('UPDATE delivery_plans SET authorization=? WHERE id=?', (dumps({'note':note,'at':now(),'payload_hash':sha(p['payload'].encode()),'review_waivers':waivers,'assessment_hash':sha((row['assessment'] or '').encode())}),plan_id))
        return {'plan_id':plan_id,'state':'authorization_recorded','external_actions':0}

    def open_run(self):
        need(self.w.prefs['execution'].get('mode') == 'official_and_email', '真实官网/邮箱投递未启用；当前只准备本地计划')
        limits = self.w.prefs['execution']
        for k in ('max_per_run','max_per_day','max_runs_per_day'):
            need(type(limits.get(k)) is int and limits[k] > 0, '缺少有效配额：' + k)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            need(not self.db.execute("SELECT 1 FROM delivery_attempts WHERE state IN ('pending','unknown','blocked','filled_not_submitted') LIMIT 1").fetchone(), '须先核实或完成已有待处理投递')
            used=self.db.execute('SELECT COUNT(*) FROM runs WHERE day=?',(day(),)).fetchone()[0]
            need(used < limits['max_runs_per_day'], '已达每日执行轮次上限')
            run=uuid.uuid4().hex[:16]
            self.db.execute('INSERT INTO runs(id,day,policy_hash) VALUES(?,?,?)',(run,day(),self.policy_hash()))
            self.db.commit()
            return {'run_id':run,'day':day()}
        except Exception:
            self.db.rollback();raise

    def reserve(self, plan_id, run_id):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            need(self.w.prefs['execution'].get('mode') == 'official_and_email','未启用真实官网/邮箱投递')
            p=self.plan(plan_id);payload=json.loads(p['payload']);row,body,b=self.materials(p['job'])
            need(self.w.prefs['submission_channels'][p['channel']].get('enabled') is True, '该渠道未启用')
            need(p['authorization'], '该具体计划尚无发送授权')
            auth=json.loads(p['authorization']);need(auth['payload_hash']==sha(p['payload'].encode()),'授权后计划发生变化')
            need(p['policy_hash']==self.policy_hash(),'筛选条件或额度变化，须重新准备计划')
            errors=self.screening_errors(row)
            if auth.get('review_waivers'):
                need(auth.get('assessment_hash')==sha((row['assessment'] or '').encode()),'例外授权后的评估已变更，须重新核对授权')
                errors=[e for e in errors if e not in auth['review_waivers']]
            need(not errors,'；'.join(errors))
            need(payload['jd_hash']==row['jd_hash'] and payload['binding_hash']==sha(row['binding'].encode()),'岗位或简历版本已更新')
            need(file_sha(payload['pdf'])==payload['pdf_sha256'],'PDF与准备时不同')
            if p['channel']=='email':
                need(file_sha(self.root/'outbox'/plan_id/'application.eml')==payload['draft_sha256'],'EML草稿已变化，须重新准备授权')
                settings=self.w.prefs['submission_channels']['email']
                need(payload['smtp_transport']=={k:settings[k] for k in ('host','port','sender')},'邮件服务器/发件人变化，须重新准备')
            run=self.db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
            need(run and run['day']==day() and not run['halted'] and run['policy_hash']==self.policy_hash(),'执行轮次失效或停止')
            need(not self.db.execute("SELECT 1 FROM delivery_attempts WHERE state IN ('pending','unknown','blocked','filled_not_submitted') LIMIT 1").fetchone(),'有结果不明或待处理的投递，禁止继续')
            need(not self.db.execute("SELECT 1 FROM attempts WHERE state IN ('pending','unknown','blocked') LIMIT 1").fetchone(),'有旧通道结果待核实')
            need(not self.db.execute('SELECT 1 FROM delivery_attempts WHERE identity=? OR job=?',(p['identity'],p['job'])).fetchone(),'该岗位已有投递尝试，不自动重复')
            need(not self.db.execute('SELECT 1 FROM attempts WHERE identity=? OR job=?',(p['identity'],p['job'])).fetchone(),'旧通道已尝试，不跨渠道重复投递')
            if p['channel']=='email':
                previous=self.db.execute('SELECT p.payload FROM delivery_attempts a JOIN delivery_plans p ON p.id=a.plan WHERE a.day=?',(day(),)).fetchall()
                for item in previous:
                    prior=json.loads(item[0])
                    need(not (prior.get('company')==payload['company'] and prior.get('specification',{}).get('recipient')==payload['specification']['recipient']),'同公司同招聘邮箱今天已有投递，请先核对合并意向，避免重复发送')
            for key,clause,value in (('max_per_run','run=?',run_id),('max_per_day','day=?',day())):
                identities={r[0] for table in ('attempts','delivery_attempts') for r in self.db.execute('SELECT identity FROM '+table+' WHERE '+clause,(value,))}
                need(len(identities)<self.w.prefs['execution'][key], '已达'+key)
            cur=self.db.execute('INSERT INTO delivery_attempts(plan,run,job,identity,day,state,created) VALUES(?,?,?,?,?,?,?)',
                                (plan_id,run_id,p['job'],p['identity'],day(),'pending',now()))
            self.db.commit()
            return cur.lastrowid,payload
        except Exception:
            self.db.rollback();raise

    def finish(self, attempt, result):
        need(result.get('status') in ('smtp_accepted','web_confirmed','not_sent','unknown','blocked','filled_not_submitted'),'无效结果')
        a=self.db.execute('SELECT * FROM delivery_attempts WHERE id=?',(attempt,)).fetchone()
        need(a is not None and a['state'] in ('pending','unknown','blocked','filled_not_submitted'),'找不到待核实动作')
        plan=self.plan(a['plan']);payload=json.loads(plan['payload'])
        if result['status']=='smtp_accepted':
            e=result.get('evidence') or {}
            need(plan['channel']=='email' and e.get('smtp_code')==250 and e.get('message_id')==payload['specification']['message_id'] and e.get('recipient')==payload['specification']['recipient'], 'SMTP接受证据与邮件计划不符')
        if result['status']=='web_confirmed':
            e=result.get('evidence') or {}
            need(plan['channel']=='web' and e.get('job')==payload['job'] and e.get('url') and e.get('success_text'), '网页成功证据不完整或不属于当前岗位')
            need(origin(e['url']) in payload['specification']['allowed_origins'] and payload['specification']['success_check']['contains'] in e['success_text'], '网页成功证据与站点/提示规则不符')
        with self.db:
            self.db.execute('UPDATE delivery_attempts SET state=?,result=? WHERE id=?',(result['status'],dumps(result),attempt))
            if result['status'] not in ('smtp_accepted','web_confirmed'):
                self.db.execute('UPDATE runs SET halted=1 WHERE id=?',(a['run'],))
        return {'attempt_id':attempt,**result}

    def execute(self, plan_id, run, fill_only=False):
        p=self.plan(plan_id);payload=json.loads(p['payload'])
        need(not fill_only or p['channel']=='web','fill-only仅适用于官网，邮箱请先查看本地EML草稿')
        need(p['authorization'], '该具体计划尚无发送授权')
        need(self.w.prefs['execution'].get('mode')=='official_and_email', '当前没有启用真实投递')
        # Local credentials/runtime must exist before reserving or attempting external delivery.
        if p['channel']=='email':
            from mail_transport import load_secret,send_message
            from email.parser import BytesParser
            from email.policy import SMTP
            settings=self.w.prefs['submission_channels']['email']
            secret=load_secret(self.root/'private/smtp-auth.dpapi')
            draft=self.root/'outbox'/plan_id/'application.eml'
            need(file_sha(draft)==payload['draft_sha256'],'EML草稿已变化')
            message=BytesParser(policy=SMTP).parsebytes(draft.read_bytes())
        else:
            settings=self.w.prefs['submission_channels']['web']
            need(Path(settings['node_executable']).is_file(),'Node运行时不可用')
            need(Path(settings['playwright_module']).is_dir(),'Playwright运行库不可用')
        attempt,payload=self.reserve(plan_id,run)
        try:
            if p['channel']=='email':
                result=send_message(message,settings,secret)
                if result['status']=='smtp_accepted':
                    result['evidence']={'smtp_code':result['smtp_code'],'message_id':payload['specification']['message_id'],'recipient':payload['specification']['recipient'],'pdf_sha256':payload['pdf_sha256']}
            else:
                folder=self.root/'outbox'/plan_id
                job={'payload':payload,'profile_dir':str(self.root/'private/browser-profile'),
                     'playwright_module':settings['playwright_module'],'fill_only':fill_only,
                     'headed':bool(settings.get('headed',False)),'screenshot':str(folder/'page.png')}
                spec=folder/'execution.json';spec.write_text(json.dumps(job,ensure_ascii=False),encoding='utf-8')
                receipt=folder/'web-result.json'
                command=[settings['node_executable'],str(Path(__file__).with_name('web_executor.cjs')),'execute',str(spec),str(receipt)]
                subprocess.run(command,check=False,capture_output=True,timeout=180)
                result=read(receipt) if receipt.is_file() else {'status':'unknown','reason':'浏览器未返回结果，保留待核实，不重发'}
        except Exception as exc:
            result={'status':'unknown','reason':'执行中断：'+type(exc).__name__+'；不自动重试'}
        finished = self.finish(attempt,result)
        if result['status'] == 'smtp_accepted':
            # Archive only after durable SMTP acceptance; copy failure must never trigger resend.
            try:
                from mail_sent_copy import save_sent_copy
                finished['sent_copy'] = save_sent_copy(self.root, plan_id)
            except Exception as exc:
                finished['sent_copy'] = {'status':'copy_unconfirmed','error_type':type(exc).__name__,
                                         'smtp_resent':False}
        try:
            folder=self.root/'outbox'/plan_id
            (folder/'result.json').write_text(json.dumps({'recorded_at':now(),**finished},ensure_ascii=False,indent=2),encoding='utf-8')
            from report import export_csv
            finished['report_csv']=str(export_csv(self.root,self.root/'applications.csv'))
        except Exception as exc:
            finished['local_report_warning']=type(exc).__name__
        return finished

    def login_web(self, plan_id):
        need(sys.stdin.isatty(),'网页登录只在本机交互终端启动，由用户手动完成')
        p=self.plan(plan_id);need(p['channel']=='web','该计划不是官网表单')
        payload=json.loads(p['payload']);settings=self.w.prefs['submission_channels']['web']
        folder=self.root/'outbox'/plan_id
        spec=folder/'login.json'
        spec.write_text(json.dumps({'payload':payload,'profile_dir':str(self.root/'private/browser-profile'),
                                  'playwright_module':settings['playwright_module'],'headed':True},ensure_ascii=False),encoding='utf-8')
        result=folder/'login-result.json'
        subprocess.run([settings['node_executable'],str(Path(__file__).with_name('web_executor.cjs')),'login',str(spec),str(result)],check=True)
        return read(result)

    def status(self):
        channels=self.w.prefs.get('submission_channels',{})
        return {'execution_mode':self.w.prefs['execution'].get('mode'),
                'email_sender':channels.get('email',{}).get('sender'),
                'email_credential_present':(self.root/'private/smtp-auth.dpapi').is_file(),
                'web_live_sites_verified':channels.get('web',{}).get('verified_live_sites',[]),
                'prepared_plans':self.db.execute('SELECT COUNT(*) FROM delivery_plans').fetchone()[0],
                'results':dict(self.db.execute('SELECT state,COUNT(*) FROM delivery_attempts GROUP BY state').fetchall()),
                'today_attempts':self.db.execute('SELECT COUNT(*) FROM delivery_attempts WHERE day=?',(day(),)).fetchone()[0]}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    s=p.add_subparsers(dest='command',required=True)
    q=s.add_parser('prepare');q.add_argument('--job',required=True);q.add_argument('--channel',choices=('email','web'),required=True);q.add_argument('--file',required=True)
    q=s.add_parser('show');q.add_argument('--plan',required=True)
    q=s.add_parser('authorize');q.add_argument('--plan',required=True);q.add_argument('--note',required=True);q.add_argument('--allow-review',action='store_true')
    s.add_parser('open-run')
    s.add_parser('status')
    q=s.add_parser('archive');q.add_argument('--plan',required=True)
    q=s.add_parser('report');q.add_argument('--csv')
    q=s.add_parser('execute');q.add_argument('--plan',required=True);q.add_argument('--run',required=True);q.add_argument('--fill-only',action='store_true')
    q=s.add_parser('resolve');q.add_argument('--attempt',type=int,required=True);q.add_argument('--file',required=True)
    q=s.add_parser('setup-mail')
    q=s.add_parser('login-web');q.add_argument('--plan',required=True)
    a=p.parse_args();d=None
    try:
        d=Delivery(a.root)
        if a.command=='prepare':out=d.prepare(a.job,a.channel,read(a.file))
        elif a.command=='show':out=d.plan(a.plan);out['payload']=json.loads(out['payload'])
        elif a.command=='authorize':out=d.authorize(a.plan,a.note,a.allow_review)
        elif a.command=='archive':
            from mail_sent_copy import save_sent_copy
            out=save_sent_copy(d.root,a.plan)
        elif a.command=='report':
            from report import records,export_csv
            out={'applications':records(d.root)}
            if a.csv:out['csv']=str(export_csv(d.root,Path(a.csv)))
        elif a.command=='open-run':out=d.open_run()
        elif a.command=='execute':out=d.execute(a.plan,a.run,a.fill_only)
        elif a.command=='resolve':out=d.finish(a.attempt,read(a.file))
        elif a.command=='login-web':out=d.login_web(a.plan)
        elif a.command=='status':out=d.status()
        else:
            from getpass import getpass
            from mail_transport import save_secret
            need(sys.stdin.isatty(),'授权码只在本机交互终端输入，不从管道或聊天接收')
            code=getpass('QQ邮箱16位SMTP授权码（不回显，不是QQ密码）：').strip()
            need(len(code)==16 and code.isascii() and code.isalnum(),'授权码格式无效')
            save_secret(d.root/'private/smtp-auth.dpapi',code);out={'credential':'saved_encrypted_for_current_windows_user','secret_printed':False}
        print(json.dumps(out,ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError,KeyError,sqlite3.Error,subprocess.SubprocessError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr);return 1
    finally:
        if d:d.db.close()


if __name__=='__main__':raise SystemExit(main())
