# 测试

使用项目`.venv`执行`python -m pytest`。D00结构、D01索引/存储、D02真实工具解码、D03队列/API、D09上传/衍生契约及D17授权文件/预览/下载基础均使用临时合成数据，D04新增真实OCR/人脸/CLIP与生命周期测试后D10新增31项、并发存储路径新增2项后D11新增40项，D12新增18项、D06新增38项后当前共438项Python测试；D12另有4项Node测试及真实浏览器闭环；D02缺工具会失败，不以skip代替环境验收。D03包含真实子进程终止恢复和loopback HTTP；D04使用61包视觉锁、固定权重及公开诊断图，实际运行CPU/GPU；仍不代表生产素材、医学或部署/备份业务验收。D10验证完整视频、遮罩修订、时间映射、复核/撤回及Windows进程树；仅核心环境执行`pytest --ignore=tests/models --ignore=tests/redaction --ignore=tests/retime/test_combined.py --ignore=tests/web/test_privacy.py --ignore=tests/export/test_privacy.py`。真实本地模板及D02/D03/D09/D17文件基础jobs验证批次另见对应文档。

D06复核/导出测试只读加载本地六原始模板，不提交契约副本；未提供模板时如实失败。包含合成发布消费/人工决定状态机，不是生产医学审核；真实浏览器输入/原文/非零起点片段与两表工程批次见docs/review-csv.md。
