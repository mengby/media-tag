# 术语公共实现

`contracts/` 定义分类、概念、别名、来源及发布请求；`lexicon/` 负责候选、审核、不可变发布、差异及回滚。运营数据保存在 SQLite 和忽略 Git 的 `jobs/terminology-*/`。D14 匹配/向量已实现；D15 素材检索页面另行实现。见 [D13说明](../../../docs/terminology-release.md)。

D14 `resolve/` 与 `embedding/` 已实现，`workers/terminology.py` 接入D03；D15素材检索/页面仍后续。见 [D14说明](../../../docs/terminology-worker.md)。
