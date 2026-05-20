# 🚀 ForgeCode

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python Version">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/AIAgent-Coding-orange.svg" alt="Category">
  <img src="https://img.shields.io/badge/Safety-Sandboxed-red.svg" alt="Safety">
</p>

**ForgeCode** 是一款专为开发者设计的本地、安全、可控的 **AI 自动编码智能体 (AI Coding Agent)**。它运行在您的本地终端，支持 OpenAI 和 Anthropic 等主流模型提供商，能够自动分析项目结构、搜索代码、安全修改文件并执行终端命令。

与其他过度自治、可能失控的 AI 编码代理不同，ForgeCode 建立在**细粒度权限校验**与**智能命令审查**系统之上，将执行决定权安全地保留在您手中，让 AI 协作既高效又安全。

---

## ✨ 核心特性

* 🖥️ **出色的交互式 CLI 体验**：基于 `prompt_toolkit` 构建的高级终端界面，支持命令补全、语法高亮与流畅的交互。
* 🛡️ **三级执行安全策略 (Dangerous Mode)**：
  * `deny`：完全禁止一切写文件和执行 Shell 命令等危险操作。
  * `ask` (默认)：在执行任何危险命令或修改文件前，显式请求您的确认，并告知潜在影响。
  * `allow`：允许完全自主执行。
* 📝 **智能思维可视 (Reasoning / Thinking Showcase)**：原生支持大模型的 Reasoning 过程（如 Claude 思维链或 DeepSeek 思维过程），并通过终端优雅呈现思维深度，控制 Token 预算。
* 💾 **会话持久化与恢复 (Session Persistence)**：支持保存编码会话，您可以随时 `--resume` 恢复上一次的上下文，无需重新向 AI 描述背景。
* 🔌 **灵活的多提供商集成**：支持 `anthropic` (Claude) 和 `openai` (兼容 OpenAI 协议的所有提供商，如 DeepSeek、SiliconFlow、本地 Ollama 等)。

---

## 🛠️ 架构与工作流

ForgeCode 采用模块化架构，将感知、决策与执行清晰地解耦：

```mermaid
graph TD
    A[用户输入/提示词] --> B(Interactive CLI / cli.py)
    B --> C(会话管理 / session.py)
    C --> D(智能体运行时 / runtime.py)
    D --> E{权限过滤器 / permissions.py}
    E -- 安全审查通过 --> F[工具箱 / tools]
    E -- 触发危险指令 --> G{Dangerous Mode 策略}
    G -- 允许 / 用户确认 --> F
    G -- 拒绝 --> H[中断执行并报告错误]
    F --> I[执行 Shell / 修改文件 / 搜索]
    I --> J[结果反馈给 LLM]
```

* **权限与策略机制 (`permissions.py` & `command_policy.py`)**：能够精准解析大模型将要执行的 Shell 命令行。如果包含高危命令（例如系统文件修改、全局删除等），会依据策略拦截并向用户发出警示。
* **增量多文件编辑**：AI 不会粗暴地重写整个文件，而是通过高精度的匹配与替换块，以极低的 Token 成本安全地完成非连续的多行代码修改。

---

## 📦 快速开始

### 1. 安装 ForgeCode

确保您的本地 Python 环境版本 $\ge 3.10$。

```bash
# 克隆仓库
git clone https://github.com/your-username/forge-code.git
cd forge-code

# 使用带有大模型依赖的模式进行本地安装
# 根据您常用的模型提供商选择：
pip install .[openai]      # 使用 OpenAI / DeepSeek 等
pip install .[anthropic]   # 使用 Claude 3.5 Sonnet 等
```

### 2. 配置 API Key

您可以将 API Key 设置为环境变量，或者通过命令行直接传入。

**OpenAI / 兼容提供商 (例如 DeepSeek):**
```bash
export OPENAI_API_KEY="your-api-key"
# 如需使用非官方 OpenAI 节点（例如 DeepSeek API）：
export OPENAI_BASE_URL="https://api.deepseek.com"
```

**Anthropic (Claude):**
```bash
export ANTHROPIC_API_KEY="your-api-key"
```

### 3. 运行智能体

在您的项目工作目录下直接启动 ForgeCode：

```bash
# 启动智能体，默认会在当前目录运行并采取安全询问模式 (ask)
forge-code

# 如果您完全信任智能体，希望其全自主运行：
forge-code --allow-dangerous
```

---

## ⚙️ 命令行参数详解

运行 `forge-code --help` 可以查看所有支持的命令行参数：

| 参数 | 缩写 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `--working-dir` | `-d` | 当前目录 | 智能体操作的工作目录，建议在您想修改的项目根目录下运行 |
| `--dangerous-mode` | - | `ask` | 危险操作策略：`ask` (每次询问), `deny` (完全禁止), `allow` (全自动) |
| `--allow-dangerous` | - | - | 快速开启全自动无阻碍执行模式 (等同于 `--dangerous-mode allow`) |
| `--provider` | - | `openai` | API 提供商类型：支持 `openai` 或 `anthropic` |
| `--model` | - | 自动获取 | 目标模型名称。如 `claude-3-5-sonnet-20241022` 或 `deepseek-chat` |
| `--show-thinking` | - | `True` | 开启或关闭模型思考/推理链过程在终端的显示 (`--no-show-thinking` 关闭) |
| `--thinking-budget`| - | `10000` | 限制大模型思考所能消耗的最大 Token 预算 |
| `--prompt-file` | - | - | 从特定文件读取 Prompt，执行单次运行任务后立即退出 |

### 💾 会话管理参数 (Session Persistence)

| 参数 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `--session [ID]` | - | 开启新会话 (不填 ID) 或恢复指定 ID 的历史会话 |
| `--resume` | - | 自动恢复最近一次运行的会话 |
| `--list-sessions` | - | 列出所有本地保存的历史会话列表 |
| `--delete-session [ID]` | - | 删除指定的历史会话 |
| `--session-dir` | `session/` | 会话文件的本地保存目录（已默认加入 `.gitignore`） |

---

## 🛡️ 安全承诺与最佳实践

AI 编码代理在拥有 Shell 和文件操作权限后，具有强大的生产力，但也潜藏风险。ForgeCode 的安全边界：

1. **绝对忽略目录**：我们默认在 `.gitignore` 和安全读取规则中过滤了敏感的环境变量文件（如 `.env`），Agent 不会意外读取或泄漏您的私钥。
2. **安全建议**：
   * 建议仅在配置了 Git 的项目中运行 ForgeCode。在让 Agent 大规模修改代码或执行 Shell 命令前，通过 `git status` 和 `git diff` 确认变更。
   * 在使用不熟悉的第三方命令时，始终保持 `--dangerous-mode ask`（默认开启）。

---

## 🤝 参与贡献

我们极其欢迎任何形式的贡献！
如果您有关于新工具的设计、更优的 UI 提示，或是发现了任何 Bug，请阅读我们的 [CONTRIBUTING.md](file:///f:/ForgeCode/CONTRIBUTING.md) 以获取开发指南和提交流程。

---

## 📄 开源协议

本项目基于 **[MIT License](file:///f:/ForgeCode/LICENSE)** 开源，您可以自由用于个人、学术或商业用途。
