# 测试

使用项目`.venv`执行`python -m pytest`。D00结构、D01索引/存储、D02真实工具解码、D03队列/API、D09上传/衍生契约及D17授权文件/预览/下载基础均使用临时合成数据，D04新增真实OCR/人脸/CLIP与生命周期测试后D10新增31项、并发存储路径新增2项后D11新增40项，D12新增18项、D06新增38项后当前共438项Python测试；D12另有4项Node测试及真实浏览器闭环；D02缺工具会失败，不以skip代替环境验收。D03包含真实子进程终止恢复和loopback HTTP；D04使用61包视觉锁、固定权重及公开诊断图，实际运行CPU/GPU；仍不代表生产素材、医学或部署/备份业务验收。D10验证完整视频、遮罩修订、时间映射、复核/撤回及Windows进程树；仅核心环境执行`pytest --ignore=tests/models --ignore=tests/redaction --ignore=tests/retime/test_combined.py --ignore=tests/web/test_privacy.py --ignore=tests/export/test_privacy.py`。真实本地模板及D02/D03/D09/D17文件基础jobs验证批次另见对应文档。

D06复核/导出测试只读加载本地六原始模板，不提交契约副本；未提供模板时如实失败。包含合成发布消费/人工决定状态机，不是生产医学审核；真实浏览器输入/原文/非零起点片段与两表工程批次见docs/review-csv.md。

D13新增 `terminology/lexicon/` 的合成分类/概念/别名、逐条审核、导入/来源/引用/关系、发布故障/并发/回滚及D06消费验证。`python tests/terminology/lexicon/validate_http.py` 在私有jobs批次执行真实loopback请求，不发布正式医学词表。

D14新增40项术语/文字工程测试；真实推理在 `terminology/embedding/test_inference.py`，无模型核心环境显式排除此文件。`python tests/terminology/resolve/validate_http.py` 执行真实CPU BGE、40次HTTP、索引恢复/重启/回滚及两次CLI Worker，数据只在私有jobs。

D19 `client_adapter/` 新增13项固定下载/用途/任务边界测试；`python tests/client_adapter/validate_provider.py` 执行实际loopback、BGE、真实变速副本、断点恢复及独立CLI。检查单仅合成契约验证，未声称实际AI观看或宿主采用。

D07 `maintenance/` 新增13项离线备份/校验/还原测试；`python tests/maintenance/validate_restore.py` 使用已停服的合成D20工程批次，实际执行三个独立CLI并启动隔离恢复后的HTTP/BGE，比较值/权限/衍生/词表索引配对；无生产部署或业务签收。

D07本机部署新增`deployment/`最终7项配置/锁/停止代次/真实解释器Job归属与分配失败清理；`validate_local.py`实际GPU租约杀进程恢复/HTTP/模型/备份，`validate_installed.py`实际EDY计划任务和Codex全局MCP停服/重连。`mcp_adapter/validate_codex.py`使用真实已登录Codex CLI四工具/图像/撤权；宿主输出和运行数据只存忽略目录，无业务签收。

D19实际剪辑来源新增12项编排边界测试；validate_editing.py对E盘和剪辑仓库的显式新任务验证原隐私/登记/验收/素材校验、复用与撤权。详见client_adapter/README.md；真实AI观看和D20完整剪辑预览另验。
