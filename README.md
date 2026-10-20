# DouyinMind · 抖音收藏夹 RAG 知识库

> 把你的抖音收藏夹变成可搜索、可对话的个人知识库

## 它能做什么

登录抖音 → 同步收藏夹 → 选择视频入库（音频提取 → 完整文字稿 → 可搜索资料）→ 对话问答

```
收藏夹视频 → 提取音频 → Qwen3.8-Omni-Flash逐句转写 → 保存完整文字稿与时间信息 → 文字分段 → 资料检索数据转换 → ChromaDB → DeepSeek对话
```

## 快速开始

### 前置条件

- Python 3.12+
- Node.js 18+
- ffmpeg（加入 PATH）
- Chrome 或 Edge 浏览器

### 1. 安装后端依赖

```powershell
cd backend
pip install uv
uv sync --python 3.12
playwright install chromium
```

### 2. 配置环境变量

```powershell
cp .env.example .env
# 编辑 .env，至少填入：
#   DASHSCOPE_API_KEY=你的百炼密钥（声音/画面识别与资料检索使用）
#   DEEPSEEK_API_KEY=你的DeepSeek API Key
#   QWEN_OMNI_MODEL=qwen3.8-omni-flash
#   QWEN_OMNI_BASE_URL 与 EMBEDDING_BASE_URL 按密钥所属区域填写
```

### 3. 安装前端依赖

```powershell
cd frontend
npm install
```

### 4. 启动

Windows 本地依赖已安装后，可直接双击项目根目录的 `启动项目.bat`。请保持启动窗口打开；按 Ctrl+C 可停止该窗口启动的服务。已经运行的本项目服务会直接复用，其他程序占用端口时会提示，不会关闭其他程序。启动日志保存在 `backend/app/storage/local_services`，不会上传到 Git。

也可分别在两个终端中启动：

```powershell
# 终端1：后端
cd backend
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# 终端2：前端
cd frontend
npm run dev
```

打开 http://localhost:5173

## 使用流程

1. 点击「扫码登录」→ 浏览器自动打开抖音登录页 → 扫码
2. 登录成功后，点击「同步」拉取收藏夹
3. 在「我的知识库」点击「新建」，输入主题名称；也可以在导入确认窗口中创建
4. 展开收藏夹，勾选需要的视频（可跨收藏夹选择），点击「导入所选视频」，选择目标知识库再确认；点击「整夹导入」或选中当前收藏夹全部视频时，默认自动使用收藏夹名称创建知识库，已有同名库直接复用。只选部分或跨夹混选时仍手动选择目标库；「导入全部收藏」默认命名为「全部收藏」
5. 选中知识库，在右侧对话区根据库内已完成的视频提问

一个视频可放入多个知识库。已完成识别的视频直接复用文字稿和检索资料，不再次调用 AI；新视频或失败重试才进行识别。旧的已入库视频仍保留在「全部已入库资料」，也可勾选加入新库。「移出」只删除当前知识库的归类，原文字稿保留；收藏夹中的「删除入库数据」会删除全局资料，并从所有知识库移除。

知识库检索、视频列表和总结均限定当前库中的已完成视频；空库不会从其他库补充资料。切换知识库会开始新的对话，历史对话记住所属知识库，重新打开时恢复对应范围。抖音取消收藏或再次同步不会清空已经归入知识库的资料。

回答会随着 AI 生成逐步显示，完成后显示来源和检索过程。连接中断或生成失败时，页面保留已收到的文字并显示提示；未完成的回答不会写入聊天记录。

每个问题支持去掉首尾空白后最多2000个字符。普通中文或😀这类表情通常各计1，组合表情可能计多个单位，以输入框显示的计数为准。过长时保留输入并提示修改，不会悄悄截掉后半句。单纯问候可以直接交流；“你好，请解释视频中的步骤”仍会查找正文，英文单词中的 `hi` 也不会被当成问候。

