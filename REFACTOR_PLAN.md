# ForgeCode 五目录重构 Plan

## 目标

将当前 `src/coding_agent` 下较扁平的模块结构，重构为 5 个更清晰的职责目录：

- `main`：Agent 核心编排、运行时、上下文、会话、配置等主流程能力
- `cli`：命令行入口、交互展示、命令补全、帮助信息和启动界面
- `tools`：模型可调用的工具定义、注册和具体工具实现
- `memory`：长期记忆、记忆索引、记忆召回和注入逻辑
- `safety`：权限、危险命令识别、安全策略和执行前拦截

重构目标是先完成“物理拆目录 + import 修正”，尽量不改变业务逻辑和行为。

## 目标目录结构

```text
src/coding_agent/
  __init__.py

  main/
    __init__.py
    config.py
    context.py
    context_manager.py
    executor.py
    prompts.py
    runtime.py
    session.py
    token_tracker.py

  cli/
    __init__.py
    banner.py
    cli.py
    commands.py
    completer.py
    io.py

  tools/
    __init__.py
    base.py
    builtin.py
    file_write.py
    registry.py
    search.py
    shell.py

  memory/
    __init__.py
    memory.py

  safety/
    __init__.py
    command_policy.py
    permissions.py
```

## 文件迁移映射

| 当前文件 | 目标位置 | 说明 |
| --- | --- | --- |
| `config.py` | `main/config.py` | 全局配置和运行参数 |
| `context.py` | `main/context.py` | 对话消息和上下文构建 |
| `context_manager.py` | `main/context_manager.py` | 上下文窗口管理与压缩 |
| `executor.py` | `main/executor.py` | 工具调用分组与并发策略 |
| `prompts.py` | `main/prompts.py` | 系统提示词构建 |
| `runtime.py` | `main/runtime.py` | Agent 主循环、模型客户端、工具调用编排 |
| `session.py` | `main/session.py` | 会话持久化 |
| `token_tracker.py` | `main/token_tracker.py` | token 使用统计 |
| `banner.py` | `cli/banner.py` | 启动画面格式化 |
| `cli.py` | `cli/cli.py` | 命令行入口 |
| `commands.py` | `cli/commands.py` | slash 命令定义和帮助文本 |
| `completer.py` | `cli/completer.py` | 命令补全 |
| `io.py` | `cli/io.py` | 终端输入输出 |
| `memory.py` | `memory/memory.py` | 记忆加载、召回、索引维护 |
| `command_policy.py` | `safety/command_policy.py` | shell 命令风险分类 |
| `permissions.py` | `safety/permissions.py` | 权限检查和安全标签 |
| `tools/*` | `tools/*` | 保持现有目录，仅修正引用路径 |

## Import 调整规则

迁移后统一使用绝对导入，避免相对路径层级混乱。

示例：

```python
# 迁移前
from coding_agent.config import AgentConfig
from coding_agent.permissions import PermissionChecker
from coding_agent.io import AgentIO

# 迁移后
from coding_agent.main.config import AgentConfig
from coding_agent.safety.permissions import PermissionChecker
from coding_agent.cli.io import AgentIO
```

重点替换方向：

- `coding_agent.config` -> `coding_agent.main.config`
- `coding_agent.context` -> `coding_agent.main.context`
- `coding_agent.context_manager` -> `coding_agent.main.context_manager`
- `coding_agent.executor` -> `coding_agent.main.executor`
- `coding_agent.prompts` -> `coding_agent.main.prompts`
- `coding_agent.runtime` -> `coding_agent.main.runtime`
- `coding_agent.session` -> `coding_agent.main.session`
- `coding_agent.token_tracker` -> `coding_agent.main.token_tracker`
- `coding_agent.banner` -> `coding_agent.cli.banner`
- `coding_agent.cli` -> `coding_agent.cli.cli`
- `coding_agent.commands` -> `coding_agent.cli.commands`
- `coding_agent.completer` -> `coding_agent.cli.completer`
- `coding_agent.io` -> `coding_agent.cli.io`
- `coding_agent.memory` -> `coding_agent.memory.memory`
- `coding_agent.command_policy` -> `coding_agent.safety.command_policy`
- `coding_agent.permissions` -> `coding_agent.safety.permissions`

