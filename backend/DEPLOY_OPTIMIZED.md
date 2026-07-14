# 后端优化部署指南 v2.0

## 📋 优化内容

| 优化项 | 说明 | 效果 |
|--------|------|------|
| 🚦 **并发控制** | `Semaphore(2)` 限制同时处理 2 个任务 | 防止服务器资源耗尽 |
| 💾 **URL缓存** | 相同 B站/YouTube 链接直接返回缓存结果，不重复处理 | 重复请求响应秒级 |
| ⚡ **合并AI请求** | 纠错+总结一次完成，减少 API 调用 | 提速 ~50% |
| 🔄 **启动预加载** | 服务启动时后台加载 ASR 模型 | 首次请求不等待模型加载 |
| 📊 **进度细化** | 5%-20%-30%-60%-70%-85%-100% 逐步更新 | 小程序进度条更平滑 |
| 🔧 **接口兼容** | 新增 `/api/export`、`/api/view`、`limit` 分页 | 小程序全功能可用 |
| 🧵 **线程池** | ASR 阻塞操作在线程池执行 | 不阻塞事件循环 |

## 🚀 部署步骤

### 1. 测试优化版本（本地）

```bash
cd D:\Dev\AI_Transcriber\backend

# 停止当前运行的后端
# （关掉之前运行 main.py 的 PowerShell 窗口）

# 启动优化版
python main_optimized.py
```

然后在浏览器访问 `http://localhost:8001/`，或在小程序里测试功能。

### 2. 合并你的 ASR 定制

**服务器上**你配置了「**腾讯云 ASR**」和「**阿里百炼**」两个额外 ASR 后端。优化版保留了原有的调用链：

```
transcribe_audio() 中的优先级：
  ① OpenAI Whisper API（你通常没配 OPENAI_API_KEY）
  ② 本地 Whisper 模型
  ③ 本地 FunASR 模型
```

你的服务器版本应该在 `transcribe_audio()` 里插入了额外的 ASR 判断（腾讯云/阿里百炼）。合并步骤：

1. 在优化版的 `transcribe_audio()` 函数中，**在 `if OPENAI_API_KEY:` 之后、`# 第二级：本地模型` 之前** 插入你的 ASR 判断代码

2. 对应的，在 `FUN_ASR_MODEL` 环境变量后面加上你的腾讯云/阿里百炼配置项

### 3. 推送到服务器

```bash
# 在 D:\Dev\AI_Transcriber 目录下
# 用你的方式上传到服务器
# 例如 scp 或 rsync
```

### 4. 服务器上切换

```bash
# SSH 到服务器
ssh 124.221.77.205

# 进入项目目录
cd /path/to/ai-transcriber/backend

# 备份原文件
cp main.py main_backup.py

# 上传优化版 + 合并你的 ASR 定制
# ...

# 重启服务
# （根据你的运行方式，例如 systemctl restart 或直接重启 docker）
```

## 🔧 新增配置

优化版新增环境变量（可选，不配则用默认值）：

```bash
# 最大并发任务数（默认 2）
MAX_CONCURRENT_TASKS=2

# URL 缓存有效期（小时，默认 24）
CACHE_EXPIRE_HOURS=24
```

## ✅ 验证

启动后访问 `http://localhost:8001/api/health` 检查服务状态：

```json
{
  "status": "healthy",
  "asr_model": "whisper",
  "ai_service": "configured or not_configured",
  "pending_tasks": 0,
  "cached_urls": 0,
  "total_tasks": 0
}
```
