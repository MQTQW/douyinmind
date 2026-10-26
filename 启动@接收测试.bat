@echo off
chcp 65001 >nul
cd /d "%~dp0backend"
echo DouyinMind 评论区 @ 接收原型
echo 只接收预览，不识别、不入库、不发送回复。
echo 请在独立助手浏览器手动登录 B。关闭浏览器可停止测试。
".venv\Scripts\python.exe" -m app.mention_receiver --duration-minutes 60
if errorlevel 1 (
  echo 接收原型未能启动，请查看本地接收测试台。
  pause
)