## 入口兼容策略

`pyproject.toml` 当前入口为：

```toml
forge-code = "coding_agent.cli:main"
```

重构后建议改为：

```toml
forge-code = "coding_agent.cli.cli:main"
```

如果希望短期兼容旧路径，可以在 `src/coding_agent/cli.py` 保留一个薄转发文件：

```python
from coding_agent.cli.cli import main

__all__ = ["main"]
```

但由于本次目标是拆成 5 个目录，推荐最终移除根目录下的旧 `cli.py`，直接更新 `pyproject.toml`。

## 重构步骤

1. 新建目录

   在 `src/coding_agent` 下创建：

   ```text
   main/
   cli/
   memory/
   safety/
   ```

   并为每个目录添加 `__init__.py`。

2. 移动文件

   按“文件迁移映射”将文件移动到目标目录。

3. 批量修正 import

   使用搜索替换修正所有 `coding_agent.xxx` 引用。

   优先处理被广泛引用的基础模块：

   - `config`
   - `permissions`
   - `context`
   - `tools.base`
   - `io`
   - `session`

4. 更新项目入口

   修改 `pyproject.toml`：

   ```toml
   forge-code = "coding_agent.cli.cli:main"
   ```

5. 检查循环引用

   重点关注：

   - `main/runtime.py`
   - `main/context.py`
   - `memory/memory.py`
   - `tools/file_write.py`
   - `safety/permissions.py`

   如果出现循环引用，优先通过局部导入解决，不要引入新抽象。

6. 运行基础验证

   建议依次执行：

   ```bash
   python -m compileall src
   python -m coding_agent.cli.cli --help
   forge-code --help
   ```

   如果项目已有测试，再执行：

   ```bash
   pytest
   ```

## 验收标准

- `python -m compileall src` 通过
- `forge-code --help` 可以正常输出帮助信息
- `pyproject.toml` 的 console script 指向新入口
- 根目录 `src/coding_agent` 下只保留：
  - `__init__.py`
  - `main/`
  - `cli/`
  - `tools/`
  - `memory/`
  - `safety/`
- 所有跨模块引用均使用新的目录路径
- 不改变原有运行逻辑、权限逻辑、工具行为和会话格式

## 风险点

### 1. `cli` 文件与 `cli` 目录命名冲突

当前已有 `src/coding_agent/cli.py`。迁移到 `src/coding_agent/cli/cli.py` 后，需要确保旧文件被移除或只作为兼容转发，否则 Python 可能优先识别旧模块，导致 `coding_agent.cli.cli` 无法正常导入。

### 2. `memory` 文件与 `memory` 目录命名冲突

当前已有 `src/coding_agent/memory.py`。迁移为 `src/coding_agent/memory/memory.py` 后，同样要避免旧文件残留造成导入歧义。

### 3. `permissions` 和 `command_policy` 被多处引用

安全模块会被 `cli`、`runtime`、`tools` 同时引用。迁移时应优先修正它们，否则容易出现启动阶段导入失败。

### 4. console script 入口必须同步修改

如果只移动文件但忘记更新 `pyproject.toml`，安装后的 `forge-code` 命令会找不到旧入口。

## 建议提交粒度

推荐拆成 3 个提交：

1. `refactor: split modules into five packages`

   只移动文件，添加 `__init__.py`。

2. `refactor: update imports for new package layout`

   修正所有导入路径和入口配置。

3. `test: verify package layout after refactor`

   补充或调整必要测试，确认 CLI 和工具调用仍可用。

## 最小重构原则

本次重构只做目录归类，不建议同时做以下事情：

- 改类名或函数名
- 改运行时逻辑
- 改权限策略
- 改工具协议
- 改 session JSON 格式
- 改配置文件格式

这样可以把风险控制在“导入路径和入口配置”范围内，方便快速验证和回滚。
