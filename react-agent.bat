@echo off
REM react-agent 一键启动：激活 .venv 并进入 REPL
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python main.py %*
