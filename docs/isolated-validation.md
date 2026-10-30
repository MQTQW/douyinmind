# 便携隔离测试工具

先按照项目安装说明，在目标电脑准备 Python 3.12 环境与测试依赖（包括 pytest）。工具使用**当前解释器**的依赖目录，不携带旧电脑的虚拟环境，也不会联网安装依赖。

把 `portable_guarded_tests.py` 放到项目的 `tools/`，或放在项目之外。不要放在项目的 `backend/app/storage/` 中。

在项目根目录的 PowerShell 中执行：

```powershell
& .\backend\.venv\Scripts\python.exe .\tools\portable_guarded_tests.py `
  --source . `
  --tests test_evidence_focus.py `
  --label first-check `
  --output ..\douyinmind-validation-reports
```

`--tests` 接受精确文件与节点，可写多个，例如 `test_workflow_core.py test_workflow_review_probes.py` 或 `test_file.py::test_name`。不接受目录、通配符或项目之外的路径。

`--output` 必须在被测项目目录之外。每次运行会创建独立报告目录；临时代码副本和失败证据均保留，不自动删除。

## 实际隔离范围

- 只复制 `backend/app/` 和 `backend/tests/` 中的公开 `.py`，包含新开发文件；不复制 `.env`、storage、索引、音视频、登录资料或虚拟环境。
- 在临时副本中运行所选 pytest；清除敏感环境变量，关闭插件自动加载，将数据库、缓存和测试写入位置指向独立临时目录。
- Python audit hook 拒绝网络、外部进程、原生进程创建、真实源文件写入、真实数据库连接、文件链接及测试阶段的新 FFI 加载/解析/调用。
- 严格测试 hook 前，仅初始化当前 CPython 标准库 ctypes。Windows 仅允许该标准库初始化的 kernel32 / GetLastError；POSIX 仅允许当前进程句柄。不会加载或调用 DPAPI，不导入产品模块。
- 每轮执行原始 7 项负向金丝雀及额外 5 项 FFI/原生进程金丝雀，失败即不能判验证通过。
- 为无色文件输出，禁用 pytest 可选 Windows colorama 附着及可选 `current` 链接。不会修改产品或测试代码来绕过失败。

这是**可信合成测试的 Python 审计防护**，不是执行恶意代码的操作系统沙箱。不适用于直接执行生产命令、真实模型、真实入库、评论发送或登录核验。

## 读取结果

查看每次运行的 `report.json`、`terminal.txt` 与前后哈希清单：

- `snapshot_tests_passed=true`：冻结副本的测试与防护检查通过。
- `validation_passed=true`：除上述条件外，被测源码与 runner 在运行期间也没有改变。
- 并行开发导致源码变动时，不可把快照通过称为最新源码验证通过。
- `guard_denials` 非空、金丝雀失败、测试失败或无法确认用量/结果时，保留原证据；先区分业务问题与隔离环境限制，不关闭防护强行跑绿。

已在 Windows CPython 3.12 对当前合成 `test_evidence_focus.py` 验证。其他操作系统或 CPython 版本的 bootstrap 行为需要单独验证；不匹配时会保守失败。
