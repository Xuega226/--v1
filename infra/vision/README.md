# QQ 图片识别服务

图片识别分为两个互相独立的本地服务：

- PaddleOCR 在 Docker 的 CPU 容器中提取文字，监听 `127.0.0.1:8088`。
- Windows 原生 Ollama 用 `qwen3-vl:2b` 按需调用 GPU，监听 `127.0.0.1:11434`；
  每次识别后立即卸载模型。原生运行避免 Docker CUDA 镜像额外占用数 GB 磁盘。

## 启动与检查

```powershell
docker compose -f infra/vision/compose.yaml up -d
ollama pull qwen3-vl:2b

Invoke-RestMethod http://127.0.0.1:8088/health
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

OCR 首次真正识别图片时会下载模型，因此第一次可能需要几分钟。模型缓存保存在
Docker 命名卷中，重建容器不会重复下载。Qwen 模型由 Windows Ollama 保存在用户模型
目录中。若 OCR 暂时离线，机器人仍会降级为只用视觉模型识别。

## QQ 中的触发方式

- 发送图片并 `@机器人`，或同时说“看看这张图 / 识图 / 图里是什么”。
- 回复一条带图片的消息，再 `@机器人` 或说“看看”。
- 跑团进行中，发给机器人的图片会自动识别。
- `/vision status` 查看视觉模型、OCR 和缓存状态。

同一轮只分析第一张图，结果按图片内容哈希缓存。视觉识别结果只注入当前一轮，长期
聊天记录只保存“附带图片”标记，避免上下文持续膨胀。

## 停止

```powershell
docker compose -f infra/vision/compose.yaml down
```

不要添加 `--volumes`，否则会删除已下载的 OCR 模型缓存。
