# D12 人员操作与入库验证

`test_local_login.py`验证角色/跨站/远端/代理、过期/重启/并发重放；`validate_local_login.py`与`local-login-browser.mjs`使用真实隔离Chromium验证自动会话、刷新/退出/只读拒写，无长期token传入浏览器。

使用项目`.venv`、合成媒体及真实本地工具，覆盖会话/CSRF、可见范围、幂等入库/共享/撤回、后台任务故障恢复、时间/隐私输出和静态资源打包。
`browser-check.mjs`使用本机Chromium CDP实际操作页面，不引入生产依赖。它仅连接测试进程，测试身份和媒体不表示生产许可或人工医学/隐私签收。

`test_processing.py` 和 `processing-browser.mjs` 验证 CT-20 独立清洗输出复核页：真实
HTTP/分页/当前角色与 ACL 能力，以及本机 Chromium 的图像、音频、真实裁剪视频对照、独立审批、稳定
下载/撤销和会话过期清空。全部使用临时合成目录与测试身份；浏览器不会连接生产实例。
