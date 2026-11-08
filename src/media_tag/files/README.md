# D17 文件基础接口

`service.py` 统一对象权限/隐私链检查、固定哈希、后端验证、预览资源和清单；
`http.py` 实现条件请求、单区间 Range、流式关闭和安全文件名。不接受任意路径/URL。
实现边界见 [D17 文件说明](../../../docs/file-access.md)。

D17公共检索、能力发现及受限播放区间见 [API说明](../../../docs/asset-search-api.md)；models.py 校验实际所选文件的播放范围。
