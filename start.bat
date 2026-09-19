@echo off
REM ==========================================================================
REM  react-agent 一键启动脚本 (Windows)
REM
REM   双击本文件           启动 Web 对话界面（后端 8000 同时托管前端页面）
REM   start.bat dev        开发模式：后端 8000 + Vite 热更新 5173
REM   start.bat cli        命令行 REPL（等同原来的 react-agent.bat）
REM   start.bat build      仅构建前端产物 web\dist
REM   start.bat stop       停止后台的后端 / 前端服务
REM   start.bat help       查看帮助
REM
REM  设计说明：
REM   - 就绪探测用 netstat 判断端口是否 LISTENING，不用 curl
REM     （本机若设了 http_proxy，curl 对 127.0.0.1 也会返回 502，探测会失真）
REM   - 后端优先独立窗口启动；若受限环境不允许开新窗口，自动回退到本窗口后台启动
REM ==========================================================================
REM 注意：本文件必须以 GBK(936) 编码保存，且不要在脚本内切换代码页。
REM cmd.exe 按字节偏移解析批处理，chcp 切换后重新解析会把中文行劈碎。
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title react-agent launcher

set "PORT=8000"
set "DEV_PORT=5173"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=web"

REM 本机若设置了代理，访问 127.0.0.1 时必须绕开
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"

REM ---- 配置档案：默认 config.deepseek.json ----
REM 想切回默认档案：把下一行的文件名改成 config.json
REM 想临时指定：先 set REACT_AGENT_CONFIG=config.xxx.json 再运行本脚本
if "%REACT_AGENT_CONFIG%"=="" set "REACT_AGENT_CONFIG=%~dp0config.deepseek.json"
set "CFG=%REACT_AGENT_CONFIG%"

if /i "%MODE%"=="help"   goto :usage
if /i "%MODE%"=="-h"     goto :usage
if /i "%MODE%"=="--help" goto :usage
if /i "%MODE%"=="/?"     goto :usage
if /i "%MODE%"=="stop"   goto :stop
if /i "%MODE%"=="cli"    goto :cli
if /i "%MODE%"=="build"  goto :build_only
if /i "%MODE%"=="dev"    goto :dev
if /i "%MODE%"=="web"    goto :web

echo [x] 未知参数：%MODE%
goto :usage


REM ==========================================================================
REM 生产模式：构建前端（缺失时）+ 启动后端 + 打开浏览器
REM ==========================================================================
:web
echo ==========================================================================
echo  react-agent  Web 对话界面
echo ==========================================================================
call :check_python
if errorlevel 1 goto :fail
call :check_config
if errorlevel 1 goto :fail
call :ensure_dist
call :start_backend
if errorlevel 1 goto :fail
call :open_url "http://127.0.0.1:%PORT%"
goto :summary


REM ==========================================================================
REM 开发模式：后端 + Vite dev server（改前端代码即时热更新）
REM ==========================================================================
:dev
echo ==========================================================================
echo  react-agent  开发模式（前端热更新）
echo ==========================================================================
call :check_python
if errorlevel 1 goto :fail
call :check_config
if errorlevel 1 goto :fail
where npm >nul 2>nul
if errorlevel 1 (
    echo [x] 未检测到 npm，开发模式需要 Node.js（版本 18 及以上）。
    echo     请安装后重试： https://nodejs.org/
    goto :fail
)
call :start_backend
if errorlevel 1 goto :fail
if not exist "web\node_modules" (
    echo [i] 首次运行，安装前端依赖 npm install ...
    pushd web
    call npm install
    if errorlevel 1 ( popd & echo [x] npm install 失败 & goto :fail )
    popd
)
echo [i] 启动 Vite 开发服务器：http://127.0.0.1:%DEV_PORT%
start "react-agent frontend(dev)" /D "%CD%\web" cmd /k "npm run dev"
call :wait_port %DEV_PORT% 12
if "%READY%"=="0" (
    echo [w] 无法新建窗口，改为在本窗口后台启动 Vite
    start "" /D "%CD%\web" /B cmd /c npm run dev
    call :wait_port %DEV_PORT% 30
)
if "%READY%"=="0" (
    echo [w] Vite 未在预期时间内监听 %DEV_PORT%，请查看上方日志确认实际端口
)
call :open_url "http://127.0.0.1:%DEV_PORT%"
goto :summary_dev


REM ==========================================================================
REM 命令行 REPL
REM ==========================================================================
:cli
call :check_python
if errorlevel 1 goto :fail
call :check_config
if errorlevel 1 goto :fail
shift
"%PY%" main.py %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :done


REM ==========================================================================
REM 仅构建前端
REM ==========================================================================
:build_only
call :npm_build
if errorlevel 1 goto :fail
goto :done


REM ==========================================================================
REM 停止服务
REM ==========================================================================
:stop
echo [i] 停止 %PORT% / %DEV_PORT% 端口上的服务 ...
call :kill_port %PORT%
call :kill_port %DEV_PORT%
taskkill /FI "WINDOWTITLE eq react-agent backend*"      /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq react-agent frontend(dev)*" /F >nul 2>&1
echo [ok] 已停止
goto :done


REM ==========================================================================
REM 子过程
REM ==========================================================================

:check_python
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    echo [ok] Python：.venv\Scripts\python.exe
    exit /b 0
)
where uv >nul 2>nul
if not errorlevel 1 (
    echo [i] 未找到 .venv，正在执行 uv sync ...
    call uv sync
    if exist ".venv\Scripts\python.exe" (
        set "PY=.venv\Scripts\python.exe"
        echo [ok] 依赖安装完成
        exit /b 0
    )
)
where python >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    echo [w] 未使用 .venv，回退到系统 python（建议先执行 uv sync）
    exit /b 0
)
echo [x] 找不到 Python，也找不到 uv。请安装后重试，或手动执行： uv sync
exit /b 1


