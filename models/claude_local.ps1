<#
用本地 Ollama 的模型跑 Claude Code（不连云、不消耗额度）。

前置：Ollama 必须是用 models\start_ollama.ps1 启动的（带 OLLAMA_MODELS=D:\AI\Models）。
      直接 ollama serve / 托盘冷启动会看到空模型列表（历史坑，见 CONTEXT.md §16.3）。
原理：Ollama 0.34+ 原生提供 Anthropic Messages 协议端点 /v1/messages，
      Claude Code 只需把 ANTHROPIC_BASE_URL 指过去即可，API Key 填任意值（Ollama 不校验）。

用法::

    # 默认用 PM 模型
    .\models\claude_local.ps1

    # 指定模型（16K 不够时换 32K 版本）
    .\models\claude_local.ps1 -Model qwen3-8b-pm-32k

    # 非交互问一句
    .\models\claude_local.ps1 -Print "列出当前目录的文件"
#>
param(
    [string]$Model = 'qwen3-8b-pm-16k',
    [string]$OllamaHost = 'http://127.0.0.1:11434',
    [string]$Print = ''
)

# 本机坑：CodeBuddy 注入的 NODE_OPTIONS（genie-trash 安全删除 hook）会把 node 子进程卡死
$env:NODE_OPTIONS = ''
$env:CODEBUDDY_SAFE_DELETE_ENABLED = '0'

# 指向本地 Ollama 的 Anthropic 兼容端点
$env:ANTHROPIC_BASE_URL = $OllamaHost
$env:ANTHROPIC_AUTH_TOKEN = 'ollama'
$env:ANTHROPIC_API_KEY = 'ollama'

# 各档位都指向同一个本地模型（Claude Code 会按任务类型挑名字，缺哪个都会回退到云上）
$env:ANTHROPIC_MODEL = $Model
$env:ANTHROPIC_SMALL_MODEL = $Model
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = $Model
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = $Model
$env:ANTHROPIC_DEFAULT_OPUS_MODEL = $Model

Write-Host "Claude Code -> 本地 Ollama $OllamaHost  模型 $Model" -ForegroundColor Cyan
if ($Print) {
    & claude -p $Print
} else {
    & claude @args
}
