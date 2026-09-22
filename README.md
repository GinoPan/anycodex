# AnyCodex

![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20tested-informational)
![Deps](https://img.shields.io/badge/dependencies-zero-success)

> **English TL;DR** — Mix official OpenAI models with your own GLM / DeepSeek API keys *inside the same model picker* of the ChatGPT desktop app (Codex). A ~300-line stdlib-only local router passes official traffic through with your ChatGPT login untouched, and routes `glm*` / `deepseek*` models to their vendors. No environment switching, no third-party tools, fully reversible. [中文介绍见下 ↓](#效果)

**在 ChatGPT 桌面版的 Codex 里，官方模型与 GLM / DeepSeek 同一个选择器混选、点谁走谁。**

如果你的 ChatGPT 套餐额度不够用，又订阅了 [GLM Coding Plan](https://bigmodel.cn) 或有 DeepSeek API Key，这个项目让你在**不放弃官方模型和账号能力**的前提下，把它们直接加进 Codex 右下角的模型选择器。

- 官方模型照常用（消耗你的 ChatGPT 套餐）
- GLM / DeepSeek 走你自己的 API Key（消耗 Coding Plan / DeepSeek 额度，便宜得多）
- 零第三方工具依赖：核心是一个数百行、无任何 pip 依赖的 Python 本地路由

> 仅需 Python 3.11+。已在 Windows 实测；macOS/Linux 路由核心为纯 Python，可参考 [手动安装](#其他系统)。

---

## 效果

右下角选择器里，官方模型与自定义模型并列（截图占位，发布前替换为真图 `assets/picker.png`）：

![model picker with mixed models](assets/picker.png)

```
GPT-6-Astra / GPT-5.6-Sol / GPT-5.6-Terra ...   ← 官方，走你的 ChatGPT 套餐
GLM-5.3 / GLM-5.3-Flash                          ← 智谱 Coding Plan
DeepSeek-Flash / DeepSeek-V4-Pro                 ← DeepSeek API
```

选谁就走谁：GLM 系列请求转发到智谱、DeepSeek 系列转发到 DeepSeek（两者均原生支持 OpenAI Responses 协议），其余原样透传到 OpenAI 官方后端并携带你自己的 ChatGPT 登录态。

## 原理

Codex 引擎是"全局单一供应商"设计，模型目录里没有供应商字段——这就是为什么官方路径做不到多厂商混选。AnyCodex 用四层机制绕开它：

```
ChatGPT 桌面版（官方登录态，不做任何修改）
      │ 所有模型请求
      ▼
config.toml：全局供应商指向本机路由（requires_openai_auth = true）
      ▼
codex_router.py（127.0.0.1:8231，按请求体里的 model 字段分流）
      ├─ glm*      → open.bigmodel.cn/api/v1     （智谱 Key）
      ├─ deepseek* → api.deepseek.com            （DeepSeek Key）
      └─ 其他       → chatgpt.com/backend-api/codex（透传你的 ChatGPT JWT，经系统代理）
```

1. **登录态透传**：`requires_openai_auth = true` 让引擎把你的 ChatGPT 凭证发给本机路由，官方请求原样转发——官方模型行为与原生完全一致（实测抓包验证）。
2. **按请求分流**：供应商成为模型名的纯函数，从机制上杜绝"界面选 A、请求发给 B"。
3. **模型目录**（`models.json`）：向引擎声明第三方模型的元数据（上下文窗口、推理档位等），字段经引擎严格校验。
4. **选择器缓存注入**：桌面端选择器的底表来自服务器下发的账号模型清单（`models_cache.json`），路由器内置监视线程在应用每次刷新该缓存后，自动把自定义模型追加进去——这是第三方模型出现在选择器里的关键。

## 前置条件

- ChatGPT 桌面版（已登录，Codex 可正常使用）
- Python 3.11+（仅需标准库）
- [GLM Coding Plan](https://bigmodel.cn) 的 API Key 和/或 [DeepSeek](https://platform.deepseek.com) 的 API Key（可任选其一或都要）
- 如果 cc-switch 等工具正在接管你的 `~/.codex/config.toml`，先用它还原为官方配置再安装（本工具与"切换器"类工具不兼容，因为它需要常驻供应商）

## 安装

```bash
git clone https://github.com/<you>/anycodex.git
cd anycodex
python setup.py
```

安装器会交互式完成：选择厂商 → 填 Key（环境变量里有则自动识别）→ 备份并修改 `~/.codex/config.toml` → 生成模型目录 → 启动路由并注册开机自启。

**最后一步：完全退出并重启 ChatGPT 桌面版**（选择器在启动时加载一次）。

## 使用须知

- **第三方模型请在新对话中使用**，或对旧对话使用"总结上下文，新开窗口继续"。引擎会把供应商钉死在会话创建那一刻——在路由配置之前创建的旧对话里原地切第三方模型会被官方后端拒绝（`not supported when using Codex with a ChatGPT account`）。新对话里官方/第三方随便切。
- 打开选择器太快时第三方模型可能还没注入完（应用启动刷新缓存后约 2 秒内补上），重开一下选择器即可。
- 官方模型报 429 是你的 ChatGPT 套餐额度用完，与本工具无关。
- 修改 `router_config.json`（换 Key、加模型）后重启路由进程即可；改了模型目录相关内容需重启应用。

## 添加其他厂商

编辑 `router_config.json`（或 `~/.codex/router_config.json`），照抄一个 vendor 块改四个字段：`match_prefixes`（模型名前缀）、`host`、`path_prefix`（Responses 端点路径）、Key；`models` 里按模板补模型元数据。重启路由生效。

> 厂商必须原生支持 OpenAI **Responses** 协议才能直连。只有 chat completions 接口的厂商需要在路由里加一层协议翻译（欢迎 PR）。

## 排障

一切看日志：`~/.codex/router.log`（每个请求的模型、路由、状态码）。

| 现象 | 处理 |
|------|------|
| 第三方模型不出现在选择器 | 确认路由在运行（端口 8231）；看日志有无 `models_cache injected`；重启应用后等 2 秒再开选择器 |
| 官方模型从选择器消失 | 多为你的 ChatGPT/Codex 配额耗尽（服务器列表通道随限流失效）。本工具的本地目录会把官方模型兜底显示出来；使用时会正常提示配额限制，重置后恢复 |
| 第三方模型报 "not supported ... ChatGPT account" | 你在路由配置之前创建的旧对话里用了第三方模型——新开对话或用新开窗口流程 |
| 所有模型都失败 | 路由没在运行：运行 `python setup.py` 的第 4 步或手动 `pythonw ~/.codex/codex_router.py` |
| 官方模型 429 | ChatGPT 套餐额度，等重置 |
| 改了配置不生效 | 引擎不热加载：重启 ChatGPT 桌面版 |

## 卸载

```bash
python uninstall.py
```

自动停止路由、移除自启、把 `config.toml` 恢复为官方供应商。安装时的备份文件（`config.toml.bak-anycodex-*`）会保留。

## 其他系统

Windows 为实测平台。macOS/Linux 上路由核心相同（`codex_router.py` 为纯标准库 Python），但 `setup.py` 的系统代理检测与自启动注册仅实现了 Windows——macOS 用户可手动执行等效步骤（启动路由 + 在 config.toml 加 provider 块 + 生成 models.json），欢迎 PR 补全。

## 免责声明

本项目按"现状"提供，仅供学习与个人使用。它修改的是 OpenAI 官方文档公开支持的 Codex 自定义供应商配置（`model_providers`、`model_catalog_json`），并对应用自身维护的本地缓存文件做追加式修改；不逆向、不破解、不绕过任何付费。但应用更新可能改变内部行为导致失效，使用风险自负。与 OpenAI、智谱、DeepSeek 无任何关联。

## 致谢

思路受 [cc-switch](https://github.com/farion1231/cc-switch)、[opencodex](https://github.com/lidge-jun/opencodex) 等项目与智谱 / DeepSeek 官方 Codex 接入文档启发。本项目的差异点：不切换环境、不替换供应商，官方与第三方在同一选择器中共存。
