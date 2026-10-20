# 结构与检索规则

## 技术分工

| 组件 | 用途 |
| --- | --- |
| React 19、TypeScript、Vite、Tailwind CSS | 网页界面、类型检查、开发构建与样式 |
| FastAPI | 后台开发工具，接收请求并返回视频、知识库或回答 |
| SQLAlchemy、SQLite | 管理本地数据表，保存视频归类和对话 |
| ChromaDB | 保存文字的数字表示，用于按含义查找原文 |
| Qwen Omni、DeepSeek | 分别负责声音识别和依据资料生成回答 |
| Playwright、ffmpeg | 浏览器登录与收藏读取、音轨提取和分段 |

音频默认使用 `qwen3.8-omni-flash`，回答默认使用 `deepseek-flash`，数字表示默认使用 `text-embedding-v4`。这些是配置中的模型标识，实际可用名称、区域和服务权限以自己的账号为准。

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
| [frontend/src/pages/Workspace.tsx](../frontend/src/pages/Workspace.tsx) | 主工作台 |
| [frontend/src/components](../frontend/src/components) | 对话、知识库、导入、选择器及视频详情 |
| [backend/scripts](../backend/scripts) | 可复用的检索、回答和长视频评测工具 |

## 问题先分流

只有明确要求视频或标题清单时才只列标题。「有哪些原因」「列出步骤」「初始化后出现什么目录」以及列视频后还要求解释细节的问题，会查正文。纯问候可以直接交流；带有内容问题的问候仍查资料。

明确追问在查找前加入最近最多 3 条用户问题，最多 1000 个字符。旧的助手回答不进入这一步，避免把先前猜测当成事实。事实依据仍只能来自本次资料；模糊指代不保证自动识别。

## 从候选原文到回答

默认最多取 32 条语义候选，与当前范围的正文关键词结果融合排序，排除仅有标题和作者的记录。选择时尽量覆盖不同子问题并减少重复，最终最多提供 8 个正文片段、总计 8000 个字符。

原文保留时间和声音或画面类型，不先让模型生成摘要替代证据。句子被文字块截断时，只从同视频、同来源且有可靠重叠的相邻块补齐，额外最多 350 字符。放不下的超长候选不阻止继续检查后面较短的候选；必要截取在完整句或行边界结束并标记。

8000 个字符是提供给回答模型的原文预算，不是完整文字稿的存储长度，也不是答案字数。长视频或分散的多项问题仍可能漏掉必要资料，应通过带原话的考卷检查。

来源根据答案实际标注和本次正文白名单返回。未引用时不会把所有候选自动追加为来源；历史记录保存同样的已引用来源。用户可以展开本次提供的原文，但旧历史没有保存的片段不会凭空补造。

## 临时故障和时间

问题转数字的请求使用 `QUERY_EMBEDDING_TIMEOUT_SECONDS=5`，查询不额外重试。明确的临时网络故障可进入关键词后备，界面会说明；密钥权限、配置或本地索引错误仍然报错。这是网络阶段超时，不是完整回答的时间保证。

查找过程区分理解问题、语义查找、读取原文、合并关键词和相邻句补全耗时。细分耗时已包含在所属阶段，不重复相加；候选数和最终片段数分别记录。错误使用固定提示与错误码，不将第三方原始异常直接显示到聊天页面。

## 开发检查

```powershell
# 在 frontend 目录
npm ci
npm test
npm run build
```

```powershell
# 在 backend 目录，安装测试依赖后执行
uv sync --python 3.12 --extra dev
uv run python -m pytest
```

单元测试使用模拟服务或临时数据；主动执行带 `--run` 的真实评测会调用配置的模型服务。两者的结果和费用口径应分开。

启动后访问 [后台接口说明](http://127.0.0.1:8000/docs) 查看当前接口，包括知识库管理、已保存视频分页、处理状态、导出及流式问答。

## 当前边界

项目主要面向本地个人使用，没有完整的多人权限体系。单个后台的任务保护不等于多个后台实例之间的统一事务；资料库规模、模型服务稳定性和识别质量也会影响效果。向公网开放或改成多用户服务前，应单独设计这些能力。

[返回首页](../README.md)
