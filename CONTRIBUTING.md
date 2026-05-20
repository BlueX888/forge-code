# 贡献指南 (Contributing Guide)

感谢您有兴趣为 **ForgeCode** 做出贡献！开源社区的参与是推动项目不断前行的最大动力。

以下是参与本项目开发和提交贡献的指南。

---

## 目录
- [如何参与](#如何参与)
- [本地开发环境搭建](#本地开发环境搭建)
- [运行测试](#运行测试)
- [代码风格与规范](#代码风格与规范)
- [提交 Pull Request (PR) 流程](#提交-pull-request-pr-流程)

---

## 如何参与

您可以从以下几个方面参与 ForgeCode 的建设：
1. **报告 Bug**：如果您在使用中遇到问题，请通过 [GitHub Issues](https://github.com/your-username/forge-code/issues) 提交，并附带相关的错误日志、操作系统和 Python 版本。
2. **提出新功能 (Feature Request)**：如果您希望 ForgeCode 支持更多模型提供商（如 Local Ollama）、更丰富的工具链或更好的 UI 体验，欢迎提交 Issue 讨论。
3. **改进文档**：修正文档错误、翻译、添加使用教程或示例。
4. **提交代码**：修复已知 Bug、实现新特性、编写测试用例。

---

## 本地开发环境搭建

要开始本地开发，请确保您的系统已安装 **Python 3.10** 或更高版本。

1. **克隆仓库**
   ```bash
   git clone https://github.com/your-username/forge-code.git
   cd forge-code
   ```

2. **创建并激活虚拟环境**
   * **Windows (PowerShell)**:
     ```powershell
     python -m venv .venv
     .venv\Scripts\Activate.ps1
     ```
   * **Linux / macOS**:
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     ```

3. **以可编辑模式安装项目及所有开发依赖**
   ```bash
   pip install --upgrade pip
   pip install -e .[dev,anthropic,openai]
   ```
   *此命令会安装 ForgeCode 以及测试（pytest）和主流大模型提供商（OpenAI, Anthropic）的 SDK 依赖。*

---

## 运行测试

ForgeCode 使用 `pytest` 作为单元测试框架。我们强烈鼓励在提交任何代码更改前运行测试。

1. **运行所有测试**
   ```bash
   pytest
   ```

2. **运行带有测试覆盖率报告的测试**
   ```bash
   pytest --cov=coding_agent
   ```

在编写新功能或修复 Bug 时，请务必为更改的代码补充对应的单元测试。

---

## 代码风格与规范

为了保持代码库的整洁和一致性，请遵循以下规范：
- 符合 [PEP 8](https://peps.python.org/pep-0008/) Python 代码风格指南。
- 变量名、函数名和类名使用清晰、具有自解释性的英文命名。
- 保持非核心更改的代码中原有的注释和 Docstring 的完整性。

我们建议使用 `black` 或 `ruff` 来自动格式化您的代码：
```bash
pip install ruff
ruff format .
ruff check .
```

---

## 提交 Pull Request (PR) 流程

1. **同步最新代码**：在开发新功能前，确保您的本地 `main` 分支与远程上游仓库保持同步。
2. **创建功能分支**：
   ```bash
   git checkout -b feature/your-awesome-feature
   ```
3. **进行开发与测试**：编写代码，并运行 `pytest` 确认所有测试通过。
4. **提交代码**：
   ```bash
   git add .
   git commit -m "feat: 描述您的修改内容"
   ```
5. **推送到您的 Fork 仓库** 并打开 GitHub 页面提交 **Pull Request**。
6. **代码评审**：项目维护者会对您的 PR 进行 Review，并提出改进意见。感谢您的耐心配合与贡献！
