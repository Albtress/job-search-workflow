# Job Search Workflow Skill

一个用于Codex的中文求职工作流Skill：根据真实经历筛选岗位、定制简历、准备并执行获授权的投递，再把结果整理成可记录的表格。默认只查找、评估和准备材料。

## 能力

- 从企业、高校和官方来源核对专业、学历、届别、地点与薪资口径。
- 用确认素材匹配核心职责，区分直接经验、可迁移基础和缺口；按岗位选材，核验简历一页、文字和渲染。
- 本地SQLite账本绑定JD、附件与授权，限制配额、重复和未知结果后的重发。
- Windows DPAPI保存授权码，SMTP_SSL发送单收件人邮件，IMAP另存并核验已发送副本。
- 导出公司、岗位、公告薪资、地点、投递日期、链接、方式及状态的CSV。

## 安装与开始

把`skills/job-search-workflow`复制到自己的Codex skills目录，例如`~/.codex/skills/job-search-workflow`，保留scripts、references与assets。也可让宿主skill-installer按仓库子路径安装。同名已存在时先保留个人版本，不直接覆盖。

在自己的Python 3.10+虚拟环境中安装requirements.txt。入口为[SKILL.md](skills/job-search-workflow/SKILL.md)，配置与命令见[使用说明](skills/job-search-workflow/references/commands.md)。

首次可以对Codex说：

> 使用 $job-search-workflow，先读取我已有的简历和求职条件，核对后找约10个岗位。这次只筛选，不投递。

随后准备某岗简历或在明确批次内投递。真实档案、简历、邮件、凭据和账本放在仓库外；仓库示例均为虚构。

## 当前支持范围

| 能力 | 状态 |
|---|---|
| 队列、评估、哈希绑定、报表 | 首版实现；自动测试不联网 |
| 邮箱执行 | Windows；QQ/Foxmail配置有使用经验，公开测试用假传输层 |
| 已发送副本 | IMAP追加与正文/附件核验；失败不重发申请 |
| 简历排版 | 调用宿主文档/PDF能力或现有工具，不附真实模板 |
| 官网网申 | 实验适配器，逐站观察授权验证，无通用ATS承诺 |
| 收信跟进 | 只读列头/正文，不自动回复HR |
| 定时投递 | 宿主另行配置，本项目不安装后台任务 |

SMTP接受、已发送副本和HR阅读是不同状态。未知结果停止后续动作；待确认项可经具体例外授权，明确不符不能放行。

## 验证

```text
python -m unittest discover -s tests -v
```

测试使用临时目录、虚构岗位与假邮箱，不读取个人数据或发邮件。发布前运行skill-creator的quick_validate.py，并检查待提交源码没有个人信息或运行数据。

MIT License。限制和改进见[流程检查](docs/workflow-review.md)。
