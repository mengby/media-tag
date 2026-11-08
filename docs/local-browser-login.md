# 本机自动登录

2026-11-08，已在当前电脑启用。双击桌面“素材处理”快捷方式，自动打开素材库并建立当前用户会话，不需要复制或输入token。页面、API、Codex只读身份和医学/输出复核权限继续沿用已有配置。

## 使用

桌面入口调用项目`.venv/Scripts/pythonw.exe -m media_tag local-open --root D:/ai/media-tag/data/local`，辅助进程不显示终端。服务已停止时启动原有登录任务，等待就绪后打开默认浏览器；失败显示简短提示。等价命令：

```powershell
& .\.venv\Scripts\python.exe -m media_tag local-open --root D:\ai\media-tag\data\local
& .\scripts\windows\local-service.ps1 -Action Open -Root D:\ai\media-tag\data\local
```

已登录后可刷新或使用原`http://127.0.0.1:8765/ui/`地址。会话最长8小时，退出、过期、服务重启或更换浏览器后，从桌面入口重新打开。没有会话的普通地址只提示使用本机入口，不给匿名请求分配编辑权限；本机模式隐藏手工token表单。其他未启用实例仍保留原登录方式。

新环境或首次从旧版本启用时，先正常停止服务，再Install，最后Start；旧进程可能不认识新增配置，不能先写配置再停旧服务。`local-enable-browser`增加了运行中旧配置拒绝保护。Install启用私有配置并创建当前用户桌面快捷方式，不覆盖其他应用同名入口。

```powershell
& .\scripts\windows\local-service.ps1 -Action Stop -Root D:\ai\media-tag\data\local
& .\scripts\windows\local-service.ps1 -Action Install -Root D:\ai\media-tag\data\local
& .\scripts\windows\local-service.ps1 -Action Start -Root D:\ai\media-tag\data\local
```

## 身份与权限

`local_browser_login`默认关闭，仅为显式实例配置loopback origin与私有browser-login目录。本机入口核对实例Windows SID、owner-token对应的已有身份及editor/admin角色，使用原凭证摘要绑定会话；不生成默认管理员、医学复核人或额外对象授权。

入口把一次性随机凭据的SHA256、原凭证摘要及60秒时限写入`data/local/browser-login/`，目录继承EDY/SYSTEM ACL。长期token不进入浏览器、链接、命令参数或新配置。浏览器仅在URL fragment接收短期随机凭据，页面立即清除fragment，再以同源POST兑换HttpOnly/SameSite=Strict会话。fragment不随页面GET发送，见[MDN说明](https://developer.mozilla.org/en-US/docs/Web/URI/Reference/Fragment)。短期凭据不写localStorage；偏好存储仍只保存画廊/列表模式。

兑换检查实际loopback客户端、固定Host/端口、精确Origin及Fetch Metadata，拒绝跨站、代理头、伪造、过期、重放及重启前凭据。消费使用跨进程文件锁，只能成功一次；实际身份从当前服务配置查找。reader仍是reader，worker不能登录人员页；API继续要求原Bearer或有效会话，逐对象权限、私有输出门槛和审核角色检查保留。

日常不复制登录链接，不将其保存到书签或聊天。过期后重新打开桌面入口。权限和隐私输出复核不会因本机自动登录而跳过。

## 实际验证

全量663项、专项最终36项、普通核心wheel36项、原JS逻辑4项及92份Markdown/133模块资源一致性通过；隔离Chromium批次`jobs/local-login-844d2c27/validation.json`与已部署实例批次`jobs/installed-local-login-1ca25d36/validation.json`验证无需手输token、fragment清除、HttpOnly会话、刷新、退出、重放拒绝及reader上传403/医学角色未指定。截图在对应私有批次，均为空库，无真实素材或医学/隐私批准。

首次切换时，旧进程因配置模型不认识新增字段在停止收尾返回2；当时任务/上传均为0，数据未变化。新服务已复验正常停止退出码0和计划任务重启，新增切换保护防止重现。全量、普通wheel及提交结果见[进度](PROGRESS.md)。本轮不修改相邻剪辑项目、原件、词表或模板，不新增依赖。
