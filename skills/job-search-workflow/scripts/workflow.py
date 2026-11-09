"""Local job evidence, matching and immutable resume bindings. Python 3.10+."""
import argparse,hashlib,json,os,re,sqlite3,sys,uuid
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.parse import urlsplit
TZ=timezone(timedelta(hours=8))
DEFAULT_ROOT=Path(os.environ.get('JOB_SEARCH_DATA',str(Path.home()/'.local/share/job-search-workflow')))
TEMPLATE=Path(__file__).resolve().parents[1]/'assets/preferences.json'


def now():
    return datetime.now(TZ).isoformat(timespec='seconds')

def day():
    return datetime.now(TZ).date().isoformat()

def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def sha(value):
    return hashlib.sha256(value).hexdigest()

def file_sha(path):
    return sha(Path(path).read_bytes())

def norm(value):
    return re.sub(r'\s+', '', str(value)).casefold()

def city_name(value):
    value = str(value).strip()
    for prefix in ('江苏省', '浙江省', '安徽省', '广东省'):
        value = value.removeprefix(prefix)
    return value.removesuffix('市')

def need(test, message):
    if not test:
        raise ValueError(message)

def nonempty(obj, keys):
    for key in keys:
        need(isinstance(obj.get(key), str) and obj[key].strip(), '缺少非空字段：' + key)

