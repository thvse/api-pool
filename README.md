# API Pool

一个轻量级的 API 聚合管理面板，支持多端点自动切换、健康检测、模型批量管理。
纯 Python 标准库实现，零依赖，单文件运行。
![Python](https://img.shields.io/badge/Python-3.10+-blue) ![License](https://img.shields.io/badge/License-MIT-green) ![Zero Deps](https://img.shields.io/badge/Dependencies-None-brightgreen)

> **✨ 推荐截图位置**：你可以把你的炫酷控制台截图命名为 \screenshot.png\ 并放在这儿！
> \![Dashboard Screenshot](screenshot.png)
## 核心亮点

- **智能健康探针 🛡️** ：
  支持为昂贵的模型设置「零成本探测模式」或「免扰模式」，既能精准捕获首字延迟（TTFT），又能自动识别冷启动进行二次唤醒，坚决避免对大厂计费接口的冗余探测误杀。
- **LMSYS Elo 客观优先级 🏆** ：
  内置自动化排序逻辑，根据真实大盘性能排行榜（Elo）决定哪个 API 顺位靠前，让请求永远打到最强大脑上。
- **多维度高可用网关 🔁** ：
  当节点遇到 429、5xx 或者网络超时，请求会被瞬间无缝切换至备胎节点。
- **异构协议与直连隔离 🔌** ：
  底层原生支持 OpenAI 与 Anthropic (Claude) 双协议自由互转，并支持单节点级别控制「全局代理」或「纯净直连」。
- **Vercel / Grafana 级数据大盘 📊** ：
  独享现代玻璃拟物化 (Glassmorphism) UI，基于持久化 SQLite 追踪 Token 结构及调用明细。

## 功能特性

- **多端点聚合** ：配置多个 API 端点，按优先级自动调度。
- **并发与额度限制** ：支持每分钟最高并发 (RPM) 限制及每日最高调用额度控制，超限自动顺延。
- **延迟分级** ：自动用绿/黄/红三色标注端点健康度与延迟情况。
- **GUI 面板** ：从模型浏览、批量添加、全功能测试到可视化大盘，前端页面功能极度丰富。

## 快速开始
`ash
# 克隆
git clone https://github.com/yourname/api-pool.git
cd api-pool

# 启动
python api_pool_server.py
`

打开浏览器访问 http://localhost:5100

## API 接口与测试工具

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/endpoints | 获取所有端点 |
| POST | /api/test-pool | 测试聚合池高可用转移能力 |
| POST | /api/health-check | 手动触发全盘探针雷达 |

## 切换逻辑

\请求 → 优先级 1 端点
         │
         ├─ 触发额度/并发超限 → 切换到优先级 2
         ├─ 成功 → 返回结果，重置状态
         │
         └─ 429 / 超时 / 5xx / 连接异常
              │
              ├─ 设置冷却（默认 5 分钟）
              ├─ 切换到优先级 2 端点
              │    │
              │    ├─ 成功 → 返回
              │    └─ 失败 → 继续往下切
              │
              └─ 冷却到期 → 自动切回优先级 1
\
## License
MIT
