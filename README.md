# ForgeCode

ForgeCode 是一个本地命令行 AI 编码助手。它可以读取项目、搜索代码、修改文件并执行命令，同时通过权限策略控制高风险操作。

## 特性

- 支持 OpenAI 兼容接口和 Anthropic。
- 内置文件读取、目录查看、代码搜索、文件写入和命令执行工具。
- 默认对写文件和执行命令等高风险操作进行确认。
- 支持会话保存、恢复和历史管理。
- 支持模型推理内容展示和 token 用量统计。

## 系统架构

ForgeCode 采用高度模块化的分层架构，基于 Python 实现。其系统架构设计专注于**高性能执行**、**严密的沙箱安全策略**以及**智能的上下文管理**。

### 1. 核心模块与职责划分

```
┌────────────────────────────────────────────────────────┐
│                      CLI 交互层                         │
│   (argparse, prompt_toolkit, AgentIO, Cancellation)    │
└───────────┬────────────────────────────────────────────┘
            │ 1. 提交输入 / 命令
            ▼
┌────────────────────────────────────────────────────────┐
│                   Agent 核心运行时                     │
│    (AgentRuntime, SystemPromptBuilder, Session)        │
└───────────┬────────────────────────────────────────────┘
            │ 2. 构建上下文并调用 API
            ▼
┌────────────────────────────────────────────────────────┐
│                   API 适配与流式连接                    │
│   (OpenAIModelClient, AnthropicModelClient)            │
└───────────┬────────────────────────────────────────────┘
            │ 3. 解析出 Tool 调用指令
            ▼
┌────────────────────────────────────────────────────────┐
│                   安全沙箱与权限管理                    │
│      (PermissionChecker, CommandPolicy, Sandbox)       │
└───────────┬────────────────────────────────────────────┘
            │ 4. 权限检查通过
            ▼
┌────────────────────────────────────────────────────────┐
│                      工具执行层                         │
│ (ToolRegistry, Parallel Batch Execution, Custom Skills) │
└───────────┬────────────────────────────────────────────┘
            │ 5. 执行结果
            ▼
┌────────────────────────────────────────────────────────┐
│                 3 层渐进式上下文压缩管道                  │
│       (Truncation -> Pruning -> Compaction)            │
└────────────────────────────────────────────────────────┘
```

* **CLI 交互层 (`src/cli/`)**:
  * `cli.py`: 使用 `argparse` 解析各类 CLI 参数（包括模型、API Key、Session 持久化、Dangerous 模式等），并负责全局与项目级配置的分层加载与热迁移。
  * `io.py`: 基于 `prompt_toolkit` 构建支持实时流式 Token 渲染、模型思考过程（Thinking Budget）折叠展示、命令行自动补全以及语法高亮的交互终端。
  * `cancellation.py`: 支持优雅的系统中断，内置 `EscKeyMonitor` 监听 Escape 键，实现线程/进程安全的无损任务取消。
  * `banner.py`: 渲染精美的启动终端横幅。

* **Agent 运行时 (`src/main/`)**:
  * `runtime.py`: `AgentRuntime` 控制整个 Agent 思考-执行（ReAct）的主循环，包括 Tool 调用的调度、Token 消耗统计和多轮推理控制。
  * `config.py`: 负责加载并合并分层配置：**CLI 参数优先 > 项目配置文件 (`.forgecode.toml`) > 全局配置文件 (`~/.forgecode/config.toml`) > 系统默认值**。
  * `prompts.py`: 动态组装系统提示词（System Prompt），自动注入当前工作目录、平台信息、Git 状态、用户自定义记忆（Memory）以及活跃技能（Skills）。
  * `session.py`: 提供 Session 序列化与反序列化，记录完整的交互历史与 Meta 状态，支持跨终端会话恢复与精确删除。
  * `plan_mode.py`: 控制只读的“规划模式”（Plan Mode）生命周期，保证在设计实现方案时对项目源码是绝对安全的只读状态。

