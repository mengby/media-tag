# Windows本机启动

`local-service.ps1` 为已经 `local-init` 的实例安装/启动/停止/查询/移除当前用户登录任务。限定RunLevel Limited、Interactive登录、自启动命令为项目`.venv`；没有SYSTEM身份、外网监听、密码参数或数据清理操作。安装前后应核对任务实际动作与私有实例信息；运行与凭证不提交Git。
