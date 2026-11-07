# 公共契约

- `models.py`：建议、有效值、证据、任务与结果的运行时结构校验。
- `templates.py`：本地六文件契约包的只读加载、模板/说明表同序、字段归属及基线摘要检查；原始枚举留在本地，不自动发布。
- `vocabulary.py`：词表发布清单、版本及内容哈希入口检查。
- `cli.py`：校验与 JSON Schema 输出；`python -m media_tag` 或 `media-tag`。

协议说明及尚未实现的语义/权限校验见 [contracts.md](../../../docs/contracts.md)。Pydantic 的 `model_construct` 和 `model_copy(update=...)` 可绕过校验，外部输入不得走这些入口；使用构造器、`model_validate` 或 `model_validate_json`。
