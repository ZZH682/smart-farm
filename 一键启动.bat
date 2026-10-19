@echo off
chcp 936 >nul
title 智慧农业大棚监控系统 v2.0 - 一键启动
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo  ==================================================
echo    智慧农业大棚监控系统 v2.0   一键启动
echo    (病虫害识别 + 产量预测 + 趋势分析 + 报表 + 温度预测)
echo  ==================================================
echo.

REM ============ 1. 定位 Python 3.10 ============
set "PY="
if exist "C:\Users\sbguo\AppData\Local\Programs\Python\Python310\python.exe" (
    set "PY=C:\Users\sbguo\AppData\Local\Programs\Python\Python310\python.exe"
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%i in ('py -3.10 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
    )
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo  [错误] 未找到 Python 3.10，请先安装 Python 后重试。
    pause
    exit /b 1
)
echo  [1/5] Python 解释器: %PY%
"%PY%" --version
echo.

REM ============ 2. 检查并安装依赖 ============
echo  [2/5] 检查项目依赖...
numpy-check
"%PY%" -c "import paho.mqtt, flask, flask_socketio, PIL, numpy" >nul 2>nul
if errorlevel 1 (
    echo   检测到依赖缺失，正在自动安装（需要联网，请稍候）...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo  [错误] 依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)
echo   依赖就绪。
echo.

REM ============ 3. 创建必要目录 ============
echo  [3/5] 初始化目录结构...
if not exist "models" mkdir models
if not exist "uploads\pest" mkdir uploads\pest
echo   models/ 和 uploads/pest/ 目录已就绪。
echo.



REM ============ 3.5 检查并启动 MQTT Broker (Mosquitto) ============

echo  [3.5/5] 检查 MQTT Broker...

netstat -ano | findstr ":1883" | findstr "LISTENING" >nul

if errorlevel 1 (

    if exist "C:\Program Files\mosquitto\mosquitto.exe" (

        echo   MQTT Broker 未运行，正在启动 Mosquitto...

        start "Mosquitto" "C:\Program Files\mosquitto\mosquitto.exe" -c "C:\Users\sbguo\mosquitto_smartfarm.conf"

        timeout /t 3 /nobreak >nul

        netstat -ano | findstr ":1883" | findstr "LISTENING" >nul

        if not errorlevel 1 (

            echo   Mosquitto 启动成功 (监听 0.0.0.0:1883)

        ) else (

            echo   [警告] Mosquitto 启动失败，请手动检查

        )

    ) else (

        echo   [警告] 未找到 Mosquitto，MQTT 功能不可用

    )

) else (

    echo   MQTT Broker 已在运行

)

echo.

REM ============ 4. 检查端口 5000 占用情况 ============
echo  [4/5] 检查端口 5000 ...
netstat -ano | findstr ":5000" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo   检测到端口 5000 已被占用（可能是系统实例正在运行）。
    echo   为避免误杀无关进程，本脚本不再自动结束进程，
    echo   请手动关闭占用 5000 端口的程序后重试。
    echo   当前占用情况：
    netstat -ano | findstr ":5000" | findstr "LISTENING"
    pause
    exit /b 1
)
echo.

REM ============ 5. 启动系统并打开浏览器 ============
echo  [5/5] 启动系统...
start "SmartFarm" "%PY%" main.py
echo   正在等待 Web 服务启动...
timeout /t 6 /nobreak >nul
start "" "http://localhost:5000"

echo.
echo  ==================================================
echo    系统已启动！
echo    监控面板 : http://localhost:5000
echo    新功能   : 病虫害识别 / 产量预测 / 趋势分析 / 报表 / 温度预测
echo    停止方式 : 在 "SmartFarm" 窗口按 Ctrl+C
echo    本窗口现在可以关闭。
echo  ==================================================
timeout /t 6 >nul
exit /b 0