* **3 层渐进式上下文管理管道 (`src/main/context/`)**:
  * **第一层 - 细粒度截断 (Layer 1 - Truncation)** (`truncation.py`): 对每次 Tool 执行产生的超大输出在**进入上下文队列前**进行即时截断。完整的超大输出会被保存到项目本地的缓存文件 `.forgecode/spill/` 中，并在上下文中替换为带引用链接的 Token 友好型元数据摘要，将大文件的检索成本降到最低。
  * **第二层 - 历史修剪 (Layer 2 - Pruning)** (`pruning.py`): 当上下文 Token 接近预设阈值时，自动启动**零 LLM 消耗**的反向扫描，主动将历史记录中老旧、未受保护的只读工具输出内容替换为简短占位符。
  * **第三层 - 语义压缩 (Layer 3 - Compaction)** (`compaction.py`): 发生严重 Token 溢出时，在下一次对话轮次开始前，调用 LLM 对最老的几轮对话进行**语义级主动总结摘要**，将旧的历史信息压缩为高度凝练的上下文背景，彻底解决窗口爆满问题。

* **安全与权限层 (`src/safety/`)**:
  * `permissions.py`: 基于职责链模式（Rule-Chain）的 `PermissionChecker`。每个敏感或写操作都必须顺序通过：**工作目录沙箱限制校验 ──> Plan Mode 只读约束校验 ──> 危险操作阻断校验 (Dangerous Mode)**。
  * `command_policy.py`: 细粒度控制 Shell 命令的执行风险。将指令分为只读白名单（如 `git status`、`pytest` 等可直接运行）和危险动作黑名单（破坏性指令、外部发布指令等），根据安全策略触发用户二次确认或直接拦截。

* **工具与技能层 (`src/tools/`, `src/skills/`)**:
  * `registry.py` & `builtin.py`: 提供可扩展的 Tool 注册表。对连续出现的只读工具调用（如 `Read`、`ListDir`、`Search` 等）执行**并行批处理优化（Parallel Batch Execution）**，大大提升文件检索速度。
  * `file_write.py`: 安全的 `WriteFileTool` 和 `EditFileTool`，确保对文件写动作施加精确范围控制。
  * `shell.py`: 封装受安全策略约束的系统命令执行器。
  * `skills.py`: 自定义命令行宏（Skills），允许用户通过定义简单的配置，扩展 Agent 的 slash 快捷指令集。

---

### 2. 核心执行数据流 (Data Flow)

一个完整的 ReAct 轮次数据流转如下：

1. **用户输入** ──> CLI 解析，`AgentRuntime` 初始化 turn 状态。
2. **构建上下文** ──> `SystemPromptBuilder` 收集环境状态（Git/Memory/Skills），结合 `ContextBuilder` 装载压缩后的历史消息，构建最终 Prompt。
3. **模型流式推理** ──> 适配层通过 API 调用模型。如果模型支持 Reasoning（如 DeepSeek R1），`AgentIO` 将流式渲染其 Thinking 过程；随后流式渲染最终文本或解析出 Tool 调用指令。
4. **工具分批与安全检查** ──>
   * 运行时将检测到的多个 Tool 调用按读写属性分组。
   * 连续的 `READONLY` 工具分入并行批次，并发执行；写操作和 Shell 命令归入串行组。
   * 每个 Tool 调用前，都由 `PermissionChecker` 进行沙箱和命令策略判定。若处于 `DangerousMode.ASK`，会挂起并打印详细的 Diff/命令内容，等待用户在终端输入确认。
5. **渐进式压缩与上下文归档** ──>
   * 工具执行完成后，结果传入 `ContextWindowManager`。
   * **Layer 1** 检测输出大小，必要时落盘溢出（Spill）并截断。
   * 将截断后的结果插入上下文，并评估当前窗口 Token 占比。
   * 若超标，则运行 **Layer 2** Pruning 释放冷数据；若仍有风险，标记 `compaction_pending`，在**下一个轮次开始前**运行 **Layer 3** 异步压缩历史。
