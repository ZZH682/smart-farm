@echo off
chcp 65001 >nul
title SmartFarm ESP32 编译烧录工具
setlocal enabledelayedexpansion

set CLI=C:\Users\sbguo\arduino-cli\arduino-cli.exe
set SKETCH=C:\Users\sbguo\Desktop\smart_farm 2.0\esp32_firmware\esp32_greenhouse_node
set FQBN=esp32:esp32:esp32

echo ========================================
echo   SmartFarm ESP32 编译烧录工具
echo ========================================
echo.

REM 检测串口
echo [1/3] 检测 ESP32 串口...
set PORT=
for /f "tokens=2 delims=:" %%a in ('"%CLI% board list" 2^>nul ^| findstr /i "USB Serial"') do (
    set PORT=%%a
    goto :found
)
:found
if "%PORT%"=="" (
    echo   未自动检测到 ESP32，请输入串口号（例如 COM3）:
    set /p PORT=
) else (
    echo   检测到串口: %PORT%
)
echo.

REM 编译
echo [2/3] 编译固件...
"%CLI%" compile --fqbn %FQBN% "%SKETCH%"
if errorlevel 1 (
    echo.
    echo [错误] 编译失败，请检查代码！
    pause
    exit /b 1
)
echo   编译成功！
echo.

REM 烧录
echo [3/3] 烧录到 ESP32 (%PORT%)...
echo   按住 ESP32 的 BOOT 按钮，直到开始写入...
"%CLI%" upload -p %PORT% --fqbn %FQBN% "%SKETCH%"
if errorlevel 1 (
    echo.
    echo [错误] 烧录失败！
    echo 可能原因：
    echo   1. 串口被占用（关闭串口监视器）
    echo   2. 未按 BOOT 按钮进入下载模式
    echo   3. 串口选择错误
    pause
    exit /b 1
)
echo.
echo ========================================
echo   烧录成功！ESP32 即将重启运行
echo ========================================
echo.
echo 提示：可以用串口监视器查看运行日志（波特率 115200）
pause
