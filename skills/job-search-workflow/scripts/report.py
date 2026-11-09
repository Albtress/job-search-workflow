"""Export application snapshots; no network and no credential access."""
import csv,json,sqlite3
from contextlib import closing
from pathlib import Path

FIELDS=('公司名称','岗位名称','公告薪资','工作地点','投递日期','岗位链接','投递方式','收件邮箱','状态','已发送副本','备注')

def salary_text(pay):
    if pay.get('fixed_monthly') is not None:return str(pay['fixed_monthly'])+'元/月（固定）'
    low=pay.get('advertised_monthly_min',pay.get('advertised_monthly'))
    high=pay.get('advertised_monthly_max',pay.get('advertised_monthly'))
    if low is None and high is None:return '面议/未公开，待确认'
    unit=str(pay.get('currency','CNY'))+'/'+str(pay.get('period','month'))
    return (str(low) if low==high else str(low if low is not None else '?')+'–'+str(high if high is not None else '?'))+' '+unit+'（公告区间）'

def records(root):
    root=Path(root)
    with closing(sqlite3.connect(root/'workflow.sqlite3')) as db:
        db.row_factory=sqlite3.Row
        rows=db.execute('SELECT a.created,a.state,a.plan,p.payload FROM delivery_attempts a JOIN delivery_plans p ON p.id=a.plan ORDER BY a.id').fetchall()
    result=[]
    for row in rows:
        p=json.loads(row['payload']);copy_file=root/'outbox'/row['plan']/'已发送副本.json'
        copy=json.loads(copy_file.read_text(encoding='utf-8')) if copy_file.exists() else {}
        state={'smtp_accepted':'SMTP已接受，HR阅读未确认','web_confirmed':'官网已确认提交','pending':'处理中，禁止重试','unknown':'结果未知，禁止重试','not_sent':'未发送成功','blocked':'待人工处理','filled_not_submitted':'已填写，尚未提交'}.get(row['state'],row['state'])
        result.append(dict(zip(FIELDS,(p['company'],p['role'],salary_text(p.get('salary',{})),p.get('location_note') or p['city'],row['created'][:10],p['source_url'],p['channel'],p['specification'].get('recipient',''),state,copy.get('status','未确认'),'相关岗位仅表达意向：'+'、'.join(p.get('related_roles',[])) if p.get('related_roles') else ''))))
    return result

def export_csv(root,target):
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_suffix(target.suffix+'.tmp')
    with temporary.open('w',encoding='utf-8-sig',newline='') as out:
        writer=csv.DictWriter(out,fieldnames=FIELDS);writer.writeheader()
        for row in records(root):
            # Company/JD fields are external data, not spreadsheet formulas.
            writer.writerow({k:("'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v) for k,v in row.items()})
    temporary.replace(target);return target
