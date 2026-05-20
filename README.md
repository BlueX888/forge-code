# ForgeCode

ForgeCode 是一个本地命令行 AI 编码助手。它可以读取项目、搜索代码、修改文件并执行命令，同时通过权限策略控制高风险操作。

## 特性

- 支持 OpenAI 兼容接口和 Anthropic。
- 内置文件读取、目录查看、代码搜索、文件写入和命令执行工具。
- 默认对写文件和执行命令等高风险操作进行确认。
- 支持会话保存、恢复和历史管理。
- 支持模型推理内容展示和 token 用量统计。

## 系统架构

ForgeCode 按职责拆分为几个模块：

- `cli`：命令行入口、交互界面和内置指令。
- `main`：配置加载、上下文管理、模型调用、工具调度和会话保存。
- `tools`：文件读取、搜索、写入和命令执行等工具。
- `safety`：路径沙箱、权限确认和命令风险策略。
- `memory`：项目级记忆的存储与检索。

整体流程是：用户输入由 `cli` 接收，`main` 构造上下文并调用模型；模型需要操作项目时，会通过 `safety` 检查后调用 `tools`；执行结果再回传给模型，形成多轮协作。

## 安装

需要 Python 3.10 或更高版本。

```bash
git clone https://github.com/BlueX888/forge-code.git
cd forge-code

pip install -e ".[openai,anthropic]"
```

开发环境可额外安装测试依赖：

```bash
pip install -e ".[dev]"
```

## 快速开始

首次运行需要提供模型配置，ForgeCode 会写入全局配置 `~/.forgecode/config.toml`。后续在任何目录直接运行均默认沿用同一份配置。

```bash
forge-code --model deepseek-chat --api-key sk-xxx --base-url https://api.deepseek.com/v1
```

Anthropic 示例：

```bash
forge-code --provider anthropic --model claude-3-5-sonnet-20241022 --api-key sk-xxx --base-url https://api.anthropic.com
```

之后在任何目录直接运行：

```bash
forge-code
```

## 常用参数

| 参数 | 说明 |
| --- | --- |
| `--working-dir`, `-d` | 指定工作目录，默认当前目录 |
| `--provider` | 模型提供方：`openai` 或 `anthropic` |
| `--model` | 模型名称 |
| `--api-key` | API Key |
| `--base-url` | API Base URL |
| `--dangerous-mode` | 高风险操作策略：`ask`、`deny`、`allow` |
| `--allow-dangerous` | 等同于 `--dangerous-mode allow` |
| `--prompt-file` | 从文件读取提示词，执行一次后退出 |
| `--resume` | 恢复最近一次会话 |
| `--list-sessions` | 查看已保存会话 |
| `--delete-session ID` | 删除指定会话 |

查看更多：

```bash
forge-code --help
```

## 交互命令

在 ForgeCode 交互界面中可使用：

| 命令 | 说明 |
| --- | --- |
| `/read <path>` | 读取文件 |
| `/ls [path]` | 查看目录 |
| `/pwd` | 查看当前目录 |
| `/tools` | 查看可用工具 |
| `/usage` | 查看 token 用量 |
| `/memory` | 查看记忆内容 |
| `/help` | 查看帮助 |
| `/quit` | 退出 |

## 配置文件

### 全局配置文件示例 (`~/.forgecode/config.toml`)：

```toml
[model]
name = "deepseek-chat"
provider = "openai"
api_key = "sk-xxx"
base_url = "https://api.deepseek.com/v1"

[agent]
dangerous_mode = "ask"
show_thinking = true
thinking_budget = 10000
```

### 项目配置文件示例 (`.forgecode.toml`)：

```toml
[agent]
dangerous_mode = "ask"
show_thinking = true
thinking_budget = 10000
```

### 配置优先级与分层规则：

1. **CLI 参数优先**：`--model` / `--provider` / `--api-key` / `--base-url`
2. **全局模型配置其次**：`~/.forgecode/config.toml` 中的 `[model]`
3. **旧项目模型配置兜底**：`.forgecode.toml` 中的 `[model]` (仅作兼容保留，第一次运行会自动将其迁移到全局配置)
4. **项目专属配置**：项目目录下的 `.forgecode.toml` 仍被读取用于配置 `[agent]`、`[commands]` 等参数，实现按项目独立控制权限、安全级别及自定义工具。

## 开发

```bash
pytest
```

## 许可证

MIT License。详见 [LICENSE](LICENSE)。
