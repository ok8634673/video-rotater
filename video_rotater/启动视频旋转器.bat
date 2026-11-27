@echo off
chcp 936 >nul
cd /d "%~dp0"
set "APP=%~dp0video_rotate_gui.py"

where pyw >nul 2>&1 && goto use_pyw
where pythonw >nul 2>&1 && goto use_pythonw
where python >nul 2>&1 && goto use_python
goto no_python

:use_pyw
start "" pyw -3 "%APP%"
exit /b

:use_pythonw
start "" pythonw "%APP%"
exit /b

:use_python
start "" python "%APP%"
exit /b

:no_python
echo.
echo   未找到 Python，请先安装 Python 3.8 或更高版本：https://www.python.org/downloads/
echo   安装时请勾选 "tcl/tk and IDLE"，否则界面无法显示。
echo.
pause
