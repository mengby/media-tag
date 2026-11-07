# D03 持久化调度

SQLite是任务/尝试/租约/资源与结果采用的唯一状态源。`models.py`是DAG与租约接口，`service.py`实现事务调度，`schema.py`提供版本2迁移，`runtime.py`是受信本地执行适配。

Worker只能领取已注册阶段，写私有attempt并回传结果；共享索引/词表发布不由Worker执行。失联资源保守隔离，须收到可信释放确认或进程停止探针证明后才复用。具体实现与未验证的硬件边界见 [D03说明](../../../docs/router-queue.md)。
