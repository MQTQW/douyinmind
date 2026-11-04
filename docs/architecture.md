# 结构与检索规则

## 技术分工

本节描述网页知识库与后台RAG（检索增强生成：先找资料再回答）。抖音助手的语义理解和受控发表另有独立门禁，不能把网页的问题分流当成评论执行授权。

| 组件 | 用途 |
| --- | --- |
| React 19、TypeScript、Vite、Tailwind CSS | 网页界面、类型检查、开发构建与样式 |
| FastAPI | 后台开发工具，接收请求并返回视频、知识库或回答 |
| SQLAlchemy、SQLite | 管理本地数据表，保存视频归类和对话 |
| ChromaDB | 保存文字的数字表示，用于按含义查找原文 |
| Qwen Omni、DeepSeek | 分别负责声音识别和依据资料生成回答 |
| Playwright、ffmpeg | 浏览器登录与收藏读取、音轨提取和分段 |

代码回退值与配置模板需分开看：

| 用途 | 代码回退值 | `.env.example`示例值 |
| --- | --- | --- |
| 回答模型 | `deepseek-chat` | `deepseek-flash` |
| 声音模型 | `qwen3.8-omni-flash` | `qwen3.8-omni-flash` |
| Embedding（文字检索转换） | `text-embedding-v4` | `text-embedding-v4` |

运行时优先级是进程环境变量、`.env`、代码回退值；实际可用名称、区域和服务权限由使用者自己的服务账号决定。本公开说明不读取或推测个人当前配置。

## 目录入口

| 路径 | 职责 |
| --- | --- |
| [backend/app/main.py](../backend/app/main.py) | 后台启动 |
| [backend/app/core/config.py](../backend/app/core/config.py) | 配置及参数边界 |
| [backend/app/api/routes](../backend/app/api/routes) | 登录、收藏、知识库、视频和对话接口 |
| [rag_service.py](../backend/app/services/rag_service.py) | 检索、原文上下文及回答编排 |
| [query_intent.py](../backend/app/services/query_intent.py) | 区分问候、视频清单及内容问题 |
| [conversation_query.py](../backend/app/services/conversation_query.py) | 提问前补充最近用户问题线索 |
| [retrieval_fusion.py](../backend/app/services/retrieval_fusion.py) | 合并按意思查找与关键词查找 |
| [retrieval_context.py](../backend/app/services/retrieval_context.py) | 选择原文、补齐相邻句及限制总预算 |
| [knowledge_service.py](../backend/app/services/knowledge_service.py) | 导入编排与资料保存 |
| [ingestion_jobs.py](../backend/app/services/ingestion_jobs.py)、[worker.py](../backend/app/services/worker.py) | 持久任务记录、单一写入 owner 与显式恢复边界 |
| [material_deletion.py](../backend/app/services/material_deletion.py) | 删除意图、阶段确认与原操作继续 |
| [material_audit.py](../backend/app/services/material_audit.py) | 只读一致性核查，未知不记零，不修数据 |
| [prepared_lexical.py](../backend/app/services/prepared_lexical.py) | 有界正文准备缓存 |
| [frontend/src/pages/Workspace.tsx](../frontend/src/pages/Workspace.tsx) | 主工作台 |
| [frontend/src/components](../frontend/src/components) | 对话、知识库、导入、选择器及视频详情 |
| [backend/scripts](../backend/scripts) | 可复用的检索、回答和长视频评测工具 |

## 网页问答的问题分流

收藏登录只用于收藏同步、媒体导入等操作。已有本地资料和对话可独立读取；登录过期不会把知识库会话改成纯 AI。会话保存 `chat_only`、知识库与收藏夹范围，继续历史时恢复原值；显式变更模式或范围应新建会话，后台拒绝把旧会话悄悄改成另一种模式。

只有明确要求视频或标题清单时才只列标题。「有哪些原因」「列出步骤」「初始化后出现什么目录」以及列视频后还要求解释细节的问题，会查正文。纯问候可以直接交流；带有内容问题的问候仍查资料。

