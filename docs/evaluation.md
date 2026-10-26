# 检索与回答评测

用自己的已入库资料准备问题、正确视频、支持答案的原话和时间，再用同一套考卷比较改动前后。题库和实测结果保存在本地，仓库不附带个人题库。下方文件名是自行准备的本地输入和输出；执行真实评测前需要先完成标注。

## 先检查是否找到资料

这些工具主要评网页RAG（检索增强生成）流程，不验证抖音原评论、账号权限、一次发送或A侧送达。实验助手还需单独的受控验收，不能套用这里的分数。

`backend/scripts/evaluate_retrieval.py` 用已标注的问题、正确视频、原话和时间检查检索。评分区分“找到正确视频”“找到至少一条支持答案的原文”和“找到全部标注依据”；只找到标题不算找到答案依据。还会单独记录问题是否被分到只查标题等流程。

提问时，只有明确索要视频或标题清单（例如「有哪些视频」「关于 Git 的视频有哪些」）才只列标题。「有哪些原因」「列出操作步骤」「初始化后出现什么目录」以及列视频后还要求解释细节的提问，进入原文检索；不会因单独出现「哪些、清单、目录」就跳过正文。

题库和实测结果保存在本地 `backend/app/storage/retrieval_eval/`，该目录已被 Git 忽略，仓库不附带个人题库。请根据自己的已入库资料标注有答案题与无答案/范围隔离题；答案以可核实的原话为准，并回看原视频核对识别不清的内容。

在 `backend` 目录执行，先作不调用模型服务的本地题库与范围检查，再运行实测：

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

`evaluate_answers.py` 使用同一套题，按网页真实流程执行资料检索、原文上下文和流式回答。会调用真实模型服务；每题只记录一次生成，不反复挑选较好的答案。旧版本中的摘要字段和调用按实际记录保留，当前流程不再调用中间摘要；字段仍存在不代表又付费生成摘要。检索资料与数据库均使用临时副本，测试聊天不会写入用户历史；正确答案和评分依据不会传给回答模型。

```powershell
# 先在本地核对考卷、资料、模型与检索基线，不调用模型
.\.venv\Scripts\python.exe scripts/evaluate_answers.py --dataset app/storage/retrieval_eval/retrieval-exam-v1.json --reference app/storage/retrieval_eval/after-routing-v1.json --output app/storage/retrieval_eval/answers-validation-v1.json

# 真实生成；已有输出不会被覆盖，保留原始检索与实际模型输入输出
.\.venv\Scripts\python.exe scripts/evaluate_answers.py --dataset app/storage/retrieval_eval/retrieval-exam-v1.json --reference app/storage/retrieval_eval/after-routing-v1.json --output app/storage/retrieval_eval/answers-v1.json --run

# 逐题人工核对后，将评阅记录与一次生成的回答一起输出为报告
.\.venv\Scripts\python.exe scripts/render_answer_report.py --dataset app/storage/retrieval_eval/retrieval-exam-v1.json --answers app/storage/retrieval_eval/answers-v1.json --review app/storage/retrieval_eval/answer-review-v1.json --output app/storage/retrieval_eval/最终回答准确性与依据报告.html --csv app/storage/retrieval_eval/最终回答逐题核查.csv
```

人工评阅分别检查必要知识点是否覆盖、具体事实是否有原文依据、引用是否支持对应说法。合理同义解释及清楚区分的通用建议不当作捏造；核心点答到了但夹带无据产品能力、数字、文件路径等，仍不能视为完整有据。无答案题独立检查是否承认当前范围没有依据。报告基于本次生成与当前文字稿，不把一次测试的比例当作长期准确率。

修复后使用新的回答及评阅文件名，不覆盖旧样本。`render_answer_report.py` 可加 `--before-answers` 和 `--before-review` 生成前后逐题对照；只有题库、资料、索引及模型配置一致时才允许比较。源码变化单独展示，实际模型调用次数也按记录统计。

### 长视频测试流程（约 7–30 分钟）

本地测试目录可设为 `backend/app/storage/long_video_tests/my-test-run/`，测试数据和回听音频均被 Git 忽略。手动导入并确认准备好后再执行质量检查，评测命令不会定时自动运行。

新处理任务会在 `app/storage/ingestion_runs/视频编号/` 保存每次尝试的下载/音轨提取、分段转写、整理入库与总处理时间。失败和重试分别记录，总时间不含排队。视频详情可展开“本次处理耗时”；以前的视频若没有计时，不用数据库创建时间或文件时间倒算。回答评测另外记录首段文字出现和完整回答耗时。

若某批视频已在旧服务开始处理，不能为它补造精确计时。`observe_ingestion.py` 可以保存轮询观察记录，需标明误差和未知的阶段起点；它不做内容评测或重启服务。测试期间可设置 `LONG_VIDEO_TEST_KEEP_AUDIO=true`，约6–31分钟音频会为回听保留，覆盖7–30分钟边界，测试结束可关闭。旧批次若WAV已清理，可从保留的本地原视频提取音轨，不自动下载或重新识别。

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


[返回首页](../README.md)
