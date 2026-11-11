# 近似判重队列与受权候选查询

2026-11-11，CT-06/11/16部分主流程接线。复用已有Router、分析意图、实际媒体指纹和FileService，没有新增数据库迁移。默认`LocalConfig.similarity.enabled=false`；本轮只在隔离合成实例显式启用，未改生产配置或服务。

## 实际链路

启用分析与similarity后，新登记的图片、动画、视频、音频沿同一个分析作业执行`probe → similarity`。未知类型先实际探测，采用有效报告后追加阶段，保留成功probe。指纹阶段与语义/精标并行，不把指纹成功当作十字段完成。已有作业不自动全库重排，可通过已有显式analysis修复入口补缺。

实际CPU handler从Router受权源读取有界原件字节，复验SHA256，生成实际视觉/声学指纹。每次attempt保存原字节快照与严格报告，绑定任务输入摘要、源版本、算法及当前Pillow/FFmpeg/NumPy身份。原件不改名、不修改；成功结果不携带标签、医学批准或重复确认。视频仅稀疏实际PTS帧，不能证明整片重复或动态过程；动画检查完整帧。新输入经实际probe确认原格式GIF且严格整数frame_count=1时按image意图生成完整静态指纹；已有绑定的animation意图与报告继续按原契约校验，不自动改写或重跑，不宣称有运动。未知帧数或多帧GIF仍为animation。音频完整解码所选轨道，语义能力仍待后续。

处理在原租约和CPU资源门槛内执行，逐阶段检查停止状态；每个native命令受剩余执行时间约束。取消期间正在执行的有界命令可到下一安全边界停止，资源只在handler及native命令返回后释放。原件默认128MiB上限，动画/像素/音频时长等沿用[核心预算](similarity-core.md)，超限不采用前缀结果。

多音轨/多视频轨没有明确选择时保留`awaiting_track_selection`，不会擅取第一轨；本批未提供轨道选择页面或接口，后续需补。预算不足记`awaiting_budget`，其他失败维持原Router状态，不自动循环重试。

## 已接HTTP入口

- `GET /similarity/assets/{asset_id}`：返回当前实际指纹状态及算法版本，不返回原始向量或私有路径。
- `POST /similarity/assets/{asset_id}/candidates`：请求体仅为`asset_ids`（1～32个稳定素材ID）和`limit`（1～32）；拒绝客户端向量、描述符或路径。

两个入口沿当前认证身份、对象read权和FileService字节门槛。候选池由调用方明确指定；无权限、文件受限或不存在的候选不暴露身份、分数、原因或隐藏数量。当前有权限但尚未分析/失效的候选保留待办诊断。

仅取每项素材最新active任务的当前成功且已释放attempt。取消/暂停、新待办/失败、源版本/工具变化或报告/实际输入副本被改均不回退旧成功。读取与排序前后复验当前权限、源和生产者；报告哈希与媒体哈希不持有SQLite写事务。每次核验遍历最多256MiB源文件，返回前再次核验相同已选集合，因此最多两遍；报告另有4MiB单份内容上限。源副本实际大小必须等于主库登记大小；检查大小并有界读取，文件膨胀或增长时拒绝，查询与修复均不先调用通用无界哈希。

分数始终是未校准候选，接口不自动合并来源/权限、复制标签或确认重复。独立关系台账已有核心，但本批尚未接HTTP人工关系决定。全库自动召回索引、正式阈值、关系台账备份/云同步和人员页面仍待后续。

## 实际验证

`tests/similarity/test_pipeline.py`验证真实PNG、GIF、MP4、WAV队列执行、实际源/报告校验、未知类型同作业追加且probe不重跑、失败/新待办/取消不回退、受权候选池、HTTP认证/严格请求、读取预算及显式修复保留probe。`tests/similarity/test_config.py`从真实模板建立隔离LocalConfig，实际启动配置执行器并处理新登记素材，默认关闭行为同时验证。

这些是工程与实际合成媒体检查，不是业务近似判重精确率/召回率、医学审核或实际模型质量验收；未下载或调用新模型，未全库重跑或推送。

交叉审查实际复现并修正了单帧GIF模态不一致，以及通用产物校验先无界哈希导致的读取预算绕过。新增实际单帧GIF修复稳定性、报告/源副本膨胀在正文读取前拒绝、修复入口同样不先无界扫描的回归；没有修改旧Router公共校验实现。

父代理最终联合回归：`tests/similarity tests/analysis tests/upload/test_analysis_repair.py tests/topic_index tests/export tests/search/test_ai_drafts.py tests/search/test_draft_search_speed.py` **157项通过（95.15秒）**。此前178项关联回归与本批重叠，不相加；私有证据`jobs/ct-similarity-topic-index-20261111-892e6c41/validation.json`，六份原模板及旧契约/依赖锁摘要保持。
