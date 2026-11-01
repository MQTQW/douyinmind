@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0backend"
echo ============================================
echo  可见窗口登录助手 B（一次性本机登录）
echo  若助手正在运行，会先询问是否自动停止助手。
echo  登录完成后回到本窗口按 Enter 关闭。
echo ============================================
".venv\Scripts\python.exe" -m app.assistant.login_window
echo.
echo 登录窗口已结束。若浏览器中登录成功，请回项目网页点「开启助手」。
pause