明确追问在查找前加入最近最多 3 条用户问题，最多 1000 个字符。旧的助手回答不进入这一步，避免把先前猜测当成事实。事实依据仍只能来自本次资料；模糊指代不保证自动识别。

## 从候选原文到回答

默认最多取 32 条语义候选，与当前范围的正文关键词结果融合排序，排除仅有标题和作者的记录。选择时尽量覆盖不同子问题并减少重复，最终最多提供 8 个正文片段、总计 8000 个字符。

全局与指定库范围都先限制为 SQL 中 `done` 的资料；`deleting`、处理中或失败记录不会因 Chroma 尚有残留向量而继续参与问答。关键词准备缓存只复用兼容的正文签名和范围；真实请求仍包含索引读取、签名检查等工作，不能把缓存命中数字当成完整回答速度。

原文保留时间和声音或画面类型，不先让模型生成摘要替代证据。句子被文字块截断时，只从同视频、同来源且有可靠重叠的相邻块补齐，额外最多 350 字符。放不下的超长候选不阻止继续检查后面较短的候选；必要截取在完整句或行边界结束并标记。

8000 个字符是提供给回答模型的原文预算，不是完整文字稿的存储长度，也不是答案字数。长视频或分散的多项问题仍可能漏掉必要资料，应通过带原话的考卷检查。

来源根据答案实际标注和本次正文白名单返回。未引用时不会把所有候选自动追加为来源；历史记录保存同样的已引用来源。用户可以展开本次提供的原文，但旧历史没有保存的片段不会凭空补造。

## 临时故障和时间

问题转数字的请求使用 `QUERY_EMBEDDING_TIMEOUT_SECONDS=5`，查询不额外重试。明确的临时网络故障可进入关键词后备，界面会说明；密钥权限、配置或本地索引错误仍然报错。这是网络阶段超时，不是完整回答的时间保证。

查找过程区分理解问题、语义查找、读取原文、合并关键词和相邻句补全耗时。细分耗时已包含在所属阶段，不重复相加；候选数和最终片段数分别记录。错误使用固定提示与错误码，不将第三方原始异常直接显示到聊天页面。

普通非流式文字调用与每个入库 Embedding 批次有明确预算和重试边界，SDK 隐式重试关闭；仅对明确的限流拒绝在次数、剩余预算及 `Retry-After` 上限内重试。超时、连接或服务端结果不明时不自动重复付费调用；已经开始的流式输出不重启。批次成功结果在本次调用中保留，后批失败不重跑前批。同步 HTTP 的 I/O 超时不是操作系统级精确墙钟取消保证。

[service_usage.py](../backend/app/services/service_usage.py) 提供有界、进程内的脱敏观测：请求次数、时长、实际返回的用量与未知状态。它不保存问题、密钥或供应商原始错误，也不是持久化的完整账单。

笔记调用使用独立的 `observation_sink`，按具体请求保存计量，不能读取全局“最近一次调用”猜测并发归属。缓存申请引用同一生成过程，不把历史计量当作新的请求再次累计；缺失计量保持未完整，不推断为零费用。

## 本机 AI 笔记

[notes_service.py](../backend/app/services/notes_service.py) 冻结当前视频的识别快照、标题、提示词版本、分段参数与实际模型配置标识；[notes_store.py](../backend/app/services/notes_store.py) 在业务 SQLite 中分别保存申请 UUID、一次生成及各分段/合并步骤。

`POST /api/notes/videos/{id}` 要求明确确认及当前来源指纹，稳定 UUID 绑定这次意图。每个收费步骤先持久提交 `started`，取得成功输出后立即保存；崩溃、超时或提交不明确时保留待核对状态，不自动重新请求。用户以新 UUID 明确重新生成时，只复用相同来源、配置与输入下已核实的成功步骤，不重置旧未知记录。原文、配置或停机状态变化会阻止后续新收费步骤。