“那两个数字呢”“它为什么这样”等明确追问，会在查找前附带最近最多3条用户问题作为线索，历史线索总计不超过1000字符，当前问题保持完整。旧的助手回答不进入这一步，避免把之前的猜测当成事实；切换知识库仍需要新建对话。主体只在助手回答里出现或指代不明确时，这套规则不保证能自动识别，应明确写出想问的对象。

内容问答先排除仅有标题和作者的记录，再从候选中选择正文，直接保留原话、时间及声音/画面类型，不生成中间摘要来替代资料。默认将最多32条语义候选与当前范围内的正文关键词查找进行融合排序，优先覆盖不同子问题并减少重复片段；最多提供8个正文片段，仍受8000字符总预算限制。原文未提供的数字、路径、命令参数或功能要明确说明缺少依据，不主动用通用知识补齐；多项提问逐项回答，不限为3点。

命中固定字符块后，只从同一视频、同一声音/画面来源且有可靠原文重叠的相邻块补齐句子，总新增最多350字符，不重写文字稿、不重建索引。超长候选放不下时继续检查后续较短候选；首块过长只在完整句/行边界截取并明确标记未提供的后文。实际参与补句的原始块编号会进入检索追踪和已引用来源记录。

问题转换为检索数字的网络请求使用独立超时配置 `QUERY_EMBEDDING_TIMEOUT_SECONDS=5`，不额外重试，也不改变视频入库的批量转换规则。它是网络阶段超时，不是完整回答必须在5秒内结束。明确的临时超时、连接故障、限流或服务端故障发生时，组合查找可以继续使用当前范围的关键词匹配；页面会标为“关键词查找（后备）”，这类来源不会显示虚假的语义相似分数。密钥权限、配置、无效数据或本地索引错误仍然报错，不会伪装成成功或空知识库。关键词后备也可能漏掉用词差别很大的资料，不代表与正常查找效果完全相同。

展开回答下方的查找过程，可查看理解问题、按含义查找、读取原文、合并关键词结果、补齐相邻句各自耗时，以及实际交给回答模型的片段数与字符数。细分耗时已包含在上方查找时间里，不重复相加；检索候选数与最终提供的片段数分别记录。服务报错只使用固定中文提示和错误码，不把第三方服务的原始异常返回到聊天页面或聊天错误日志。

每个片段可展开“查看本次提供的原文”，核对预览后面的限制、数字和时间；文字可选中，长片段可滚动查看。它展示这次实际交给AI的片段，不是整篇文字稿，也不表示每段都被回答引用；整篇文字稿仍从视频详情查看。旧的历史记录如果没有保存这些片段，不补造完整原文。

来源在回答完成后，根据回答实际标注与本次正文白名单返回；没有引用时不会把所有检索结果自动追加为参考来源。对话历史保存相同的已引用来源，重新打开也不会将未使用的候选当作依据。历史助手回答仅作对话背景，不作为事实证据。

左侧「历史对话」可点击打开、查看之前的消息并继续提问，选中的对话会高亮。每行右侧可重命名或删除；删除需要确认，并会删除这一段对话中的全部消息。点击「新对话」可单独开始一次聊天。回答生成期间暂时不能切换或删除当前对话，避免未完成的回答进入其他会话。

首页的「先体验 AI 对话」可在未登录抖音时打开聊天，只需配置 DeepSeek 密钥即可。此时不会使用收藏视频；收藏内容问答仍需扫码登录、同步和入库。窄窗口可通过顶部「收藏夹」按钮切换资料面板与聊天。

本地 `.env` 配置中的 `LLM_MODEL=deepseek-flash` 对应 DeepSeek 4.1 Flash。`LLM_THINKING_ENABLED=false` 关闭深度思考，让回答更快开始显示。

## 上传范围与本地隐私

仓库只包含源码（应用的程序文件）、通用测试、依赖清单和配置模板。使用者需要填写自己的密钥，并扫码登录自己的账号。

以下内容保留在使用者电脑上，不随 Git 上传：

