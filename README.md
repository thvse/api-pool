# API Pool

一个轻量级的 API 聚合管理面板，支持多端点自动切换、健康检测、模型批量管理。

纯 Python 标准库实现，零依赖，单文件运行。

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![License](https://img.shields.io/badge/License-MIT-green) ![Zero Deps](https://img.shields.io/badge/Dependencies-None-brightgreen)

## 功能

### 核心

- **多端点聚合** — 配置多个 API 端点，按优先级自动调度
- **故障自动切换** — 429 / 超时 / 连接断 / 5xx 等错误自动切到下一个端点
- **并发与额度限流** — 支持每分钟最高并发 (RPM) 限制及每日最高调用额度控制
- **代理精准隔离** — 支持单节点配置“强制直连”或“随系统代理”，无惧 v2ray 等全局代理污染
- **异构协议转换** — 底层原生支持 OpenAI 和 Anthropic (Claude) 协议，对外统一暴露标准 OpenAI 接口，客户端无缝对接
- **冷却恢复** — 端点失败后进入冷却期，到期自动切回高优先级端点
- **健康检测** — 启动时自动检测 + 每 2 分钟定时复检 + 手动触发
- **并发检测** — 使用线程池并发测试所有端点，不阻塞

### 延迟分级

| 状态 | 延迟 | 含义 |
|------|------|------|
| ✅ 绿色 | < 2s | 可用 |
| 🐢 黄色 | 2s ~ 5s | 慢，可用 |
| ❌ 红色 | > 5s / 报错 | 不可用 |

### GUI 面板

- 端点列表 — 筛选（全部/启用/禁用）、健康状态、调用统计
- 聚合链可视化 — 实时显示切换顺序和各端点状态
- 模型浏览器 — 从 API 获取模型列表、搜索、分页、多选
- 批量添加 — 一键添加多个模型为独立端点
- 延迟测试 — 测试选中模型的响应延迟
- 多模态检测 — 实际发图片测试模型是否支持视觉输入
- 免费模型筛选 — OpenRouter 自动识别免费模型

## 快速开始

```bash
# 克隆
git clone https://github.com/yourname/api-pool.git
cd api-pool

# 启动
python api_pool_server.py
```

打开浏览器访问 `http://localhost:5100`

## 配置

### 通过 GUI 配置（推荐）

1. 打开面板 → 点击「＋ 添加端点」
2. 填写 Base URL 和 API Key
3. 点击「🔍 获取」拉取模型列表
4. 选择模型 → 保存

### 通过配置文件

复制示例配置并编辑：

```bash
cp api_config.example.json api_config.json
```

编辑 `api_config.json`：

```json
{
  "api_endpoints": [
    {
      "name": "OpenAI",
      "base_url": "https://api.openai.com/v1",
      "api_key": "sk-xxx",
      "model": "gpt-4o",
      "priority": 1,
      "timeout": 15,
      "max_retries": 1,
      "cooldown_minutes": 5,
      "enabled": true
    },
    {
      "name": "DeepSeek",
      "base_url": "https://api.deepseek.com/v1",
      "api_key": "sk-xxx",
      "model": "deepseek-chat",
      "priority": 2,
      "timeout": 20,
      "max_retries": 0,
      "cooldown_minutes": 5,
      "daily_limit": 1000,
      "rpm_limit": 60,
      "use_proxy": false,
      "enabled": true
    }
  ]
}
```

### 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | string | — | 端点名称 |
| `base_url` | string | — | API 地址（如 `https://api.openai.com/v1`） |
| `api_key` | string | — | API 密钥 |
| `model` | string | — | 模型名称 |
| `priority` | int | 999 | 优先级，数字越小越优先 |
| `timeout` | int | 15 | 请求超时（秒） |
| `max_retries` | int | 0 | 非 429 错误的重试次数（建议为 0 以实现秒切） |
| `cooldown_minutes` | int | 5 | 失败后冷却时间（分钟），0 = 不冷却 |
| `daily_limit` | int | 0 | 每日最高调用次数，超限则该端点当日挂起，0 = 不限 |
| `rpm_limit` | int | 0 | 每分钟并发请求数上限，超限自动切换下一个，0 = 不限 |
| `use_proxy` | bool | true | 是否使用系统代理。如果节点为本地或直连，设为 `false` 可绕过全局代理 |
| `protocol` | string | openai | 节点底层协议，支持 `openai` 和 `anthropic`。若选 `anthropic`，系统会自动将 OpenAI 格式的请求转换为原生 Claude 格式 |
| `enabled` | bool | true | 是否启用 |

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/endpoints` | 获取所有端点 |
| GET | `/api/chain` | 获取当前聚合链 |
| POST | `/api/endpoints` | 添加端点 |
| POST | `/api/endpoints/batch` | 批量添加端点 |
| PUT | `/api/endpoints/<name>` | 更新端点 |
| DELETE | `/api/endpoints/<name>` | 删除端点 |
| POST | `/api/endpoints/<name>/toggle` | 切换启用/禁用 |
| POST | `/api/health-check` | 手动触发健康检测 |
| POST | `/api/fetch-models` | 获取模型列表 |
| POST | `/api/test-model` | 测试模型延迟 |
| POST | `/api/test-vision` | 检测模型多模态能力 |
| POST | `/api/test` | 测试单个端点 |
| POST | `/api/test-pool` | 测试聚合池 |
| POST | `/api/reset` | 重置所有状态 |

## 切换逻辑

```
请求 → 优先级 1 端点
         │
         ├─ 触发额度/并发超限 → 切换到优先级 2
         ├─ 成功 → 返回结果，重置状态
         │
         └─ 429 / 超时 / 5xx / 连接断
              │
              ├─ 设置冷却（默认 5 分钟）
              ├─ 切换到优先级 2 端点
              │    │
              │    ├─ 成功 → 返回
              │    └─ 失败 → 继续往下切
              │
              └─ 冷却到期 → 自动切回优先级 1
```

## 系统要求

- Python 3.10+
- 无第三方依赖

## License

MIT
