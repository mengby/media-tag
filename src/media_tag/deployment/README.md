# 本机部署

`local.py` 初始化私有空库配置与凭证，提供本机API/Worker监管、停止和状态。Windows命名Job Object约束所有子进程，恢复前真实清空该实例的进程树，按所属Worker确认旧租约停止；不释放其他执行器的资源。登录自启动由仓库PowerShell安装器配置，运行身份为当前用户，数据/日志/凭证在忽略目录。详见 [部署说明](../../../docs/windows-deployment.md)。
