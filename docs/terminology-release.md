# D13 词表契约与发布

2026-11-08：分类/概念/多义别名契约、候选审核、不可变发布和配对回滚工程实现完成。没有发布正式医学词表；D14已另行实现真实BGE CPU编码（[说明](terminology-worker.md)）；医学来源、实际词表管理人及 72 条候选仍待 Q03 提供/审核。此项不代替 D14的医学业务校准、D15 维护页面或 D16 独立业务评测。

## 代码与数据

- `src/media_tag/terminology/contracts/`：严格、拒绝额外字段的 Pydantic 契约。
- `src/media_tag/terminology/lexicon/`：验证、候选、逐条审核、导入、差异、发布、回滚、认证 HTTP。
- SQLite 迁移 **7**：候选修订、审核、幂等请求、不可变发布/审计和唯一生效指针；迁移 1～6 的 SQL/摘要不变。
- 运行数据在私有 SQLite 和 `jobs/terminology-<uuid>/`；不进入 Git，不复制原始模板、候选表或正式词表到代码目录。六份模板保持只读。

## 契约

分类使用 `d13-taxonomy-v1`，包含原模板摘要、`options`。每个选项有稳定 `entry_id`、原字段名、值、active/retired 状态、显式迁移目标、来源及理由。仅允许原模板标明 `external_vocabulary` 的字段；既有闭合枚举/表头不扩充。选项 ID 的字段和值不可改义；变更采用退役及新 ID。退役 ID 不重新启用，回滚也不清除退役历史。

别名使用 `d13-lexicon-v1`，绑定明确的 `taxonomy_release_id`，包含：

| 实体 | 关键数据 |
| --- | --- |
| concept | 稳定 ID、标准名称、类型、定义、范围、分类选项 ID、旧名称、退役及迁移 |
| alias | 原表达、可未知的目标、关系、上下文、默认扩展开关、显式歧义组、逐项来源/理由 |
| relation | 有向起点/终点、关系、扩展开关、来源/理由 |
| source | human/authorized_record/external_reference/synthetic、定位、原句、实际允许使用范围，可选稳定 HTTP 页面 |

关系区分 equivalent/contextual/broader/narrower/related/metaphor。只有 equivalent 能申请默认扩展；多目标且上下文相同必须显式归入歧义组并关闭默认扩展，不取第一项。不同上下文分别保存。上下文包含科室/器官选项 ID、否定、变化方向、必要/排除词，不清洗掉医学语义。实际短语匹配及条件判定由 D14 实现。

不知道专业术语时 `target_id:null` 可以保存草稿，但不能审核通过/发布。发布检查全部来源、有效引用、类型、重复、多义冲突、等价关系的分类一致性、上下位环、退役后的悬挂引用和明确迁移。上下位有向含义：`source broader target` 表示起点是上位；narrower 反向。contextual 的条件保存在 alias，不接受缺条件的 contextual 概念边。

标准名改名保留 ID，并将原名放入 `old_names`。概念类型不得换义复用 ID；概念定义、范围、分类映射和退役/迁移改变时产生语义变更清单。删除已发布概念/选项必须改为显式退役；删除别名/关系则记录为 removed。

## 启用与权限

沿用 [本地配置](../config/README.md) 的 `serve`，新增可选配置：

```json
{
  "terminology": {
    "enabled": true,
    "managers": ["configured-terminology-manager"],
    "publishers": ["configured-terminology-publisher"]
  }
}
```

这些示意 ID 需换成真实凭证绑定 ID，不是默认账号。默认 disabled、名单为空，不产生假管理人。身份来自原有 Bearer 凭证或 D12 会话；调用方不能在请求体授予角色。reader/worker 无写权限，admin 也须明确绑定名单才能审核/发布；医学素材审核角色独立。审核者和发布者可分开绑定；要同时使用预览/发布功能须同时具备对应能力。

普通编辑可提交并修改自己的候选；其他人的候选仅指定词表角色可见。仅 managers 能逐条 approve/reject，publishers 才能发布/回滚。HTTP 使用原有大小限制、同源会话写保护、错误脱敏和 no-store。

```powershell
.\.venv\Scripts\python.exe -m media_tag terminology-schema --model taxonomy
.\.venv\Scripts\python.exe -m media_tag terminology-schema --model lexicon
.\.venv\Scripts\python.exe -m media_tag terminology-schema --model candidate
```

其他 Schema 名：revision、review、publish、rollback、import。命令只输出结构，不初始化服务、不读取运营词表。

## 已实现 HTTP 接口

