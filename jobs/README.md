# 私有任务产物

`jobs/<job_id>/`记录输入副本、阶段版本/配置/摘要、实际输出、错误与耗时。除本说明外全部忽略Git，不作为静态文件或已打码副本入口公开。

D02保存`inputs/`及`runs/<stage-fingerprint>/attempt-<n>/`；成功阶段按指纹和输出哈希复用，失败或损坏阶段使用新attempt重跑。D03执行适配保存`inputs/`及`runs/<task_id>/attempt-<n>/`中的task/config/报告/证据；租约、取消、错误、耗时和唯一状态源由SQLite Router表维护。目录记录不构成第二套队列。D09上传使用`jobs/upload-<uuid>/parts/`及`input-<epoch>.bin`，SQLite维护已提交偏移/完成租约/outbox；不自动删除失败或取消的私有证据。
