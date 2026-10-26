@echo off
chcp 65001 >nul
setlocal
set "TASK_ROOT=%~dp0"
if not exist "%TASK_ROOT%backend\.venv\Scripts\python.exe" (
  echo 未找到项目现有的 Python 环境：backend\.venv\Scripts\python.exe
  echo 请核对项目目录与已有环境。本入口不会下载、安装或修改环境。
  pause
  exit /b 1
)
pushd "%TASK_ROOT%backend"
if errorlevel 1 (
  echo 无法进入项目 backend 目录，请核对文件路径与目录权限。
  pause
  exit /b 1
)
echo DouyinMind 单次智能问答测试
echo 请保留此控制台，稍后按显示的问题和本次标记从 A 账号 @ B。
"%TASK_ROOT%backend\.venv\Scripts\python.exe" -m app.mention_agent.launcher
set "TASK_EXIT=%ERRORLEVEL%"
popd
echo.
if not "%TASK_EXIT%"=="0" echo 本次测试未正常完成，请查看上方提示；不会自动重启或重发。
echo 本次运行已结束。按任意键关闭本控制台。
pause
exit /b %TASK_EXIT%
