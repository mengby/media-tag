# D09 上传

`service.py` 实现持久化偏移续传、完成租约、真实格式/解码检查、事务登记及分析 outbox。
分块与合并输入只存私有 jobs；不把客户端文件名用作路径。见 [D09](../../../docs/upload-variants.md)。
