# 双仓拆分首轮：可移植发布与独立查询

2026-11-10。用户已授权按双仓方案实施，control-center任务M-006。media-tag继续拥有本地主目录/打标/人工值；media-cloud独立维护共享契约与发布接收、查询、预览下载。原本地服务和处理入口保留。本轮不生产部署、不改auto-video来源。

## 已实现

- 独立media-contracts 0.2.0 wheel；本仓publish可选依赖精确引用，不从兄弟仓源码import。
- 显式路径和素材ID选择；SQLite只读连接及backup API构造一致快照，不调用本地应用启动/Router构造/队列恢复/模型推理。
- 复用现有源权限、衍生隐私门槛、文件/预览/模型报告验证；导出文件、人工有效字段、AI初稿与证据闭包。AI字段保持初稿，医学与许可不自动批准。
- 原Worker报告在本地验hash，输出可移植证据投影保留原报告摘要、实际源hash和帧引用；含本机路径的原报告不发送。失效报告/帧拒绝生成完整manifest。
- HTTPS/loopback受限发布客户端：先本地校验全部对象，再准备/增量传输/云验证/CAS采用；重复上传复用对象。令牌从显式私有文件读取，无重定向和环境代理。

## 安装和命令

下面使用Windows仓库目录示例；wheel文件是版本制品。新环境需先按本仓原有锁文件安装基础依赖。

```powershell
& .\.venv\Scripts\python.exe -m pip install --no-index --no-deps D:/ai/media-cloud/dist/media_contracts-0.2.0-py3-none-any.whl
& .\.venv\Scripts\python.exe -m media_tag publish build --input D:/private/export.json --destination D:/private/releases/release-001
& .\.venv\Scripts\python.exe -m media_tag publish push --bundle D:/private/releases/release-001 --config D:/private/publisher.json
```

export.json为显式私有配置，路径示例需换成真实授权位置：

```json
{
  "database": "D:/private/catalog.db",
  "object_store": "D:/private/objects",
  "workspace": "D:/private/workspace",
  "actor_id": "existing-local-actor",
  "role": "editor",
  "asset_ids": ["selected-existing-asset-id"],
  "release_id": "release-001",
  "parent_release_id": null,
  "producer_revision": "0000000000000000000000000000000000000000",
  "template_dir": "D:/ai/media-tag/asset-index-template",
  "review": {}
}
```

producer_revision填写实际完整Git提交。有人工确认记录时必须提供本地原模板及原ReviewDeployment配置（含已绑定词表版本/摘要），否则拒绝导出，不能把confirmed静默改成needs_review。模板不上传。导出仅支持当前本地local文件后端；其它后端不能直接套用。

publisher.json包含base_url（HTTPS服务origin，本机可用loopback HTTP）、token_file（发布专用令牌绝对路径）和expected_active_release（预期当前版本，首次null）。云端实际采用后才写cloud-receipt.json。不使用人员reader token或本地全局管理员凭证。

## 当前边界与验证

云服务使用独立文件对象后端、cloud-state.db和只读每版catalog.db。仅实现字面/已发布字段查询；模型查询编码、完整D17搜索响应兼容、四工具客户端迁移、企业SSO、多人收件和在线处理仍未实施。真实OSS/ECS及Linux目标运行验收另列待办。

实际测试与版本证据见D:/ai/control-center/tasks/M-006；旧生产目录和8765服务不重启，合成验证写独立work/M-006。后续按原双仓方案继续，不将本轮计作打码/变速云端完成。
