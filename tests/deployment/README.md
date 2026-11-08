# 本机部署测试

`test_browser.py`验证本机入口的实例/owner绑定、凭证不进入链接、旧配置切换保护及未就绪服务拒绝。实际验证见[本机登录](../../docs/local-browser-login.md)。

配置/凭证隔离、幂等、目录/端口边界、互斥锁、停止代次及子进程启动门闩由pytest验证。`validate_local.py` 使用真实Windows命名Job Object、HTTP上传、模型/CPU队列和杀进程恢复，写入新建私有data/jobs批次，不使用正式词表或人工批准。实际用户登录任务及Codex宿主另记录到部署批次。
