@echo off
rem Same as start-swarm.bat but for 300 agents. Use it only after the 200-agent report is clean.
rem Stop the agents first (stop-swarm.bat). Existing agents keep their state; 100 new ones are created (with --stagger so the game is not hit all at once).
cd /d "%~dp0"
set COUNT=300
set MODEL=qwen/qwen2.5-coder-14b
set VOICES=20

if exist STOP del STOP

start "Board" cmd /k python board.py
timeout /t 3 /nobreak >nul
start "Observatory" cmd /k python observatory.py --port 18766 --board http://localhost:8765
timeout /t 3 /nobreak >nul
start "Agents" cmd /k python run_swarm.py --count %COUNT% --board http://localhost:8765 --llm http://localhost:1234/v1 --llm-model %MODEL% --llm-voices %VOICES% --llm-max-per-hour 500 --max-rate 100 --stagger 0.5
timeout /t 8 /nobreak >nul
start http://localhost:18766/llm
start http://localhost:8765/
echo Started 300 agents. To stop the agents, run stop-swarm.bat
