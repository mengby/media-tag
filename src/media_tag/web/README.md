# D12 人员操作页

`local_login.py`提供可选本机私有文件交接与一次性会话兑换，身份仍映射已有凭证；匿名API请求不获授权。入口与验证见[本机登录](../../../docs/local-browser-login.md)。

`http.py`安装同源会话及人员接口；`service.py`负责可见素材分组、主动入库、后台分析outbox及失败阶段重试；`schema.py`是迁移5。
`index.html`、`app.js`、`style.css`随wheel分发，不使用CDN或前端构建依赖。没有公共媒体静态目录。
处理临时素材和入库分离，共享必须明确选择已配置人员；隐私输出通过D17来源链及实际哈希复核门槛。