6. **循环继续** ──> 模型获得工具执行结果反馈，继续下一次 ReAct 推理，直到得出最终结论并向用户输出。

---

### 3. Plan Mode 规划机制

为了防止 Agent “横冲直撞”破坏代码库，对于复杂任务，用户可以通过 `--plan` 启动或输入 `/plan <task>` 指令进入 **Plan Mode**：
1. **强制只读沙箱**：在 Plan Mode 激活状态下，权限链中的 `_rule_plan_mode_readonly` 规则将被激活。除允许向专用的 Plan 记录文件（`plan_{session_id}_{N}.md`）写入分析报告与 Task 任务清单外，**阻断任何对项目源码的修改以及任何 shell 命令执行**。
2. **交互式评审流程**：
   * 规划完成后，Agent 必须调用 `ExitPlanMode` 提出退出申请。
   * 系统会将 Plan 文件内容（包含 Context、Design、Tasks 实施清单、Verification 验收标准）完整呈现在终端，并为用户提供四种响应选项：
     1. 清空上下文并直接执行（进入自动写模式）。
     2. 保留当前上下文并直接执行（进入自动写模式）。
     3. 手动审批执行（每一步破坏性动作都会弹窗确认）。
     4. 继续修改规划（输入反馈供 Agent 重新调整规划）。
3. **任务跟踪同步**：
   * 进入执行期后，系统通过修改 Plan 实施清单中任务的状态标记（`[pending]` -> `[in_progress]` -> `[x] [done]`）来保持多轮次交互下的进度同步。
   * **开发规程**：Agent 在开始某项 Task 前将其标记为 `[in_progress]`；在此期间进行编码与验证；在**成功运行测试并完成验证后**，方可将该 Task 在 Plan 文件中标记为 `[x] [done]`，开始下一个任务。这确保了进度的真实可靠。

## 安装

需要 Python 3.10 或更高版本，且推荐使用 **pipx** 进行统一的 CLI 安装，以避免依赖冲突和 PATH 配置问题。

### 1. 准备 pipx (前置工具)

根据您的操作系统，选择以下方式之一安装 `pipx`：

- **macOS**:
  ```bash
  brew install pipx
  pipx ensurepath
  ```
  安装完成后，**请重启您的终端**以使 PATH 配置生效。

- **Windows**:
  ```powershell
  scoop install pipx  # 或使用 pip: python -m pip install --user pipx
  pipx ensurepath
  ```
  *注：如果使用 pip 安装的 pipx，可能还需要运行 `register-python-argcomplete`。*

- **Linux**:
  ```bash
  sudo apt install pipx  # Debian/Ubuntu
  pipx ensurepath
  ```

---

### 2. 安装 ForgeCode

克隆仓库并使用 `pipx` 进行本地可编辑 (editable) 安装：

```bash
git clone https://github.com/BlueX888/forge-code.git
cd forge-code

# 如果之前安装过旧版本，请先卸载
pipx uninstall forge-code

# 统一安装命令
pipx install -e .
```

安装完成后，即可直接在任意目录下使用 `forge-code` 命令：

```bash
forge-code --help
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
| `--session [ID]` | 开启或恢复会话。不带 ID：新建会话；带 ID：恢复指定会话 |
| `--resume` | 恢复最近一次会话 |
| `--no-session` | 临时禁用会话创建、加载和持久化 |
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
| `/history [count]` | 查看已保存的会话记录 |
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

开发者统一使用 `pipx` 以可编辑 (editable) 模式安装并包含开发/测试依赖：

```bash
# 如果已安装旧版本，请先卸载
pipx uninstall forge-code

# 安装可编辑版本及开发依赖
pipx install --editable --include-deps ".[dev]"
```

运行单元测试：

```bash
pytest
```

## 许可证

MIT License。详见 [LICENSE](LICENSE)。
