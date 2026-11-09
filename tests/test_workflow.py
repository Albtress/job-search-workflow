import copy,hashlib,json,sqlite3,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from email.parser import BytesParser
from email.policy import SMTP
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject

SCRIPTS=Path(__file__).resolve().parents[1]/'skills/job-search-workflow/scripts'
sys.path.insert(0,str(SCRIPTS))
from workflow import Workflow,initialize,read,file_sha,dumps
from submissions import Delivery
from mail_transport import build_message,send_message
from report import records
from mail_sent_copy import save_sent_copy as real_save_sent_copy

def pdf_fixture(path,pages=1):
    writer=PdfWriter()
    for _ in range(pages):
        page=writer.add_blank_page(width=595,height=842)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 40 750 Td (Synthetic resume evidence for isolated workflow tests only.) Tj ET')
        page[NameObject('/Contents')]=writer._add_object(stream)
    writer.write(path)

class FakeSMTP:
    sends=0
    fail_reply=False
    def __init__(self,*args,**kwargs):pass
    def login(self,*args):return (235,b'ok')
    def mail(self,*args):return (250,b'ok')
    def rcpt(self,*args):return (250,b'ok')
    def docmd(self,*args):return (354,b'go')
    def send(self,wire):type(self).sends+=1
    def getreply(self):
        if self.fail_reply:raise OSError('simulated disconnect after DATA')
        return (250,b'accepted')
    def quit(self):pass
    def close(self):pass

