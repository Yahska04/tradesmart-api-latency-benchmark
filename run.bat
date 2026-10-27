@echo off
REM Double-click to start the TradeSmart API Latency Benchmark dashboard.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found. Run:  python -m venv .venv  and  .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)
echo Starting dashboard at http://localhost:8501  (press Ctrl+C to stop)
start "" http://localhost:8501
".venv\Scripts\python.exe" -m streamlit run app.py --server.port 8501
pause