def owned(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    need(path.is_relative_to(root) and path != root, '文件不在预期目录内：' + str(path))
    return path

SCHEMA='\nCREATE TABLE IF NOT EXISTS jobs (\n key TEXT PRIMARY KEY, identity TEXT NOT NULL, body TEXT NOT NULL,\n jd_hash TEXT NOT NULL, first_seen TEXT NOT NULL, updated TEXT NOT NULL,\n assessment TEXT, binding TEXT);\nCREATE INDEX IF NOT EXISTS job_identity ON jobs(identity);\nCREATE TABLE IF NOT EXISTS runs (\n id TEXT PRIMARY KEY, day TEXT NOT NULL, policy_hash TEXT NOT NULL,\n halted INTEGER NOT NULL DEFAULT 0);\nCREATE TABLE IF NOT EXISTS attempts (\n id INTEGER PRIMARY KEY, run TEXT NOT NULL, job TEXT NOT NULL,\n identity TEXT NOT NULL, action TEXT NOT NULL, day TEXT NOT NULL,\n state TEXT NOT NULL, context TEXT NOT NULL, evidence TEXT, created TEXT NOT NULL,\n FOREIGN KEY(run) REFERENCES runs(id), FOREIGN KEY(job) REFERENCES jobs(key));\nCREATE INDEX IF NOT EXISTS attempt_identity ON attempts(identity, action, state);\n'

def initialize(root):
    root.mkdir(parents=True, exist_ok=True)
    prefs = root / 'preferences.json'
    if not prefs.exists():
        with prefs.open('x', encoding='utf-8') as f:
            config=read(TEMPLATE)
            config['resume_root']=str((root/'resumes').resolve())
            config['evidence_index']=str((root/'evidence.json').resolve())
            f.write(json.dumps(config,ensure_ascii=False,indent=2))
    if not (root/'evidence.json').exists():
        (root/'evidence.json').write_text('{"items": []}',encoding='utf-8')
    (root/'resumes').mkdir(exist_ok=True)
    db = sqlite3.connect(root / 'workflow.sqlite3')
    try:
        db.executescript(SCHEMA)
    finally:
        db.close()
    return {'root': str(root.resolve()), 'preferences': str(prefs.resolve()), 'network_actions': 0}

class Workflow:

    def __init__(self, root):
        self.root = root
        self.prefs = read(root / 'preferences.json')
        self.db = sqlite3.connect(root / 'workflow.sqlite3', timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')

    def job(self, key):
        row = self.db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
        need(row is not None, '未找到岗位：' + key)
        return dict(row)

    def ingest(self, entries):
        need(isinstance(entries, list), '导入文件必须是JSON数组')
        result = []
        with self.db:
            for entry in entries:
                nonempty(entry, ('platform', 'company', 'role', 'city', 'source_url', 'jd'))
                url = urlsplit(entry['source_url'])
                need(url.scheme in ('https', 'http') and url.hostname, '来源必须为完整网页URL')
                identity = '|'.join(norm(entry[k]) for k in ('company', 'role', 'city'))
                need(entry['platform'] in ('official_ats','employer_site','university_employer_posting','government_posting'), '来源须为企业、高校或官方岗位')
                entry['external_id'] = entry.get('external_id') or sha((entry['source_url']+'|'+identity).encode())[:20]
                key = entry['platform'] + ':' + entry['external_id']
                # The entire target snapshot is bound, not just the JD text.
                snapshot = {k: entry.get(k) for k in ('company', 'role', 'city', 'jd', 'source_url', 'role_family','salary','location_note','deadline','employment_type')}
                jd_hash = sha(dumps(snapshot).encode())
                old = self.db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
                if old:
                    changed = old['jd_hash'] != jd_hash
                    self.db.execute('UPDATE jobs SET identity=?,body=?,jd_hash=?,updated=?,assessment=?,binding=? WHERE key=?',
                                    (identity, dumps(entry), jd_hash, now(), None if changed else old['assessment'],
                                     None if changed else old['binding'], key))
                else:
                    self.db.execute('INSERT INTO jobs(key,identity,body,jd_hash,first_seen,updated) VALUES(?,?,?,?,?,?)',
                                    (key, identity, dumps(entry), jd_hash, now(), now()))
                duplicate = self.db.execute('SELECT key FROM jobs WHERE identity=? AND key<>?', (identity, key)).fetchall()
                result.append({'id': key, 'new': not bool(old), 'same_role_records': [r[0] for r in duplicate]})
        return result

    def assess(self, key, assessment):
        row = self.job(key)
        assessment = json.loads(dumps(assessment))
        nonempty(assessment, ('fit_reason', 'greeting'))
        need(assessment.get('decision') in ('recommend', 'review', 'skip'), 'decision须为recommend/review/skip')
        requirements = assessment.get('hard_requirements')
        need(isinstance(requirements, list) and requirements, '必须列明实际检查的硬条件')
        for requirement in requirements:
            nonempty(requirement, ('requirement', 'evidence'))
            need(requirement.get('status') in ('met', 'unknown', 'unmet'), '硬条件status无效')
        need(isinstance(assessment.get('source_ids'), list) and assessment['source_ids'], '缺少本人证据素材ID')
        index_path = Path(self.prefs['evidence_index'])
        index = read(index_path)
        valid = {item['id'] for item in index['items'] if item.get('usable',False)}
        need(all(s in valid for s in assessment['source_ids']), '存在未知或未确认的素材ID')
        statuses = {r['status'] for r in requirements}
        if 'unmet' in statuses:
            assessment['decision'] = 'skip'
        elif 'unknown' in statuses and assessment['decision'] == 'recommend':
            assessment['decision'] = 'review'
        body = json.loads(row['body'])
        allowed = [city_name(x) for x in self.prefs['search'].get('allowed_cities', [])]
        if allowed:
            city = city_name(body['city'])
            ambiguous_city = city in ('全国', '多地', '长三角', '珠三角') or any(x in city for x in ('/', '、', ','))
            assessment['scope_screen'] = {'city': city, 'status': 'inside' if city in allowed else 'unknown' if ambiguous_city else 'outside'}
            if assessment['scope_screen']['status'] == 'outside':
                assessment['decision'] = 'skip'
            elif assessment['scope_screen']['status'] == 'unknown' and assessment['decision'] == 'recommend':
                assessment['decision'] = 'review'
        assessment['salary_screen'] = self.salary_screen(body)
        if assessment['salary_screen']['status'] == 'below_floor':
            assessment['decision'] = 'skip'
        if assessment['decision'] == 'recommend':
            pay = assessment['salary_screen']['status']
            if pay == 'below_floor':
                assessment['decision'] = 'skip'
            elif pay in ('unknown', 'range_crosses_floor', 'internship_separate'):
                assessment['decision'] = 'review'
        career = assessment.get('career_assessment')
        if self.prefs['search'].get('require_career_assessment') and assessment['decision'] != 'skip':
            if not isinstance(career, dict):
                assessment['decision'] = 'review'
                assessment['career_review_note'] = '须补充核心职责、本人证据与成功机会的相对判断'
            else:
                nonempty(career, ('relation', 'chance_tier', 'confidence', 'reason'))
                need(career['relation'] in ('direct', 'adjacent', 'unrelated'), 'career relation无效')
                need(career['chance_tier'] in ('main', 'try', 'low'), 'chance_tier无效')
                need(career['confidence'] in ('high', 'medium', 'low'), 'career confidence无效')
                matches = career.get('task_matches', [])
                need(isinstance(matches, list), 'task_matches须为数组')
                for match in matches:
                    nonempty(match, ('jd_task', 'evidence'))
                    need(isinstance(match.get('source_ids'), list) and match['source_ids'] and all(x in valid for x in match['source_ids']), '任务对应的素材ID无效')
                if career['relation'] == 'unrelated' or career['chance_tier'] == 'low':
                    assessment['decision'] = 'skip'
                elif len({norm(m['jd_task']) for m in matches}) < 2 or career['confidence'] == 'low':
                    assessment['decision'] = 'review'
        assessment.update(jd_hash=row['jd_hash'], assessed_at=now(),
                          profile_hash=sha(dumps(self.prefs['profile']).encode()),
                          source_index_sha256=file_sha(index_path))
        with self.db:
            self.db.execute('UPDATE jobs SET assessment=? WHERE key=?', (dumps(assessment), key))
        return assessment

    def salary_screen(self, body):
        policy = self.prefs['search'].get('salary_policy')
        if not policy:
            return {'status': 'not_configured', 'floor': None}
        city = city_name(body['city'])
        floor = policy['city_floors'].get(city, policy['default_floor'])
        pay = body.get('salary') or {}
        result = {'city': city, 'floor': floor, 'currency': 'CNY', 'period': 'month',
                  'status': 'unknown', 'note': '未公布可比月薪，不能保证达到城市门槛'}
        if body.get('employment_type') == 'internship' or pay.get('period') == 'day':
            result.update(status='internship_separate', note='实习日薪/补贴单列，不与正式岗位月薪混算')
            return result
        if pay.get('period') not in (None, 'month') or pay.get('currency') not in (None, 'CNY'):
            result['note'] = '计薪周期或币种不可直接比较；不把年薪除12当固定月薪'
            return result
        fixed = pay.get('fixed_monthly')
        low = fixed if fixed is not None else pay.get('advertised_monthly_min', pay.get('advertised_monthly'))
        high = fixed if fixed is not None else pay.get('advertised_monthly_max', pay.get('advertised_monthly'))
        for number in (low, high):
            need(number is None or (type(number) in (int, float) and number >= 0), '薪资必须为非负数值或null')
        need(low is None or high is None or low <= high, '薪资区间上下限倒置')
        result.update(observed_min=low, observed_max=high)
        if high is not None and high < floor:
            result.update(status='below_floor', note='公开可比月薪上限低于城市门槛')
        elif low is not None and low >= floor:
            result.update(status='fixed_meets_floor' if fixed is not None else 'posted_meets_floor',
                          note='固定月薪证据达到门槛' if fixed is not None else '公告月薪下限达到门槛；实际构成和录用工资仍需核实')
        elif low is not None and high is not None and low < floor <= high:
            result.update(status='range_crosses_floor', note='区间覆盖门槛，但不能确认会按达到门槛的金额录用')
        return result

    def bind(self, key, manifest_path):
        from pypdf import PdfReader
        row=self.job(key);body=json.loads(row['body'])
        base=Path(self.prefs['resume_root']).resolve()
        manifest_path=owned(manifest_path,base);manifest=read(manifest_path)
        need(manifest.get('company')==body['company'] and manifest.get('role')==body['role'],'简历公司或岗位不符')
        need(manifest.get('jd_hash')==row['jd_hash'],'简历不是当前JD版本')
        report=manifest.get('verification',{})
        need(report.get('visual_reviewed') is True and report.get('text_complete') is True,'须先完成文字核验和实际目检')
        pdf=owned(manifest['pdf'],base)
        need(pdf.suffix.lower()=='.pdf' and pdf.is_file(),'需要本地PDF成品')
        need(file_sha(pdf)==report.get('pdf_sha256'),'PDF与检查记录不同')
        reader=PdfReader(pdf)
        need(len(reader.pages)==1 and report.get('pages')==1,'岗位简历必须一页')
        need(len((reader.pages[0].extract_text() or '').strip())>=30,'PDF须包含可复制的正文')
        binding={'work_dir':str(manifest_path.parent),'jd_hash':row['jd_hash'],'bound_at':now(),
                 'pdf':str(pdf),'files':{str(pdf):file_sha(pdf),str(manifest_path):file_sha(manifest_path)}}
        if manifest.get('docx'):
            docx=owned(manifest['docx'],base)
            need(docx.is_file() and docx.suffix.lower()=='.docx','DOCX成品不存在')
            need(report.get('native_word_export') is True,'提供Word成品时须确认原生Word导出')
            need(file_sha(docx)==report.get('docx_sha256'),'Word与检查记录不同')
            binding['files'][str(docx)]=file_sha(docx)
        with self.db:self.db.execute('UPDATE jobs SET binding=? WHERE key=?',(dumps(binding),key))
        return binding

    def list(self,key=None):
        rows=[self.job(key)] if key else self.db.execute('SELECT * FROM jobs ORDER BY first_seen,key').fetchall()
        return [{'id':r['key'],'job':json.loads(r['body']),
                 'assessment':json.loads(r['assessment']) if r['assessment'] else None,
                 'binding':json.loads(r['binding']) if r['binding'] else None} for r in rows]

    def doctor(self):
        return {'root':str(self.root.resolve()),'mode':self.prefs['execution']['mode'],
                'setup_confirmed':self.prefs['search'].get('setup_confirmed',False),
                'evidence_exists':Path(self.prefs['evidence_index']).is_file(),
                'resume_root_exists':Path(self.prefs['resume_root']).is_dir(),
                'windows_mail_supported':os.name=='nt','network_actions':0}

    def set_mode(self,value):
        need(value in ('discovery_only','official_and_email'),'模式无效')
        self.prefs['execution']['mode']=value
        path=self.root/'preferences.json';temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.prefs,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(path)
        return {'mode':value,'note':'模式不替代对具体计划的用户授权','external_actions':0}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    sub=p.add_subparsers(dest='command',required=True)
    for name in ('init','doctor'):sub.add_parser(name)
    q=sub.add_parser('import');q.add_argument('--file',required=True)
    q=sub.add_parser('list');q.add_argument('--id')
    q=sub.add_parser('assess');q.add_argument('--id',required=True);q.add_argument('--file',required=True)
    q=sub.add_parser('bind');q.add_argument('--id',required=True);q.add_argument('--manifest',required=True)
    q=sub.add_parser('mode');q.add_argument('--value',choices=('discovery_only','official_and_email'),required=True)
    a=p.parse_args();w=None
    try:
        if a.command=='init':out=initialize(a.root)
        else:
            need((a.root/'preferences.json').is_file(),'先运行init；不会覆盖已有个人配置')
            w=Workflow(a.root)
            if a.command=='import':out=w.ingest(read(a.file))
            elif a.command=='assess':out=w.assess(a.id,read(a.file))
            elif a.command=='bind':out=w.bind(a.id,a.manifest)
            elif a.command=='list':out=w.list(a.id)
            elif a.command=='mode':out=w.set_mode(a.value)
            else:out=w.doctor()
        print(json.dumps(out,ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError,KeyError,sqlite3.Error,ImportError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr);return 1
    finally:
        if w:w.db.close()

if __name__=='__main__':raise SystemExit(main())