| 方法/路径 | 行为 |
| --- | --- |
| GET `/terminology/active` | 返回 generation、taxonomy/lexicon release ID；不返回私有来源 |
| GET/POST `/terminology/candidates` | 有范围的列表 / 创建完整候选快照 |
| GET `/terminology/candidates/{id}` | 当前修订、完整候选、逐条审核和发布问题 |
| POST `/terminology/candidates/{id}/revision` | CAS 替换候选；任何修改都回到 draft 并清除审批 |
| POST `/terminology/candidates/{id}/review` | submit；明确 entry_ids 的 approve；整个候选 reject |
| POST `/terminology/candidates/{id}/import` | JSON 行批次预检/导入、逐行报告，最多1000行 |
| GET `/terminology/candidates/{id}/preview` | 当前 generation、候选 revision、校验和差异/影响 |
| POST `/terminology/candidates/{id}/publish` | 校验并构建快照，成功后原子切换 |
| GET `/terminology/releases/{id}` | 指定管理人读取经文件哈希复核的不可变发布及定位 |
| POST `/terminology/rollback` | CAS 切换历史分类/别名配对 |

候选状态：draft → review → approved → published，review 也可 rejected。approved 表示所有 entry_ids 的精确内容哈希已被明确审核，尚不生效。submit 不自动通过，approve 不自动发布。草稿的 `base_release_id` 必须等于创建时同类生效版本；修改 published 候选须创建新候选。发布同时校验候选 revision、base release、全局 generation，避免两位人员覆盖。

每个写请求包含 `request_id`、`note`，修改另有 `expected_revision`，发布/回滚另有 `expected_generation`。幂等按操作者和 request_id 隔离；同 ID 不同内容返回冲突。成功发布重试不重复发布，并复验旧文件。失败重试不占用版本号；使用新私有构建批次。

导入 entity 为 options/concepts/aliases/relations，每行严格按对应契约解析。`apply:false` 仅报告；`apply:true` 遇格式或 ID 冲突整批不写候选，不静默丢行。完全同义的重复项合并来源，保留原 ID 和每个来源；多义项保留并报告发布问题。缺来源/未知目标可以进入草稿，不能发布。没有 CSV/Excel 自动猜列或自动启用种子表；D15 后续提供人员导入页面。

## 发布、失败与回滚

发布为有上限的同步服务端构建。BEGIN IMMEDIATE 序列化本次校验、构建和切换；它不伪装成已实现的异步 lexicon_build Worker，不领取 GPU 槽。若 SQLite 忙，沿用已有503重试行为。D14 的耗时向量构建及 Router 租约需另行实现。

私有批次包括 README、inputs、content、manifest、逐条 review、diff 和 result。业务文件以独占创建写入并 fsync，写完从文件后端重新打开校验全部 SHA256，再提交发布记录和唯一指针。快照与审计不能通过服务修改；SQLite 也禁止更新/删除发布记录。失败批次保留 failure/耗时，但没有生效指针；磁盘文件存在或 result 标记 validated 均不代表已激活，以数据库 head 为准。

每次分类发布将 active lexicon 置空，并在差异中明确标记；新别名必须绑定当前分类 release，避免新分类配旧别名。回滚明确传历史 taxonomy release 和可选 lexicon release，检查所有文件哈希和确切分类引用，一次事务切换整对。generation 始终递增，回滚不复用旧缓存代号。旧快照仍可固定版本读取；损坏不会降级为未校验版本。

分类快照额外包含由 active 选项生成的 `fields` 投影；D06 可消费完整 D13 快照，校验投影与实体一致。沿用 D06 的绝对 manifest 路径、明确 version/manifest SHA256 和 trusted_publishers 绑定，**不会自动改写本地配置或热切换运行中的 D06 固定策略**；部署者须采用审核后的新 pin 并重载。原有 D06 合成消费格式保持兼容。

失效清单标记 query_cache/text_index；当前 `model_index:null`、`resolver_available:false`，没有向量或查询效果承诺。别名变更不创建视觉任务，不更新人工字段、标签确认、许可或隐私复核。语义变更影响列表依据当前账号可读的人工有效分类值，范围明确，不暴露无权素材；尚无 D15 的素材概念关联，不能声称已枚举所有潜在影响。

## 验证与剩余输入

工程测试覆盖 T03/T04/T05/T08/T09/T10/T12 的 D13 部分；不代表 D14 向量一致性或 D15 页面验收。新增62项工程测试；项目`.venv`全量500项、27包纯CPU431项、普通wheel集成157项、4项JS通过，pip check/compileall通过。真实25次HTTP请求及重启/故障恢复报告位于私有 `jobs/d13-validation-20261108-5fbe75bc/validation.json`；61份Markdown和86个wheel模块/资源比对通过。详细状态见 [进度](PROGRESS.md)。

仍需：正式分类选项及稳定 ID、可追溯/有使用范围的候选来源、实际词表审核/发布人员、Q03 支持科室裁定。现有72条候选文件在本机仍未找到；不根据方案举例重建。未提供这些输入不阻塞结构开发，但正式词表及医学业务验收保持未完成。

D14现已增加独立serving指针：D13发布后，词表/向量构建和配对切换由 `/terminology/text-indices/activate` 完成，查询前后整组版本一致；详见 [D14说明](terminology-worker.md)。D13的通用active接口仍只描述发布head，解析状态看 `/terminology/resolve/status`。
