# 投递通道

## 邮箱

配置preferences.json的sender、SMTP_SSL主机/端口、IMAP主机/端口和sent_mailbox。QQ默认目录为`Sent Messages`，其他服务须核实ASCII IMAP目录名；不猜中文目录编码。

Windows用户运行`python scripts/submissions.py --root <目录> setup-mail`，在本机终端输入授权码，当前QQ/Foxmail入口要求16位授权码。凭据只以当前用户DPAPI加密保存在private/smtp-auth.dpapi。公开测试用假传输层，不读取真实凭据。

prepare只写本地EML。authorize记录真实授权；执行命令本身不证明用户已授权。allow-review仅用于已披露未知项，绑定评估哈希；skip、过期、范围外、文件变更、重复和超额度不能放行。

execute先事务落pending，再发送一次。DATA后断开或结果未知必须停止；Message-ID没有服务端幂等保证。SMTP接受不等于HR收件或阅读。

接受落盘后再追加已发送副本。副本/报表失败不改变SMTP接受结果；补副本用`archive --plan <ID>`。逐批读取邮件头查重，APPEND后读回核对正文与PDF哈希。结果独立保存在outbox/<计划>/已发送副本.json，不增加投递额度。

mail_reader.py提供check/list/read，只读与BODY.PEEK保留未读状态，不加载远程资源或执行附件。最新列头最多20封，指定UID读正文，不声称自动退信/邀约分类。

## 官网实验能力

web_executor.cjs需要用户自己的Node与Playwright SDK路径，不自动安装运行时。适配器来自当前页面观察，包含申请URL、允许origin、公司/岗位识别、字段与真实素材ID、文件框、提交按钮、提交后成功标记。

web.enabled默认false，逐站验证后才能启用；首版没有通用真实ATS适配承诺。登录、验证码、协议交用户；填写和上传也可能触发外发，需要同等授权。只提交一次，明确成功提示才web_confirmed；unknown或filled_not_submitted保留待处理，不换通道补投。
