@echo off
cd /d "%~dp0GPT-SoVITS"
echo [TTS] Checking service status...
powershell -NoProfile -Command "try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:9880/health' -TimeoutSec 2; if($r.status -eq 'ok'){exit 0} } catch {}; exit 1"
if not errorlevel 1 (
  echo [TTS] Connected: service is already running at http://127.0.0.1:9880
  goto :end
)
echo [TTS] Loading models onto GPU. The first start may take a while...
..\.venv\Scripts\python.exe -u api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
:end
pause