- `.env` 等本地配置中的实际密钥；`.env.example` 仅保留空值与示例。
- Cookie（相当于已登录账号的通行证）、浏览器登录资料和登录状态文件。
- `backend/app/storage/` 内的数据库、收藏记录、知识库归类、聊天历史、文字稿、音视频、检索索引和识别缓存。
- 含实际问题、回答、原文或回听片段的评测结果、个人持续优化记录，以及绑定本机资料的实验脚本。
- 本机截图、运行日志、依赖目录和编译缓存。

不要将这些私人文件复制到 `frontend/public/` 或其他源码目录；网页静态目录中的文件可以被访问者读取。忽略规则不会清除已经上传过的 Git 历史，也不能代替提交前检查。

模型调用的资料流与 Git 上传分开：正常转写会把所选音频发送给配置的识别服务，问答会把问题及检索到的原文发送给回答服务。以上忽略规则保护仓库上传范围，不表示模型服务从未接收资料。

### 首版：完整音频文字稿

当前默认 `KNOWLEDGE_ANALYSIS_MODE=audio`，使用 `qwen3.8-omni-flash`，只提取和识别音频。默认每 60 秒一段，同时识别最多 3 段（`AUDIO_TRANSCRIPTION_CONCURRENCY=3`，范围1–4，设1恢复逐段处理）。每段完整听写，全部片段处理完后按原时间合并，不用摘要替代文字稿。文字、原始响应和警告均按视频顺序保存，进度显示已成功完成的片段数，而非当前启动到第几段。

识别结果格式错误、时间越界或返回被截断时，会先自动重试，再按需要拆成较短音频重新听写；每个原始分段最多进行10次识别尝试、最多拆分两层，权限和账户错误不触发分段重试。连接服务时的底层网络重试另计，实际网络请求次数不等同此处的识别尝试数。通过校验的进度与失败响应保存在音频旁的本地文件中，再次处理相同音频和配置时复用成功分段；所有分段完成后才标记入库成功，不跳过失败内容。

拆成两半后，如果前半已成功、后半失败，重试也会读取并重新校验已保存的成功半段，不重复识别它。音频内容或模型、提示词等识别配置变化时不会混用旧结果；完整原始分段复用数与拆分子段复用数分别记录，缓存命中不算一次新的识别请求。

默认采用打包并行补听：最多12个交界，各取前后5秒，放入至多两包互不连续的短原声；另用一包前后4秒的独立窗口确认最多4处重复汉字。最多3包同时处理，并受主音频并发设置约束（`AUDIO_BOUNDARY_BATCH_ENABLED=true`、`AUDIO_BOUNDARY_BATCH_MAX_WINDOWS=12`）。短视频可覆盖全部切点；更长视频在固定预算内兼顾明显症状和分散位置。补听文字必须与两侧足够长、唯一、顺序一致的原话对应，才允许改中心极小范围；窗口两端从不整段覆盖原稿。

涉及数值、单位、否定、英文内容、删字或歧义仍保守回退。唯一新增的删字例外是：主切点左右恰好重复同一汉字，两个不同范围原声的识别都逐字支持同一结果，且只删除这个交界重复字，其他字母、数值字符、单位和符号没有任何别的改动。正常复述或两次识别不一致时保留原稿。模型提示“短窗开头/结尾可能截断”会保留在记录中；只有这类明确边缘提示可继续核对中心，“听不清”和未知警告仍拒改。补听只返回文字，用实际原声窗口定位；最终合并保留原主稿的时间包络，不让模型重新估计句子时间。

