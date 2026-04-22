@echo off
call D:\software\Miniconda3\condabin\conda.bat activate mcp
if errorlevel 1 (
  echo Failed to activate conda environment: mcp
  pause
  exit /b 1
)

cd /d %~dp0..
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
langgraph dev --config langgraph_server\langgraph.json --no-browser --allow-blocking --host 127.0.0.1 --port 2024

echo.
echo LangGraph Agent Server exited.
pause
