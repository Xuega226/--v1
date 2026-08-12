@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "DOCKER_DESKTOP=D:\UnnamekoRuntime\DockerDesktop\Docker Desktop.exe"
set "DOCKER=D:\UnnamekoRuntime\DockerDesktop\resources\bin\docker.exe"
if exist "%DOCKER%" goto docker_found
set "DOCKER_DESKTOP=%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe"
set "DOCKER=%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin\docker.exe"
if exist "%DOCKER%" goto docker_found
where docker >nul 2>nul
if not errorlevel 1 (
  set "DOCKER=docker"
  goto docker_found
)
if not exist "%DOCKER%" (
  echo [ERROR] 没有找到 Docker Desktop，请先安装或启动 Docker Desktop。
  if not defined UNNAMEKO_NO_PAUSE pause
  exit /b 1
)

:docker_found
"%DOCKER%" info >nul 2>nul
if errorlevel 1 (
  echo 正在启动 Docker Desktop...
  start "" /min "%DOCKER_DESKTOP%"
  for /l %%i in (1,1,60) do (
    timeout /t 2 /nobreak >nul
    "%DOCKER%" info >nul 2>nul && goto docker_ready
  )
  echo [ERROR] Docker Desktop 启动超时。
  if not defined UNNAMEKO_NO_PAUSE pause
  exit /b 1
)

:docker_ready
"%DOCKER%" compose -f infra\vision\compose.yaml up -d
if errorlevel 1 goto failed

set "OLLAMA=D:\UnnamekoRuntime\Ollama\ollama.exe"
if exist "%OLLAMA%" goto ollama_found
set "OLLAMA=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if exist "%OLLAMA%" goto ollama_found
where ollama >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 没有找到 Ollama，请先安装 Ollama。
  if not defined UNNAMEKO_NO_PAUSE pause
  exit /b 1
)
set "OLLAMA=ollama"

:ollama_found
"%OLLAMA%" list >nul 2>nul
if errorlevel 1 (
  echo 正在启动 Ollama...
  start "" /min "%OLLAMA%" serve
  for /l %%i in (1,1,30) do (
    timeout /t 2 /nobreak >nul
    "%OLLAMA%" list >nul 2>nul && goto ollama_ready
  )
  echo [ERROR] Ollama 启动超时。
  if not defined UNNAMEKO_NO_PAUSE pause
  exit /b 1
)

:ollama_ready
"%OLLAMA%" list | findstr /i "qwen3-vl:2b" >nul
if errorlevel 1 (
  echo 首次下载 qwen3-vl:2b，约 1.9 GB...
  "%OLLAMA%" pull qwen3-vl:2b
  if errorlevel 1 goto failed
)

echo [OK] 图片识别服务已就绪。
if not defined UNNAMEKO_NO_PAUSE pause
exit /b 0

:failed
echo [ERROR] 图片识别服务启动失败，请查看上方错误。
if not defined UNNAMEKO_NO_PAUSE pause
exit /b 1
