@echo off
call D:\software\Miniconda3\condabin\conda.bat activate mcp
if errorlevel 1 (
  echo Failed to activate conda environment: mcp
  pause
  exit /b 1
)

cd /d D:\mcp\mcp_alint
chainlit run chat_app.py

echo.
echo Chainlit exited.
pause
