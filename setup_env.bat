@echo off
chcp 65001 >nul
rem ============================================================
rem  一键创建本项目 Python 虚拟环境（Windows）
rem  用法：双击运行，或命令行执行  setup_env.bat
rem
rem  说明：
rem    .venv 已加入 .gitignore —— 不要把它提交到 GitHub。
rem    venv 内部记录的是本机绝对路径，换机器/换目录会失效，
rem    而且它依赖创建它的那个基础 Python。
rem    正确做法：把  requirements.txt + setup_env.bat 提交到
rem    GitHub，别人克隆后在任意机器运行本脚本即可得到相同环境：
rem      python -m venv .venv
rem      .venv\Scripts\pip install -r requirements.txt
rem      .venv\Scripts\playwright install chromium   (交通荷载下载用)
rem ============================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python，请先安装 Python 3.10+ 并勾选 "Add to PATH"
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 创建虚拟环境 .venv ...
  python -m venv .venv
) else (
  echo [1/3] .venv 已存在，跳过创建
)

echo [2/3] 安装依赖 requirements.txt ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [错误] 依赖安装失败，请检查网络后重试
  pause
  exit /b 1
)

echo [3/3] 下载 playwright 浏览器内核（交通荷载下载用）...
".venv\Scripts\playwright.exe" install chromium
if errorlevel 1 (
  echo [提示] chromium 下载失败，可稍后手动执行：
  echo        .venv\Scripts\playwright install chromium
)

echo.
echo 环境准备完成。常用命令：
echo   .venv\Scripts\python.exe web/app.py           启动 web 管理台
echo   .venv\Scripts\python.exe preprocess\pipeline.py ...
echo.
pause
