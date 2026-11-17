# 本地素材流水线重构（生产者-消费者）

本目录说明 `src/media_tag/pipeline/` 的批量流水线：扫描 → 预处理 → VLM 打标 → 微批发布。
它独立于现有生产队列（`router`/`upload`/`review` 等），不写源素材、不读取相邻项目配置，
只把结果写到批次目录与本地 SQLite 状态库。

## 目录结构

```
src/media_tag/pipeline/
├── __init__.py
├── __main__.py            # python -m media_tag.pipeline
├── config.py              # TOML 配置解析与校验（未知键直接报错）
├── models.py              # 阶段间传递的结构化数据
├── scanner.py             # 模块 1：单次读取 + SHA256 + 内存去重
├── media_processor.py     # 模块 2：进程池缩图 / NVDEC 视频拼图
├── vlm_client.py          # 模块 3：加权信号量 + httpx 异步池 + 字段校验
├── publisher.py           # 模块 4：微批发布与回执
├── state.py               # SQLite 断点续跑状态
├── metrics.py             # 吞吐/队列/延迟打点
└── schema/tag_fields.schema.json   # 11 字段严格 JSON Schema
config/pipeline.example.toml        # 配置样例
```

## 并发模型

全部阶段同时运行，用有界队列连接，队列满即反压：

```
scanner(线程) ─preprocess_queue(200)─► 进程池(12) ─inference_queue(512)─►
异步推理(32 协程 / 128 槽位) ─publish_queue(2048)─► 微批发布(50..100 条 或 2 秒)
```

- 扫描：每个文件只打开一次，边读边算 SHA256；`seen_hashes` 命中重复只记来源映射；
- 预处理：`ProcessPoolExecutor(max_workers=12)`，图片内存内缩放 896px、JPEG 85、上限 150KB；
  视频一次 FFmpeg 调用按 `fps + tile` 生成 9 格拼图，`decoder=auto` 时优先
  `-hwaccel cuda`（NVDEC），失败自动回退软件解码；
- 推理：统一 `WeightedLimiter(128)`，图片 1 槽、视频 2 槽；`httpx.AsyncClient` 显式连接池、
  60 秒超时、指数退避最多 3 次重试；`response_format`/`guided_json` 约束 11 字段；
- 发布：攒批（≥50 条或等满 2 秒）后一次提交，逐条确认回执；失败整批重试，仍失败则标记
  `failed` 留给下次运行，不重复推理。

## 运行

```bash
# 一次性安装依赖（与项目主依赖分开）
pip install -e ".[pipeline]"

# 准备配置
cp config/pipeline.example.toml config/pipeline.toml
# 需要时指定本地 vLLM/SSH 转发地址与输入目录
python -m media_tag.pipeline --config config/pipeline.toml

# 常用覆盖参数
python -m media_tag.pipeline --config config/pipeline.toml \
  --input "E:/素材/待处理" --limit 200 --workers 12 --concurrency 128
```

`--dry-run` 只做扫描与预处理（验证 FFmpeg/Pillow 链路），`--no-publish` 只做推理不发布。
Ctrl+C 中断后直接重跑即可：已 `published` 的 SHA256 从 SQLite 跳过，失败项自动重试。

## 依赖

- Python 包：`httpx`、`aiosqlite`、`tqdm`，图片解码复用项目已有的 `Pillow`；
- 系统工具：带 NVDEC 的 FFmpeg/ffprobe（NVIDIA 驱动 + CUDA 运行时；无 GPU 时自动走 CPU 解码）；
- 远端：兼容 OpenAI Chat Completions 的 vLLM 服务，支持 `response_format` JSON Schema
  或 `guided_json`（二者可配置回退到仅提示词约束）。

## 输出

批次目录 `outputs/pipeline/<job_id>/`：

| 文件 | 内容 |
|---|---|
| `summary.json` | 扫描/队列/吞吐/状态计数汇总 |
| `published.jsonl` | `local_jsonl` 模式的已发布记录（含字段与预处理元数据） |
| `receipts.jsonl` | 每条素材的发布回执 |
| `failed_records.jsonl` | 预处理或打标失败的记录（含错误与截断的原始回复） |

SQLite 状态库默认 `data/pipeline/state.sqlite`：`files(sha256, status, attempts, result_json, ...)`
支持断点续跑；`duplicates(sha256, path, first_path)` 记录重复来源，物理重复不合并来源。

## 边界与未决项

- `publish.mode = "http"` 的批量发布接口为**拟定**契约
  （`POST {"schema_version":"pipeline-publish-v1","items":[...]}` → `{"accepted":[{"sha256","receipt"}]}`），
  需与云端/本地 API 实际对齐后启用；默认 `local_jsonl` 不依赖任何服务端改动；
- 视频拼图为等间隔采样，不等价于连续覆盖；涉及动作/过程判断时仍需连续证据；
- 医学字段即使模型给出值也仅是初稿，本流水线不写人工有效值、不做医学确认；
- 阈值（896px/150KB/9 格/128 槽位）是初始值，应按实际显存、带宽和模型吞吐压测后调整。
