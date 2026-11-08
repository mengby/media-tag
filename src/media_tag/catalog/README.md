# 索引核心

- `database.py`：本地SQLite迁移、外键、WAL和显式事务；数据库不走SMB。
- `models.py`：调用身份、来源输入、旧ID和已授权素材快照。
- `service.py`：私有登记、对象级授权、来源/别名、人工文本修订、AI建议隔离、一致快照与按哈希读取。

公共入口是`Catalog`；`Database`和存储适配器是仅供受信服务使用的底层能力，不能直接暴露为HTTP/MCP工具。D01不实现登录、医学词表发布、人脸复核、衍生配方或表格导出。详见 [D01说明](../../../docs/catalog-storage.md)。

当前公共迁移7增加D13词表状态，不改变迁移1～6；发布能力见 [D13说明](../../../docs/terminology-release.md)。

公共迁移8增加D14不可变文本索引与serving整组指针，不改迁移1～7。
