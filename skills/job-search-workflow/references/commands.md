# 配置与命令

使用Python 3.10+，PDF绑定需要pypdf，可在用户自己的虚拟环境安装仓库requirements.txt。其余本地操作使用标准库。北京时间用于日配额和日期，首版面向中国求职。数据目录放在源码仓库外，`--root`放在子命令前。

```text
python scripts/workflow.py --root <data> init
python scripts/workflow.py --root <data> doctor
python scripts/workflow.py --root <data> import --file <jobs.json>
python scripts/workflow.py --root <data> assess --id <job-key> --file <assessment.json>
python scripts/workflow.py --root <data> list --id <job-key>
python scripts/workflow.py --root <data> bind --id <job-key> --manifest <data/resumes/job/manifest.json>
python scripts/submissions.py --root <data> prepare --job <job-key> --channel email --file <email.json>
python scripts/submissions.py --root <data> show --plan <plan-id>
python scripts/submissions.py --root <data> authorize --plan <plan-id> --note "用户真实授权原话、日期及批次范围"
python scripts/workflow.py --root <data> mode --value official_and_email
python scripts/submissions.py --root <data> open-run
python scripts/submissions.py --root <data> execute --plan <plan-id> --run <run-id>
python scripts/submissions.py --root <data> archive --plan <plan-id>
python scripts/submissions.py --root <data> report --csv <data/applications.csv>
```

init不覆盖配置。preferences.json中填写profile、evidence_index、resume_root、search.allowed_cities、salary_policy，用户确认后设search.setup_confirmed=true。email.enabled和imap.enabled单独配置；只改执行模式不替代计划授权。默认每轮1份、每日3份、每日3轮，可按用户授权调整，不代表已配置定时任务。

evidence.json格式为`{"items":[{"id":"S01","text":"用户确认的真实事实","usable":true,"source":"确认日期或原始文件"}]}`。assets/examples.json均为虚构数据，不可当用户事实。

manifest的company、role与岗位完全一致，jd_hash取自assessment.jd_hash；pdf和可选docx须在resume_root内。verification包含pages=1、text_complete=true、visual_reviewed=true和pdf_sha256。提供docx时还须docx_sha256及native_word_export=true。只在实际检查完成后填写这些字段。

工资配置格式：`{"city_floors":{"示例城市":9000},"default_floor":8000}`，数值由用户决定。用户未设置门槛时salary_policy可为null，报告仍写实际公开薪资或未知。

来源类别为official_ats、employer_site、university_employer_posting、government_posting。保留完整JD、原链接与checked_on，不把聚合站冒充官方。岗位key为platform:external_id。

待确认岗位先披露未知项并取得对应授权，再用authorize --allow-review；不要替换检查函数或临时脚本绕过其余检查。
