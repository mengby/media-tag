# 本机部署测试

`test_browser.py`验证本机入口的实例/owner绑定、凭证不进入链接、旧配置切换保护及未就绪服务拒绝。实际验证见[本机登录](../../docs/local-browser-login.md)。

配置/凭证隔离、幂等、目录/端口边界、互斥锁、停止代次及子进程启动门闩由pytest验证。`validate_local.py` 使用真实Windows命名Job Object、HTTP上传、模型/CPU队列和杀进程恢复，写入新建私有data/jobs批次，不使用正式词表或人工批准。实际用户登录任务及Codex宿主另记录到部署批次。

`validate_capacity.py --seconds 900` 执行真实 15 分钟本机监测、5 会话 Chromium 原文查询、独立私有实例上传/模型队列/变速及资源/哈希检查，见[容量与负载说明](../../docs/capacity-soak.md)。它不是普通单元测试，不自动重启或停止现有实例、不操作剪辑项目。浏览器使用已安装的 Chrome 与 Codex runtime Playwright，长期凭证不进入页面。`test_capacity.py` 验证失败/超时分母、冷热分组、链接边界及容量口径；`test_capacity_harness.py` 防止非成功终态误等至超时。
