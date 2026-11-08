# media_tag

公共实现包。`contracts/`维护结构，`catalog/`与`storage/`实现D01核心，`workers/`含D02探测/帧证据；`router/`与`api/`实现D03队列和鉴权接口，`models/`实现D04专用模型；`upload/`与`variants/`实现D09续传/版本契约，`transforms/`实现D10/D11打码、变速和输出复核；`web/`实现D12人员页/入库，`review/`与`export/`实现D06复核/CSV并接入页面。

`terminology/`实现D13发布、D14固定BGE/解析与D16评测工具；`search/`实现D15受权检索/反馈及D17公共分页契约；`files/`实现D17固定版本预览/下载/Range/隐私链检查，`mcp/`与`client/`实现D18只读桥接和D19任务内下载/用途交接。`maintenance/`实现D07离线备份/校验/隔离还原，`cli.py`提供显式命令，导入不自动启动服务。真实业务/部署/AI宿主仍待验收；正式表头、运营数据、数据库与凭证不放在包中。