:check_config
if not exist "%CFG%" (
    echo [i] 缺少 %CFG%，正在从 config.example.json 复制 ...
    copy /y "config.example.json" "%CFG%" >nul
    echo [x] 已生成 %CFG%，请用记事本打开填入 base_url / api_key 后重新运行。
    exit /b 1
)
"%PY%" -c "import json,sys;d=json.load(open(r'%CFG%',encoding='utf-8'));v=str(d.get('api_key',''))+str(d.get('base_url',''));sys.exit(0 if v.strip() and chr(60) not in v else 1)" >nul 2>&1
if errorlevel 1 (
    echo [x] %CFG% 中的 api_key / base_url 缺失或仍是占位符。
    echo     请编辑 %CFG% 填入真实值后重新运行。
    exit /b 1
)
echo [ok] 配置：%CFG%
exit /b 0


:ensure_dist
if exist "web\dist\index.html" (
    echo [ok] 前端产物：web\dist（已存在，跳过构建）
    exit /b 0
)
echo [i] 未找到 web\dist，开始构建前端 ...
call :npm_build
exit /b %errorlevel%


:npm_build
where npm >nul 2>nul
if errorlevel 1 (
    echo [x] 未检测到 npm，无法构建前端。
    echo     方案 A：安装 Node.js 后重新运行本脚本
    echo     方案 B：直接用命令行 REPL： start.bat cli
    exit /b 1
)
pushd web
if not exist "node_modules" (
    echo [i] 安装前端依赖 npm install（首次约需 1-3 分钟）...
    call npm install
    if errorlevel 1 ( popd & echo [x] npm install 失败 & exit /b 1 )
)
echo [i] 构建前端 npm run build ...
call npm run build
set "RC=%errorlevel%"
popd
if not "%RC%"=="0" (
    echo [x] 前端构建失败，请查看上方 npm 报错。
    exit /b 1
)
echo [ok] 前端构建完成：web\dist
exit /b 0


:start_backend
call :port_in_use %PORT%
if "%IN_USE%"=="1" (
    echo [i] 端口 %PORT% 已在监听，复用已有的后端实例
    exit /b 0
)
echo [i] 启动后端：http://127.0.0.1:%PORT%
start "react-agent backend" /D "%CD%" cmd /k "%PY% -m react.webapi %PORT%"
call :wait_port %PORT% 12
if "%READY%"=="1" (
    echo [ok] 后端已就绪（独立窗口运行）
    exit /b 0
)
echo [w] 无法新建独立窗口（受限环境常见），改为在本窗口后台启动
start "" /B "%PY%" -m react.webapi %PORT%
call :wait_port %PORT% 30
if "%READY%"=="0" (
    echo [x] 后端未能启动。请查看上方报错，或手动执行：
    echo     %PY% -m react.webapi %PORT%
    exit /b 1
)
echo [ok] 后端已就绪（本窗口后台运行，关闭本窗口即停止服务）
exit /b 0


:wait_port
REM %1 = 端口，%2 = 最多等待秒数；返回 READY=0/1
set "READY=0"
for /L %%i in (1,1,%2) do (
    if "!READY!"=="0" (
        call :port_in_use %1
        if "!IN_USE!"=="1" (
            set "READY=1"
        ) else (
            ping -n 2 127.0.0.1 >nul 2>&1
        )
    )
)
exit /b 0


:port_in_use
REM %1 = 端口；返回 IN_USE=0/1
set "IN_USE=0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%1 " ^| findstr /i "LISTENING"') do (
    if not "%%a"=="" set "IN_USE=1"
)
exit /b 0


:kill_port
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%1 " ^| findstr /i "LISTENING"') do (
    if not "%%a"=="" (
        echo     结束端口 %1 上的进程 PID=%%a
        taskkill /F /PID %%a >nul 2>&1
    )
)
exit /b 0


:open_url
echo [i] 打开浏览器：%~1
start "" "%~1"
exit /b 0


REM ==========================================================================
REM 收尾
REM ==========================================================================
:summary
echo.
echo ==========================================================================
echo  已启动
echo    对话界面： http://127.0.0.1:%PORT%
echo    配置档案： %CFG%
echo    停止服务： start.bat stop
echo    开发模式： start.bat dev
echo  说明：后端日志在 react-agent backend 窗口；若该窗口未能创建，
echo        后端会退回到本窗口后台运行，关闭本窗口即停止服务。
echo ==========================================================================
goto :done

:summary_dev
echo.
echo ==========================================================================
echo  已启动（开发模式）
echo    前端（热更新）： http://127.0.0.1:%DEV_PORT%
echo    后端 API：      http://127.0.0.1:%PORT%
echo    停止服务： start.bat stop
echo ==========================================================================
goto :done

:usage
echo.
echo  react-agent 一键启动脚本
echo.
echo  用法：
echo    start.bat          启动 Web 对话界面（推荐，双击即可）
echo    start.bat dev      开发模式，前端 5173 热更新 + 后端 8000
echo    start.bat cli      命令行 REPL
echo    start.bat build    仅构建前端产物 web\dist
echo    start.bat stop     停止后台服务
echo    start.bat help     显示本帮助
echo.
echo  当前配置档案：%CFG%（用 REACT_AGENT_CONFIG 切换，默认 config.deepseek.json）
echo.
goto :done

:fail
echo.
echo [x] 启动失败，请按上方提示处理后重新运行。
goto :done

:done
echo.
echo 按任意键关闭本窗口...
pause >nul
endlocal