GET 只核查状态或下载已保存的固定 Markdown。旧 `GET /api/knowledge/export/{id}?mode=ai` 也只读取兼容的已保存笔记，没有结果时返回提示，不再生成。旧版本笔记可标为过期后继续下载固定字节；删除中或已删除的正文不可下载。全局删除在最终 SQL 事务中同时清除笔记快照、输出和分段副本，留下最小请求、状态与计量记录。笔记不进入 RAG 原文索引。

## 任务与跨存储删除

正式后台从新版开始持久登记入库任务、范围与已观察阶段；没有记录的旧历史、进度或费用不会倒推。重启把未完成工作保留为待核对，不自动重放模型或入库。单一 owner 锁用于拒绝重复写入者，不提供多后台水平扩容；停机等待也不能撤回已经发出的上游请求。

同一 Worker 以 `ingestion`、`notes`、`generic` 区分任务。入库分页、计数与重提先按类型过滤；所有类型仍共享队列、视频互斥及维护保护。旧台账的类型升级在取得单一 owner 后进行，不改写旧 metadata 内容。停机总预算包含状态锁、台账写锁等待和线程退出；`stopped` 表示线程事实，超时或状态未能保存另行标明，不把未知排队数当作确定的零。原生 I/O 本身不是可强制取消的硬实时操作。

用户确认重提后，以新的任务编号保留处理记录，并关联原任务。`request_id` 是这一次申请的幂等编号：网络未知先只读查询同一编号；本次暂未找到登记记录不等于证明没有副作用，再次确认仍复用原编号。已完成资料复用；原任务、旧失败及未知费用不会清零。

全局删除先在业务 SQLite 提交操作意图和 `deleting`，此时保留原文与归属；再依次核对索引删除、媒体清理及最终 SQL 清除。错误可能发生在外部删除之后，SQL 回滚不代表向量或文件已恢复。显式继续只恢复原操作；旧操作已完成时只返回原结果，不删除后来重新入库的资料。资料身份或指纹变化会拒绝旧操作继续。

向量替换先检查全部输入和旧快照，再 upsert 新块并删除多余旧块；失败时尝试内存快照补偿并读回核对，无法确认时抛错，不标记 `done`。这不是断电后的跨存储回滚保证。

只读核查分别返回 `read_complete`、`content_verified` 和问题项。旧索引无可靠正文标志时保持未核实，不能只凭块序号判断有无正文；明确选择本地旧文核对后才读取原文分类，报告仍不返回正文或 Embedding。读取失败、缺表或观察期间变化保留未知，不按零或健康处理。工具没有修复能力。[CLI](../backend/scripts/audit_materials.py) 默认只校验参数，显式运行也只比较给定 SQLite 快照与既有安全索引导出，不自建 Chroma 客户端；导出对照不代表实时索引状态。

本机维护接口为 `GET /api/maintenance/material-audit` 和 `GET /api/maintenance/index-snapshot`，都要求本机访问边界。它们只使用已初始化的索引实例，未就绪返回 503 和未知状态，不隐式创建索引。核查可显式选择 `verify_legacy_text=true` 读取本地旧文；默认导出只含安全元信息计数与摘要，不含正文、向量或模型密钥。`complete` 表示核查范围完成，不表示没有问题，仍须核对问题数与条目。

## 合成 CPU 基准的口径

2026-11-04 的最终报告 `prepared-final-20261104T120056Z-89b9a6c5.json` 使用固定 seed `20261104`、每块 384 字符、top-k 8，环境为 Windows、CPython 3.12.14。下表只列同一合成问题 `python_environment` 的融合函数壁钟耗时：冷路径是首次准备的单次观测，暖路径是一次预热后 5 次调用的中位数，数值四舍五入到 0.01 毫秒。

| 正文块数 | 冷路径（毫秒） | 暖路径中位数（毫秒） |
| --- | ---: | ---: |
| 100 | 46.02 | 17.28 |
| 500 | 179.04 | 23.47 |
| 2000 | 1142.35 | 76.48 |
| 10000 | 5249.62 | 286.57 |

