# media_tag

公共实现包。`contracts/`维护公共结构，`catalog/`与`storage/`实现D01核心，`workers/`含D02技术探测/帧证据，`cli.py`路由本地命令。`router/`与`api/`实现D03持久化调度和受认证控制API，支持显式serve/work-once命令；`models/`实现D04真实模型、子进程与校准工具；常驻部署仍待D07。`upload/`与`variants/`实现D09续传/校验/事务outbox及版本契约；`transforms/`与`workers/face_track.py`实现D10完整视频扫描/修订/渲染/复核接口；D11固定变速/组合及隐私来源链检查也已实现；`web/`实现D12人员页、同源会话、主动入库/共享及指定输出复核；`review/`与`export/`实现D06字段证据/人工决定、缺口及CSV原子快照；D15集成仍待后续。`files/`实现D17只读详情、清单、预览/下载、Range与隐私链检查；未实现语义检索/MCP。正式表头、运营数据、数据库与凭证不放在包中。