class FakeIMAP:
    messages=[]
    appends=0
    def __init__(self,*args,**kwargs):pass
    def login(self,*args):return ('OK',[])
    def select(self,*args,**kwargs):return ('OK',[str(len(self.messages)).encode()])
    def uid(self,command,*args):
        if command=='SEARCH':
            if args[-1]=='ALL':return ('OK',[b' '.join(str(n+1).encode() for n in range(len(self.messages)))])
            return ('OK',[b''])  # Simulate QQ's false-negative HEADER search.
        if command=='FETCH':
            ids=str(args[0]).split(',')
            return ('OK',[(('1 (UID '+uid+' BODY[] {1})').encode(),self.messages[int(uid)-1]) for uid in ids])
        raise AssertionError(command)
    def append(self,mailbox,flags,date,raw):
        type(self).messages.append(raw);type(self).appends+=1;return ('OK',[b'appended'])
    def response(self,*args):return ('UIDVALIDITY',[b'1'])
    def logout(self):pass

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'local-data';initialize(self.root)
        config=read(self.root/'preferences.json');config['profile'].update(name='Synthetic Applicant',degree='硕士',major='示例专业')
        config['search'].update(setup_confirmed=True,allowed_cities=['示例城市'],salary_policy={'city_floors':{},'default_floor':8000})
        config['execution']['mode']='official_and_email';config['submission_channels']['email'].update(enabled=True,sender='applicant@example.invalid')
        self.write(self.root/'preferences.json',config)
        self.write(self.root/'evidence.json',{'items':[{'id':'S01','text':'synthetic confirmed fact','usable':True}]})
        self.d=Delivery(self.root);FakeSMTP.sends=0;FakeSMTP.fail_reply=False
        self.patches=[patch('mail_transport.load_secret',return_value='synthetic-not-a-real-secret'),patch('mail_sent_copy.load_secret',return_value='synthetic-not-a-real-secret'),patch('mail_transport.smtplib.SMTP_SSL',FakeSMTP),patch('mail_sent_copy.save_sent_copy',return_value={'status':'saved_verified','smtp_resent':False})]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.d.db.close();self.temp.cleanup()
    def write(self,path,value):path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    def job(self,external='sample',salary=True,unknown=False,unmet=False,company='Synthetic Company',role='示例研究岗位'):
        body={'platform':'employer_site','external_id':external,'company':company,'role':role,'city':'示例城市','source_url':'https://employer.example.invalid/jobs/'+external,'jd':'Synthetic full JD: experimental planning and scientific reporting.','role_family':'研究','salary':{'currency':'CNY','period':'month','advertised_monthly_min':9000,'advertised_monthly_max':11000} if salary else {}}
        key=self.d.w.ingest([body])[0]['id']
        assessment={'decision':'recommend','fit_reason':'两项真实素材所覆盖的任务，虚构测试数据','greeting':'您好，示例申请','source_ids':['S01'],'hard_requirements':[{'requirement':'示例专业','status':'unmet' if unmet else 'unknown' if unknown else 'met','evidence':'synthetic requirement evidence'}],
                    'career_assessment':{'relation':'adjacent','chance_tier':'try','confidence':'medium','reason':'synthetic match','task_matches':[{'jd_task':x,'evidence':'synthetic fact','source_ids':['S01']} for x in ('实验方案','报告整理')]}}
        self.d.w.assess(key,assessment)
        directory=self.root/'resumes'/external;directory.mkdir();pdf=directory/'resume.pdf';pdf_fixture(pdf)
        manifest={'company':company,'role':body['role'],'jd_hash':self.d.w.job(key)['jd_hash'],'pdf':str(pdf),'verification':{'pages':1,'pdf_sha256':file_sha(pdf),'text_complete':True,'visual_reviewed':True}}
        self.write(directory/'manifest.json',manifest);self.d.w.bind(key,directory/'manifest.json')
        return key,body,directory
    def plan(self,key):
        return self.d.prepare(key,'email',{'recipient':'recruiter@example.invalid','recipient_source_url':'https://employer.example.invalid/contact','subject':'Synthetic application','body':'Synthetic application for offline tests.','related_roles':[]})['plan_id']
    def test_initialize_does_not_overwrite(self):
        before=(self.root/'preferences.json').read_bytes();initialize(self.root)
        self.assertEqual(before,(self.root/'preferences.json').read_bytes())
    def test_no_authorization_no_external_send(self):
        key,_,_=self.job();plan=self.plan(key);run=self.d.open_run()
        with self.assertRaises(ValueError):self.d.execute(plan,run['run_id'])
        self.assertEqual(FakeSMTP.sends,0)
    def test_real_transport_acceptance_record_and_csv(self):
        key,_,_=self.job();plan=self.plan(key);self.d.authorize(plan,'用户明确授权本次虚构测试计划');run=self.d.open_run()
        result=self.d.execute(plan,run['run_id']);self.assertEqual(result['status'],'smtp_accepted');self.assertEqual(FakeSMTP.sends,1)
        self.assertEqual(records(self.root)[0]['公告薪资'],'9000–11000 CNY/month（公告区间）')
        self.assertTrue((self.root/'applications.csv').exists())
    def test_unknown_after_data_blocks_next_run_without_resend(self):
        key,_,_=self.job();plan=self.plan(key);self.d.authorize(plan,'用户授权该具体虚构计划，测试未知停止');run=self.d.open_run();FakeSMTP.fail_reply=True
        self.assertEqual(self.d.execute(plan,run['run_id'])['status'],'unknown')
        with self.assertRaises(ValueError):self.d.open_run()
        with self.assertRaises(ValueError):self.d.execute(plan,run['run_id'])
        self.assertEqual(FakeSMTP.sends,1)
    def test_copy_failure_preserves_accepted(self):
        key,_,_=self.job();plan=self.plan(key);self.d.authorize(plan,'用户授权当前虚构测试计划');run=self.d.open_run()
        with patch('mail_sent_copy.save_sent_copy',side_effect=OSError('synthetic archive failure')):
            result=self.d.execute(plan,run['run_id'])
        self.assertEqual(result['status'],'smtp_accepted');self.assertEqual(result['sent_copy']['status'],'copy_unconfirmed')
        self.assertEqual(FakeSMTP.sends,1)
    def test_tampered_attachment_prevents_send(self):
        key,_,directory=self.job();plan=self.plan(key);self.d.authorize(plan,'用户授权该虚构测试计划');run=self.d.open_run()
        with (directory/'resume.pdf').open('ab') as out:out.write(b'changed')
        with self.assertRaises(ValueError):self.d.execute(plan,run['run_id'])
        self.assertEqual(FakeSMTP.sends,0)
    def test_review_requires_explicit_exception_and_keeps_unknown(self):
        key,_,_=self.job(salary=False,unknown=True);plan=self.plan(key);self.d.authorize(plan,'用户授权申请但尚未同意待确认项例外');run=self.d.open_run()
        with self.assertRaises(ValueError):self.d.execute(plan,run['run_id'])
        self.d.authorize(plan,'用户知晓薪资和资格未确认，授权本次询问并附简历',allow_review=True)
        self.assertEqual(self.d.execute(plan,run['run_id'])['status'],'smtp_accepted')
        a=json.loads(self.d.w.job(key)['assessment']);self.assertEqual(a['hard_requirements'][0]['status'],'unknown');self.assertEqual(a['decision'],'review')
    def test_unmet_cannot_receive_exception(self):
        key,_,_=self.job(unmet=True);plan=self.plan(key)
        with self.assertRaises(ValueError):self.d.authorize(plan,'用户申请虚构测试例外但明确不符必须拦截',allow_review=True)
    def test_salary_change_invalidates_assessment_and_binding(self):
        key,body,_=self.job();body['salary']['advertised_monthly_min']=5000;self.d.w.ingest([body]);row=self.d.w.job(key)
        self.assertIsNone(row['assessment']);self.assertIsNone(row['binding'])
    def test_company_recipient_duplicate_and_run_quota(self):
        key,_,_=self.job();plan=self.plan(key);self.d.authorize(plan,'用户授权第一个虚构申请');run=self.d.open_run();self.d.execute(plan,run['run_id'])
        other,_,_=self.job(external='second',company='Another Synthetic Company');p2=self.plan(other);self.d.authorize(p2,'用户授权第二个虚构申请')
        with self.assertRaisesRegex(ValueError,'max_per_run'):self.d.execute(p2,run['run_id'])
        duplicate,_,_=self.job(external='third',role='另一示例岗位');p3=self.plan(duplicate);self.d.authorize(p3,'用户授权同公司不同岗位的虚构测试')
        newrun=self.d.open_run()
        with self.assertRaisesRegex(ValueError,'同公司同招聘邮箱'):self.d.execute(p3,newrun['run_id'])
        self.assertEqual(FakeSMTP.sends,1)
    def test_daily_limit_across_different_companies(self):
        for n in range(3):
            key,_,_=self.job(external='job'+str(n),company='Synthetic Company '+str(n));plan=self.plan(key);self.d.authorize(plan,'用户授权当日虚构测试批次');run=self.d.open_run();self.d.execute(plan,run['run_id'])
        with self.assertRaises(ValueError):self.d.open_run()
        self.assertEqual(FakeSMTP.sends,3)
    def test_header_injection_rejected(self):
        key,_,_=self.job()
        with self.assertRaises(ValueError):self.d.prepare(key,'email',{'recipient':'recruiter@example.invalid\r\nBcc: leak@example.invalid','recipient_source_url':'https://employer.example.invalid','subject':'x','body':'x'})
    def test_two_page_resume_rejected(self):
        key,_,directory=self.job();pdf_fixture(directory/'resume.pdf',pages=2);m=read(directory/'manifest.json');m['verification']['pdf_sha256']=file_sha(directory/'resume.pdf');self.write(directory/'manifest.json',m)
        with self.assertRaises(ValueError):self.d.w.bind(key,directory/'manifest.json')
    def test_sent_copy_is_idempotent_even_with_broken_header_search(self):
        key,_,_=self.job();plan=self.plan(key);self.d.authorize(plan,'用户授权虚构申请及本地已发送副本');run=self.d.open_run();self.d.execute(plan,run['run_id'])
        config=read(self.root/'preferences.json');config['submission_channels']['email']['imap']['enabled']=True;self.write(self.root/'preferences.json',config)
        FakeIMAP.messages=[];FakeIMAP.appends=0
        with patch('mail_sent_copy.imaplib.IMAP4_SSL',FakeIMAP):
            first=real_save_sent_copy(self.root,plan);second=real_save_sent_copy(self.root,plan)
        self.assertEqual(first['status'],'saved_verified');self.assertEqual(second['status'],'verified_existing');self.assertEqual(FakeIMAP.appends,1)
    def test_archive_rejects_path_traversal(self):
        with self.assertRaises(ValueError):real_save_sent_copy(self.root,'../../outside')
    def test_unused_evidence_cannot_support_assessment(self):
        key,_,_=self.job();a=json.loads(self.d.w.job(key)['assessment']);self.write(self.root/'evidence.json',{'items':[{'id':'S01','usable':False}]})
        with self.assertRaisesRegex(ValueError,'未确认'):self.d.w.assess(key,a)
    def test_expired_posting_cannot_be_waived(self):
        key,body,directory=self.job(salary=False,unknown=True);a=json.loads(self.d.w.job(key)['assessment'])
        body['deadline']='2000-01-01';self.d.w.ingest([body]);self.d.w.assess(key,a)
        manifest=read(directory/'manifest.json');manifest['jd_hash']=self.d.w.job(key)['jd_hash'];self.write(directory/'manifest.json',manifest);self.d.w.bind(key,directory/'manifest.json')
        plan=self.plan(key)
        with self.assertRaisesRegex(ValueError,'不能例外'):self.d.authorize(plan,'用户授权询问未知项但过期岗位仍应被拦截',allow_review=True)

if __name__=='__main__':unittest.main()
