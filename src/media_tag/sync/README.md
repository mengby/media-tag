# 持久同步桥接

云端事件先写入独立SQLite待办再推进游标。通过每人显式本地editor凭证调用已有上传API，主库只读用于核对/证据及发布；不构造或接管生产Router。相同字节保持来源/owner独立，重复事件复用本地上传ID。当前配置/命令与范围见docs/cloud-intake-sync.md。