这会增加音频输入与识别成本。一段相同12分34秒音频的旧实测为13主段+4逐窗补听，81.563秒、补听16.172秒；打包新实测为13主段+3包，55.172秒、补听12.656秒，检查4→12个交界。两次主转写和网络有随机波动，不能把全部总耗时差异归因于新补听，也不代表固定提速。新实测当时采用5处；随后仅修正边缘提示分类，对同批全部响应离线重放采用9处（7处字词、2处断句），没有重抽模型。请求数减少不代表费用下降：新检查覆盖更多原声，补听输入量增加，未读取账单。每个补听网络阶段20秒、无SDK重试；60秒后停止发新请求，流读取也检查截止并关闭。这并非严格60秒整硬取消。异常时保留原稿。`AUDIO_BOUNDARY_REVIEW_ENABLED=false`关闭所有补听；`AUDIO_BOUNDARY_BATCH_ENABLED=false`退回最多4窗的旧方式。

原主段响应、补听响应、原记录、锚点与接受/回退理由分别保留；补听缓存独立于主段缓存，并绑定音源、模型、提示词、策略和本次主段原文。`audio_processing` 分开记录主段/恢复识别次数、补听次数、补听复用数及总识别次数；普通生产记录不冒称精确网络次数。合并记录沿用完整原记录的时间包络，因此时间可能较粗，不代表逐字对齐。已有完成的正式文字稿不会因升级自动重写。

并行处理不改变分段长度、听写提示或识别校验规则，已有成功片段仍可复用。某段失败时停止继续提交新片段，等已经开始的片段保存结果后整体报错，重试只处理尚未成功的片段。程序还会检查分段是否连续覆盖完整音频；这能防止处理流程漏段，但模型是否听错或漏听仍需对照原声检查。并发度和尝试次数随识别结果保存在 `audio_processing` 中，便于比较耗时。升级时先等当前导入结束，再重启服务加载新代码，避免中断正在处理的视频。

意外关闭后台后，卡在“下载中/识别中/保存中”的视频可使用“重置失败/卡住的视频”恢复为待处理，再手动勾选导入；这不会删除已有文字稿或成功分段。重置后列表会刷新，服务忙碌时会明确提示，恢复动作本身不调用AI。准备提交、排队或执行入库时不允许重置；重置期间也不接受新的入库提交，避免误改活跃状态。

上述任务协调面向本项目的单后台进程，不提供多后台进程共用资料目录的保证。数据库归类与内存任务队列仍是两次提交：如果极少见的队列发布异常发生在归类完成后，归类可能已保存而任务未排队；程序不会为掩盖错误自动补发任务。

抖音媒体下载复用项目中已登录的浏览器，从所选视频网页取得当前播放资源。平台只提供完整视频文件时，会先下载到本地再提取声音，不进行画面分析。下载会核验视频编号、完整文件和音轨，残缺缓存不会当作成功下载；若抖音页面要求人工验证，应在正常浏览器中完成后重试。失败视频旁的「查看原因」会显示中文说明及可展开的技术详情。

- 保存完整文字稿、逐段时间记录和原始模型返回。
- 模型返回被截断、时间越界或结果格式不完整时，任务报错，不将其标记为成功。
- 首版没有可识别人声的视频暂无文字稿可入库；画面分析不自动作为替代处理。
- 点击视频标题可查看「完整文字稿」和时间记录，并导出完整结果。
- 文字稿进入资料检索，后续问答继续使用已配置的 DeepSeek。
- 资料检索转换失败时，已完成的文字稿仍保留，可查看后重试。

画面分析保留为后续可开启功能：将 `KNOWLEDGE_ANALYSIS_MODE` 改为 `audio_visual` 后，新入库视频会同时进行声音转写与画面分析。已保存的旧画面结果仍可查看和导出，切换模式不清除已有资料。