配套 `prepared-exact-output-20261104T120533Z-3f0c90da.json` 中，4 种规模 × 6 个合成问题的未缓存、冷、暖完整结果字典一致，输入未改变。它证明这些受控样例的缓存兼容性，不证明真实库准确率。测量不含网络、SQL、Chroma I/O、Embedding 或 LLM，**不是端到端 RAG 延迟或费用承诺**；内存探针也只计 Python 分配，不含原生/RSS。

用公开脚本复测，报告放在项目之外：

```powershell
# 在 backend 目录；纯合成 CPU，不需要模型密钥或运行服务
$env:PYTHONHASHSEED='0'
$reportDir = Join-Path $env:TEMP 'douyinmind-retrieval-benchmarks'
uv run --locked --extra dev python scripts/benchmark_retrieval.py --output-dir $reportDir
```

## 实验性评论助手的独立链路

模型只读理解自然原话，再由程序核对当前账号、设备、库权限与原评论，不能把模型输出当授权。未指库提问使用本人本机全部入库资料；未指库保存先在原线程询问目录。明确指定库仍严格限于该库。

| 公开源码入口 | 作用 |
| --- | --- |
| [semantic_intent_model.py](../backend/app/assistant/semantic_intent_model.py) | 只读理解提问、保存、澄清或取消，不发执行工具 |
| [semantic_plan_store.py](../backend/app/assistant/semantic_plan_store.py) | 持久记录一次理解及范围，未知结果不自动重复付费 |
| [backend_knowledge.py](../backend/app/assistant/backend_knowledge.py) | 通过受限的本机接口复用后台索引，不另建并行索引写者 |
| [thread_followup.py](../backend/app/assistant/thread_followup.py) | 先核对已有原线程与当前普通回复，再允许只读追问；多锚点不猜 |
| [publisher.py](../backend/app/mention_agent/publisher.py) | 发表前目标核对、一次发送记录与未知结果保留 |

网页回答可以显示和复制来源；评论公开正文不追加来源编号尾巴，内部仍记录实际依据。B回读可见与A接收分开。首次配置、后台B会话和本人核验限制见[助手说明](抖音助手使用说明.md)，不在这里假设全新安装已经完成初始化。

语义层默认开启；本机 `semantic-intent.json` 的显式关闭不会清除已有台账或改变授权。新公开正文在证据校验后冻结，普通回答与入库回执默认总预算 320 字符，明确摘录和选库菜单为 280，均含程序称呼，超限不自动截断。v2 菜单只缩略显示名并保留原序号、完整选项与授权；旧 v1 菜单依原版本核对，不更新原正文、引用或 hash。

## 开发检查

前端有 `package-lock.json`，后端已有 `uv.lock`。开发检查使用锁定依赖；不复制或填写真实 `.env` 来运行合成测试。

```powershell
# 在 frontend 目录
npm ci
npm test
npm run build
```

```powershell
# 在 backend 目录，安装测试依赖后执行
uv sync --locked --python 3.12 --extra dev
uv run --locked --extra dev python -m pytest
```

单元测试使用模拟服务或临时数据；主动执行带 `--run` 的真实评测会调用配置的模型服务。两者的结果和费用口径应分开。关键合成集合可按[便携隔离说明](isolated-validation.md)在公开源码快照中运行，完整 pytest 与特定隔离集合的范围也应分别报告。CI 工作流是否通过，以实际 Actions 运行记录为准，不能把工作流文件存在当作已执行。

启动后访问 [后台接口说明](http://127.0.0.1:8000/docs) 查看当前接口，包括知识库管理、已保存视频分页、处理状态、导出及流式问答。

## 当前边界

项目主要面向本地个人使用，已有本机用户、设备与库权限登记，但没有实施真实跨电脑任务通道。单 owner 保护不等于分布式事务；资料库规模、模型服务稳定性和识别质量仍会影响效果。合成回归、真实本地 SDK 检查与少量合成输入的真实模型联调应分别报告，均不能代替新版真实抖音 A/B 场景验收。评论中的保存后自动总结复合指令仍未串联，本机单视频笔记需单独明确确认。

[返回首页](../README.md)
