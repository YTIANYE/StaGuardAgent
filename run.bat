@echo off
rem ===========================================================================
rem  StaGuardAgent - Windows launcher
rem
rem  Windows 默认没有 make，这个脚本把常用操作包了一层，等价于 Makefile 的目标。
rem  用法：run.bat [子命令]，子命令见 run.bat help；不带参数 = 三步跑通
rem  （建环境装依赖 -> 生成数据集 -> 巡检 S1）。
rem
rem  两条实现约定：
rem    1. 开头把控制台切到 UTF-8，否则 rich 输出的中文与图标会显示成方块；
rem    2. 一律直接调用 .venv\Scripts\python.exe，既不需要 Activate，
rem       也不受 PowerShell 执行策略（ExecutionPolicy）限制。
rem ===========================================================================
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "VENV=.venv"
set "PY=%VENV%\Scripts\python.exe"

set "CMD=%~1"
if "%CMD%"=="" set "CMD=all"

if /i "%CMD%"=="all"      goto :all
if /i "%CMD%"=="install"  goto :install
if /i "%CMD%"=="data"     goto :data
if /i "%CMD%"=="inspect"  goto :inspect
if /i "%CMD%"=="test"     goto :test
if /i "%CMD%"=="eval"     goto :eval
if /i "%CMD%"=="sample"   goto :sample
if /i "%CMD%"=="monitor"  goto :monitor
if /i "%CMD%"=="serve"    goto :serve
if /i "%CMD%"=="schedule" goto :schedule
if /i "%CMD%"=="clean"    goto :clean
if /i "%CMD%"=="help"     goto :usage

echo 未知子命令："%CMD%"。可用命令见 run.bat help
exit /b 2

rem ------------------------------------------------------------------ 三步跑通
:all
call :install
if errorlevel 1 exit /b 1
call :data
if errorlevel 1 exit /b 1
call :inspect S1
exit /b %errorlevel%

rem ------------------------------------------------------------------ 命令
:install
if exist "%PY%" exit /b 0
set "BASEPY=python"
where python >nul 2>nul || set "BASEPY=py -3"
echo [1/2] 创建虚拟环境 %VENV% ...
%BASEPY% -m venv "%VENV%"
if errorlevel 1 (
    echo [错误] 创建虚拟环境失败：请确认已安装 Python 3.11+ 并勾选 Add python.exe to PATH
    exit /b 1
)
echo [2/2] 安装依赖 ...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -e ".[dev]"
if errorlevel 1 (
    echo [错误] 依赖安装失败
    exit /b 1
)
exit /b 0

:data
call :install
if errorlevel 1 exit /b 1
echo 生成模拟数据集：14 天归档 + 7 个故障场景（约 15 秒）...
"%PY%" -m staguard gen-data
exit /b %errorlevel%

:inspect
call :install
if errorlevel 1 exit /b 1
set "SCENARIO=%~2"
if "%SCENARIO%"=="" set "SCENARIO=S1"
set "SOURCE=%~3"
if "%SOURCE%"=="" (
    "%PY%" -m staguard run --scenario %SCENARIO%
) else (
    "%PY%" -m staguard run --scenario %SCENARIO% --source %SOURCE%
)
exit /b %errorlevel%

:test
call :install
if errorlevel 1 exit /b 1
"%PY%" -m pytest tests/ -q
exit /b %errorlevel%

:eval
call :install
if errorlevel 1 exit /b 1
"%PY%" -m staguard eval
exit /b %errorlevel%

:sample
call :install
if errorlevel 1 exit /b 1
"%PY%" -m staguard sample
exit /b %errorlevel%

:monitor
call :install
if errorlevel 1 exit /b 1
"%PY%" -m staguard mock-monitor --port 8090
exit /b %errorlevel%

:serve
call :install
if errorlevel 1 exit /b 1
"%PY%" -m staguard serve
exit /b %errorlevel%

:schedule
call :install
if errorlevel 1 exit /b 1
"%PY%" -m staguard schedule --interval 30
exit /b %errorlevel%

:clean
if exist "data\staguard.db"     del /f /q "data\staguard.db"
if exist "data\staguard.db-wal" del /f /q "data\staguard.db-wal"
if exist "data\staguard.db-shm" del /f /q "data\staguard.db-shm"
if exist "data\metrics"         rmdir /s /q "data\metrics"
if exist "reports"              rmdir /s /q "reports"
if exist "logs"                 rmdir /s /q "logs"
if exist ".pytest_cache"        rmdir /s /q ".pytest_cache"
if exist ".ruff_cache"          rmdir /s /q ".ruff_cache"
for /d /r . %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"
echo 已清理生成物（虚拟环境 %VENV% 保留）
exit /b 0

rem ------------------------------------------------------------------ 帮助
:usage
echo StaGuardAgent Windows 启动脚本（等价于 Makefile 的常用目标）
echo.
echo   run.bat              三步跑通：建环境装依赖 + 生成数据集 + 巡检 S1
echo   run.bat install      只建环境装依赖
echo   run.bat data         只生成数据集
echo   run.bat inspect S1   巡检指定场景（默认 S1），第二个参数可给 file 或 http
echo   run.bat test         全量测试
echo   run.bat eval         归因评测
echo   run.bat sample       导出报告样例到 docs\samples
echo   run.bat monitor      启动模拟监控接口（常驻，Ctrl+C 停止）
echo   run.bat serve        启动巡检 API（常驻）
echo   run.bat schedule     常驻定时巡检
echo   run.bat clean        清理生成物
echo.
echo 前置：Python 3.11+，安装时勾选 Add python.exe to PATH。
echo 配大模型 key：copy .env.example .env，填入 STAGUARD_LLM_API_KEY。
exit /b 0