模型识别可能存在听不清或看不清的内容；相关不确定性会保存在结果中。实际模型支持范围与分段限制见[千问官方文档](https://www.alibabacloud.com/help/en/model-studio/qwen-omni)。

### 流式回答验证

```powershell
# 前端：验证分批消息解析、中文和表情、收藏夹参数、断线处理
cd frontend
npm test
npm run build

# 后端：使用模拟 AI 服务和临时数据库验证逐步输出、聊天记录和错误处理
cd ../backend
python -m pytest tests/test_chat_stream.py
```

## 检索考卷：用同一套问题比较优化前后

`backend/scripts/evaluate_retrieval.py` 用已标注的问题、正确视频、原话和时间检查检索。评分区分“找到正确视频”“找到至少一条支持答案的原文”和“找到全部标注依据”；只找到标题不算找到答案依据。还会单独记录问题是否被分到只查标题等流程。

提问时，只有明确索要视频或标题清单（例如「有哪些视频」「关于 Git 的视频有哪些」）才只列标题。「有哪些原因」「列出操作步骤」「初始化后出现什么目录」以及列视频后还要求解释细节的提问，进入原文检索；不会因单独出现「哪些、清单、目录」就跳过正文。

题库和实测结果保存在本地 `backend/app/storage/retrieval_eval/`，该目录已被 Git 忽略，仓库不附带个人题库。请根据自己的已入库资料标注有答案题与无答案/范围隔离题；答案以可核实的原话为准，并回看原视频核对识别不清的内容。

在 `backend` 目录执行，先免费检查题库中的引用和范围，再运行实测：

```powershell
# 只检查题库，不调用模型服务
.\.venv\Scripts\python.exe scripts/evaluate_retrieval.py --dataset app/storage/retrieval_eval/retrieval-exam-v1.json --output app/storage/retrieval_eval/validation-v1.json

# 实测：只调用将问题转换为检索数字的服务，不生成回答，也不写聊天记录
.\.venv\Scripts\python.exe scripts/evaluate_retrieval.py --dataset app/storage/retrieval_eval/retrieval-exam-v1.json --output app/storage/retrieval_eval/baseline-v1.json --run

# 生成可打开阅读的报告和 Excel 可读的考卷
.\.venv\Scripts\python.exe scripts/render_retrieval_report.py --dataset app/storage/retrieval_eval/retrieval-exam-v1.json --results app/storage/retrieval_eval/baseline-v1.json --output app/storage/retrieval_eval/检索考卷与基线报告.html --csv app/storage/retrieval_eval/检索考卷.csv
```

实测使用资料与索引的临时副本，保留题库、资料内容和检索参数的版本指纹，方便以后对照。当前只评“找资料”和问题分流，不评资料压缩后的内容或最终回答质量。无答案题保留人工检查项，不把“返回了几个相似片段”当成回答正确。

改动后使用新的结果文件，保留第一次成绩，再生成前后对比：

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_retrieval.py --dataset app/storage/retrieval_eval/retrieval-exam-v1.json --output app/storage/retrieval_eval/after-routing-v1.json --run
.\.venv\Scripts\python.exe scripts/render_retrieval_report.py --dataset app/storage/retrieval_eval/retrieval-exam-v1.json --before app/storage/retrieval_eval/baseline-v1.json --results app/storage/retrieval_eval/after-routing-v1.json --output app/storage/retrieval_eval/问题分流修复前后对比.html
```

对比报告仅在前后考卷、资料范围及模型和检索参数一致时生成；无答案题不计入内容问题的误判率。

### 最终回答：是否答全、是否有据

`evaluate_answers.py` 使用同一套题，按网页真实流程执行资料检索、原文上下文和流式回答。会调用真实模型服务；每题只记录一次生成，不反复挑选较好的答案。旧版本中的摘要调用按实际记录保留，当前版本不再调用中间摘要。检索资料与数据库均使用临时副本，测试聊天不会写入用户历史；正确答案和评分依据不会传给回答模型。

```powershell
# 先免费核对考卷、资料、模型与检索基线是否一致
.\.venv\Scripts\python.exe scripts/evaluate_answers.py --dataset app/storage/retrieval_eval/retrieval-exam-v1.json --reference app/storage/retrieval_eval/after-routing-v1.json --output app/storage/retrieval_eval/answers-validation-v1.json

# 真实生成；已有输出不会被覆盖，原始检索、摘要输入输出与最终模型输入分别记录
.\.venv\Scripts\python.exe scripts/evaluate_answers.py --dataset app/storage/retrieval_eval/retrieval-exam-v1.json --reference app/storage/retrieval_eval/after-routing-v1.json --output app/storage/retrieval_eval/answers-v1.json --run

# 逐题人工核对后，将评阅记录与一次生成的回答一起输出为报告
.\.venv\Scripts\python.exe scripts/render_answer_report.py --dataset app/storage/retrieval_eval/retrieval-exam-v1.json --answers app/storage/retrieval_eval/answers-v1.json --review app/storage/retrieval_eval/answer-review-v1.json --output app/storage/retrieval_eval/最终回答准确性与依据报告.html --csv app/storage/retrieval_eval/最终回答逐题核查.csv
```

人工评阅分别检查必要知识点是否覆盖、具体事实是否有原文依据、引用是否支持对应说法。合理同义解释及清楚区分的通用建议不当作捏造；核心点答到了但夹带无据产品能力、数字、文件路径等，仍不能视为完整有据。无答案题独立检查是否承认当前范围没有依据。报告基于本次生成与当前文字稿，不把一次测试的比例当作长期准确率。

修复后使用新的回答及评阅文件名，不覆盖旧样本。`render_answer_report.py` 可加 `--before-answers` 和 `--before-review` 生成前后逐题对照；只有题库、资料、索引及模型配置一致时才允许比较。源码变化单独展示，实际模型调用次数也按记录统计。

### 长视频测试流程（约 7–30 分钟）

本地测试目录可设为 `backend/app/storage/long_video_tests/my-test-run/`，测试数据和回听音频均被 Git 忽略。手动导入并确认准备好后再执行质量检查，评测命令不会定时自动运行。

新处理任务会在 `app/storage/ingestion_runs/视频编号/` 保存每次尝试的下载/音轨提取、分段转写、整理入库与总处理时间。失败和重试分别记录，总时间不含排队。视频详情可展开“本次处理耗时”；以前的视频若没有计时，不用数据库创建时间或文件时间倒算。回答评测另外记录首段文字出现和完整回答耗时。

测试准备时当前批次已在旧服务运行，不能为它补造精确计时。`observe_ingestion.py` 只给这批保存轮询观察记录，明确标为有误差、已开始阶段起点未知；不做内容评测、不重启服务。等本批结束且用户通知后，先安全加载更新。`LONG_VIDEO_TEST_KEEP_AUDIO=true` 已在本地配置开启：更新加载后的约6–31分钟音频会为回听保留，覆盖7–30分钟边界，测试结束可关闭。旧批次若WAV已清理，可从保留的本地原视频提取音轨，不自动下载或重新识别。

在 `backend` 目录执行：

```powershell
# 准备时冻结旧视频清单；已有baseline不会被覆盖
.\.venv\Scripts\python.exe scripts/long_video_test.py init --run-dir app/storage/long_video_tests/my-test-run

# 用户确认导入完成后：只读检查新增/状态改变/指定本批视频，生成报告和待标注考卷，不调用模型
.\.venv\Scripts\python.exe scripts/long_video_test.py inspect --run-dir app/storage/long_video_tests/my-test-run

# 根据实际inspection路径与视频编号生成原声抽样（不调用AI）；以下占位值需替换
.\.venv\Scripts\python.exe scripts/long_video_test.py clips --run-dir app/storage/long_video_tests/my-test-run --inspection 检查目录/inspection.json --video-id 视频编号

# 回听后填写question-worksheet：正例source_audio_verified=true，负例scope_absence_verified=true，status=ready
.\.venv\Scripts\python.exe scripts/long_video_test.py compile --run-dir app/storage/long_video_tests/my-test-run --worksheet 检查目录/question-worksheet.json
```

回听开头、中间、结尾及每分钟交界，并检查长无文字间隔、重复句与专有名词；静音、音乐和真实复述不能自动判漏听。程序能检查分段请求是否覆盖整个音频、时间范围与完整文字稿是否一致；这些通过也不等于每句话都听对。每片准备6道可回答题和2道无答案/知识库范围对照，参考原话和时间必须核实；未标注完或引用不在当前库的题目不能执行。

生成 `long-video-exam.json` 后，用前面的 `evaluate_retrieval.py` 冻结本批检索基线，再用 `evaluate_answers.py` 完整生成并逐项核查。测试始终使用资料副本，不把测试对话混进用户历史；不改变原题/原话来追求好成绩。

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | FastAPI + SQLAlchemy + SQLite |
| 向量库 | ChromaDB |
| LLM | DeepSeek（OpenAI 兼容） |
| Embedding | DashScope text-embedding-v4 |
| 音频识别；后续画面分析 | Qwen3.8-Omni-Flash（阿里云百炼） |
| 视频下载和分段 | 抖音已登录网页播放资源；其他网站 yt-dlp；ffmpeg 提取和分段 |
| 浏览器 | Playwright + Chromium |
| 前端 | React 19 + Vite + TypeScript + Tailwind CSS |

## 项目结构

```
douyinrag/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口
│   │   ├── core/config.py          # Pydantic 配置
│   │   ├── db/                     # SQLAlchemy ORM
│   │   ├── models/entities.py      # 数据表实体
│   │   ├── api/routes/             # 14 个 API 接口
│   │   └── services/
│   │       ├── douyin_collector.py # Playwright 登录 + 数据抓取
│   │       ├── favorites_service.py # 收藏夹同步
│   │       ├── media_service.py    # yt-dlp 音频下载
│   │       ├── asr_service.py      # DashScope 语音转写
│   │       ├── text_processing.py  # 文本清洗 + 切块
│   │       ├── chroma_service.py   # 向量库操作
│   │       ├── llm_service.py      # DeepSeek + DashScope Embedding
│   │       ├── rag_service.py      # RAG 问答引擎
│   │       ├── knowledge_service.py # 入库编排
│   │       └── worker.py           # 后台任务队列
│   ├── pyproject.toml
│   └── tests/
├── frontend/
│   └── src/
│       ├── App.tsx                 # 根组件
│       ├── api.ts                  # API 封装
│       ├── pages/                  # Landing + Workspace
│       └── components/             # LoginModal + SourcesPanel + ChatPanel
└── DEVELOPMENT_PLAN.md             # 完整开发方案
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/douyin/login/start` | 启动扫码登录 |
| GET  | `/api/auth/douyin/login/status` | 查询登录状态 |
| POST | `/api/auth/douyin/logout` | 退出登录 |
| POST | `/api/favorites/sync` | 同步收藏夹 |
| GET  | `/api/favorites/collections` | 收藏夹列表 |
| GET  | `/api/favorites/collections/{id}/videos` | 视频列表 |
| POST | `/api/knowledge/sync` | 选择视频或全部待入库视频入库 |
| GET  | `/api/knowledge/sync/{id}` | 入库进度 |
| GET  | `/api/knowledge/stats` | 知识库统计 |
| POST | `/api/chat/ask` | 非流式问答 |
| POST | `/api/chat/ask/stream` | SSE 流式问答 |
| GET  | `/api/chat/sessions` | 会话列表 |
| GET  | `/api/chat/sessions/{id}/messages` | 消息历史 |
| DELETE | `/api/chat/sessions/{id}` | 删除会话 |

## 费用说明

| 服务 | 计费方式 | 说明 |
|------|---------|------|
| 回答模型 | 按所选服务的计费规则 | 对话问题与参考原文会产生输入量 |
| 声音识别 | 按所选模型的计费规则 | 完整主音频与额外补听均可能计费 |
| 检索转换 | 按所选服务的计费规则 | 入库文本和查询文本均可能产生用量 |

价格与免费额度以服务商当前控制台、所选模型及区域为准。项目没有读取账单；模型请求次数减少不代表费用一定下降。

## License

MIT
