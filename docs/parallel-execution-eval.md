# ForgeCode 并行执行引擎评测方案

> 版本: 1.0
> 日期: 2026-05-24
> 适用范围: `src/main/runtime.py` 三阶段并行执行引擎

---

## 目录

1. [评测背景](#1-评测背景)
2. [评测维度总览](#2-评测维度总览)
3. [微基准测试](#3-微基准测试)
4. [集成评测](#4-集成评测)
5. [正确性验证](#5-正确性验证)
6. [SWE-bench A/B 评测](#6-swe-bench-ab-评测)
7. [指标采集方案](#7-指标采集方案)
8. [报告模板](#8-报告模板)
9. [实施路径](#9-实施路径)
10. [附录](#附录)

---

## 1. 评测背景

### 1.1 并行引擎架构

ForgeCode 的并行执行引擎位于 `src/main/runtime.py`，在 Agent 运行时循环中负责执行模型返回的工具调用。引擎采用三阶段流水线设计：

```
┌─────────────────────────────────────────────────────────────┐
│                    _execute_tool_calls()                     │
│                     (runtime.py:1233)                       │
│                                                             │
│  1. _group_tool_calls() 将工具调用分组                        │
│     ├── 连续 READONLY → ToolCallGroup(parallel=True)         │
│     └── 非 READONLY   → ToolCallGroup(parallel=False)        │
│                                                             │
│  2. 逐组执行                                                 │
│     ├── parallel=True  → _execute_parallel_batch()           │
│     └── parallel=False → _execute_tool() (逐个串行)           │
│                                                             │
│  _execute_parallel_batch() 三阶段 (runtime.py:1276):         │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────┐         │
│  │ Phase 1  │→│   Phase 2    │→│    Phase 3     │         │
│  │ Prepare  │  │   Execute    │  │   Finalise    │         │
│  │ (串行)   │  │ (ThreadPool) │  │   (串行)      │         │
│  │ 权限检查  │  │ 并行 tool.run│  │ 有序记录结果   │         │
│  └──────────┘  └──────────────┘  └────────────────┘         │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 工具安全标签

引擎基于 `SafetyLabel` (`src/safety/permissions.py:13`) 决定执行策略：

| 安全标签 | 内置工具 | 并行策略 |
|---------|---------|---------|
| `READONLY` | `read_file`, `list_directory`, `search`, `EnterPlanMode`, `ExitPlanMode` | 连续 READONLY 合并为并行批次 |
| `DESTRUCTIVE` | `write_file`, `edit_file` | 独立串行，需权限确认 |
| `CONCURRENT_SAFE` | `run_command` | 独立串行，跳过 DESTRUCTIVE 确认 |

### 1.3 关键配置

| 配置项 | 默认值 | 位置 |
|-------|-------|------|
| `parallel_tool_execution` | `True` | `src/main/config.py` → `AgentConfig` |
| `ThreadPoolExecutor max_workers` | `min(N, 4)` | `runtime.py:1347` (硬编码) |

### 1.4 评测动机

- 验证并行执行相比串行的**实际延迟降低幅度**
- 确保并行执行**不改变功能正确性**（结果一致、顺序保证）
- 量化三阶段中各阶段的**开销占比**，识别瓶颈
- 为 `max_workers` 调优提供数据支撑
- 在真实 SWE-bench 任务上验证端到端效果

---

## 2. 评测维度总览

```
                        评测维度
          ┌──────────┬──────────┬──────────┬──────────┐
          │  延迟性能  │ 功能正确性 │  安全合规  │  可扩展性  │
          ├──────────┼──────────┼──────────┼──────────┤
          │ 加速比    │ 结果一致  │ 无竞态    │ worker数  │
          │ 阶段耗时  │ 顺序保证  │ 权限隔离  │ 批次规模  │
          │ 吞吐量    │ 幂等性    │ 取消安全  │ I/O 负载  │
          └──────────┴──────────┴──────────┴──────────┘
```

| 维度 | 权重 | 评测层级 | 说明 |
|------|------|---------|------|
| 延迟性能 | 高 | 微基准 + 集成 + SWE-bench | 并行引擎的核心价值指标 |
| 功能正确性 | 高 | 微基准 + 集成 | 基线保障——结果不能因并行而改变 |
| 安全合规 | 中 | 微基准 | DESTRUCTIVE 工具绝不并行执行 |
| 可扩展性 | 低 | 微基准 | 探索 max_workers 和批次大小的边际收益 |

---

## 3. 微基准测试

### 3.1 分组逻辑正确性

**被测函数**: `_group_tool_calls()` (`runtime.py:44`)

#### 测试用例矩阵

| ID | 输入序列 | 预期分组 | 验证点 |
|----|---------|---------|-------|
| G-01 | `[]` (空列表) | `[]` | 边界：无工具调用 |
| G-02 | `[read_file]` | `[P(1)]` | 单个 READONLY |
| G-03 | `[write_file]` | `[S(1)]` | 单个 DESTRUCTIVE |
| G-04 | `[run_command]` | `[S(1)]` | 单个 CONCURRENT_SAFE |
| G-05 | `[read_file, list_directory, search]` | `[P(3)]` | 全 READONLY 合并 |
| G-06 | `[write_file, edit_file, run_command]` | `[S(1), S(1), S(1)]` | 全非 READONLY 各自独立 |
| G-07 | `[read, read, write, read, read]` | `[P(2), S(1), P(2)]` | DESTRUCTIVE 切割 READONLY 批次 |
| G-08 | `[read, run_cmd, read]` | `[P(1), S(1), P(1)]` | CONCURRENT_SAFE 也切割 |
| G-09 | `[write, read, read, read, write]` | `[S(1), P(3), S(1)]` | DESTRUCTIVE 包围 READONLY |
| G-10 | `[unknown_tool]` | `[S(1)]` | 未知工具名降级为串行 |
| G-11 | `[read] * 10` | `[P(10)]` | 大批量 READONLY |
| G-12 | `[read, read, write, read, edit, read, read]` | `[P(2), S(1), P(1), S(1), P(2)]` | 复杂混合序列 |

**P(N)** = `ToolCallGroup(parallel=True, tool_calls=[...N个...])`
**S(1)** = `ToolCallGroup(parallel=False, tool_calls=[...1个...])`

#### 参考测试代码

```python
# tests/test_parallel.py

import pytest
from tools.base import ToolCall
from main.runtime import _group_tool_calls, ToolCallGroup


class FakeReadTool:
    """READONLY mock tool."""
    name = "read_file"
    description = "read"
    parameters_schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    safety_label = SafetyLabel.READONLY
    def run(self, *, arguments, config):
        return ToolResult(True, "ok")


class FakeWriteTool:
    """DESTRUCTIVE mock tool."""
    name = "write_file"
    description = "write"
    parameters_schema = {"type": "object", "properties": {}}
    safety_label = SafetyLabel.DESTRUCTIVE
    def run(self, *, arguments, config):
        return ToolResult(True, "ok")


def _make_registry(*tools):
    """Create a ToolRegistry with given tools."""
    from tools.registry import ToolRegistry
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


class TestGroupToolCalls:
    """G-01 ~ G-12: 分组逻辑正确性验证."""

    def test_g01_empty(self):
        reg = _make_registry()
        assert _group_tool_calls([], reg) == []

    def test_g05_all_readonly(self):
        reg = _make_registry(FakeReadTool(), FakeListDirTool(), FakeSearchTool())
        tcs = [
            ToolCall("read_file", {"path": "a"}, id="1"),
            ToolCall("list_directory", {}, id="2"),
            ToolCall("search", {"pattern": "*"}, id="3"),
        ]
        groups = _group_tool_calls(tcs, reg)
        assert len(groups) == 1
        assert groups[0].parallel is True
        assert len(groups[0].tool_calls) == 3

    def test_g07_mixed_readonly_destructive(self):
        reg = _make_registry(FakeReadTool(), FakeWriteTool())
        tcs = [
            ToolCall("read_file", {"path": "a"}, id="1"),
            ToolCall("read_file", {"path": "b"}, id="2"),
            ToolCall("write_file", {"path": "c", "content": "x"}, id="3"),
            ToolCall("read_file", {"path": "d"}, id="4"),
            ToolCall("read_file", {"path": "e"}, id="5"),
        ]
        groups = _group_tool_calls(tcs, reg)
        assert len(groups) == 3
        assert (groups[0].parallel, len(groups[0].tool_calls)) == (True, 2)
        assert (groups[1].parallel, len(groups[1].tool_calls)) == (False, 1)
        assert (groups[2].parallel, len(groups[2].tool_calls)) == (True, 2)
```

### 3.2 并行 vs 串行延迟对比

**目标**: 用可控延迟的 mock 工具量化加速比

#### 实验设计

```python
class SlowReadTool:
    """模拟 I/O 密集的 READONLY 工具."""
    name = "slow_read"
    safety_label = SafetyLabel.READONLY

    def __init__(self, delay: float = 0.1):
        self._delay = delay

    def run(self, *, arguments, config):
        time.sleep(self._delay)
        return ToolResult(True, f"read {arguments.get('path', '?')}")
```

#### 测试矩阵

| ID | 并行数 N | 单工具延迟 | 串行预期 | 并行预期 | 理论加速比 |
|----|---------|-----------|---------|---------|-----------|
| L-01 | 2 | 100ms | ~200ms | ~100ms | 2.0x |
| L-02 | 3 | 100ms | ~300ms | ~100ms | 3.0x |
| L-03 | 4 | 100ms | ~400ms | ~100ms | 4.0x |
| L-04 | 5 | 100ms | ~500ms | ~125ms | 4.0x (受 max_workers=4 限制) |
| L-05 | 8 | 100ms | ~800ms | ~200ms | 4.0x (受 max_workers=4 限制) |
| L-06 | 10 | 100ms | ~1000ms | ~300ms | ~3.3x (调度开销) |
| L-07 | 2 | 10ms | ~20ms | ~10ms+ | <2.0x (线程开销显著) |
| L-08 | 4 | 500ms | ~2000ms | ~500ms | ~4.0x |

#### 测量方法

```python
import time

def benchmark_parallel_vs_serial(n_tools: int, delay: float, trials: int = 10):
    """对比并行/串行执行耗时."""
    registry = _make_registry(SlowReadTool(delay))
    tool_calls = [
        ToolCall("slow_read", {"path": f"file_{i}"}, id=str(i))
        for i in range(n_tools)
    ]

    # --- 串行基线 ---
    serial_times = []
    for _ in range(trials):
        rt = _make_runtime(parallel=False, registry=registry)
        t0 = time.perf_counter()
        for tc in tool_calls:
            rt._execute_tool(tc)
        serial_times.append(time.perf_counter() - t0)

    # --- 并行测试 ---
    parallel_times = []
    for _ in range(trials):
        rt = _make_runtime(parallel=True, registry=registry)
        t0 = time.perf_counter()
        rt._execute_parallel_batch(tool_calls)
        parallel_times.append(time.perf_counter() - t0)

    serial_avg = sum(serial_times) / trials
    parallel_avg = sum(parallel_times) / trials
    speedup = serial_avg / parallel_avg

    return {
        "n_tools": n_tools,
        "delay_ms": delay * 1000,
        "serial_avg_ms": serial_avg * 1000,
        "parallel_avg_ms": parallel_avg * 1000,
        "speedup": round(speedup, 2),
        "efficiency": round(speedup / min(n_tools, 4) * 100, 1),  # vs 理论最优
    }
```

#### 关键指标

| 指标 | 定义 | 合格标准 |
|------|------|---------|
| 加速比 (Speedup) | serial_time / parallel_time | N<=4 时 >= 0.8*N |
| 并行效率 (Efficiency) | speedup / min(N, max_workers) * 100% | >= 80% |
| 开销税 (Overhead) | parallel_time - max(individual_times) | < 20ms |
| 变异系数 (CV) | std(times) / mean(times) | < 0.15 |

### 3.3 三阶段开销分析

**目标**: 分解 `_execute_parallel_batch` 各阶段耗时，识别瓶颈

#### 计时插桩方案

在 `_execute_parallel_batch` 方法的三个阶段边界插入 `time.perf_counter()` 计时点：

```python
def _execute_parallel_batch(self, tool_calls: list[ToolCall]) -> None:
    t_start = time.perf_counter()

    # Phase 1: Prepare
    prepared = []
    for tc in tool_calls:
        # ... 权限检查、路径验证、参数校验 ...
        prepared.append((tc, tool))
    t_phase1 = time.perf_counter()

    # Phase 2: Execute
    futures = {}
    with ThreadPoolExecutor(max_workers=min(len(prepared), 4)) as pool:
        for tc, tool in prepared:
            futures[id(tc)] = pool.submit(tool.run, ...)
    t_phase2 = time.perf_counter()

    # Phase 3: Finalise
    for tc, _tool in prepared:
        tool_result = futures[id(tc)].result()
        self._context.add_tool_result(...)
    t_phase3 = time.perf_counter()

    # 记录 trace
    trace = ExecutionTrace(
        batch_size=len(prepared),
        prepare_ms=(t_phase1 - t_start) * 1000,
        execute_ms=(t_phase2 - t_phase1) * 1000,
        finalize_ms=(t_phase3 - t_phase2) * 1000,
        total_ms=(t_phase3 - t_start) * 1000,
    )
```

#### 预期分布特征

```
短延迟工具 (I/O < 10ms):
  Phase 1 (Prepare):  ~60-80%  ← 串行权限检查成为瓶颈
  Phase 2 (Execute):  ~10-30%
  Phase 3 (Finalise): ~5-10%

长延迟工具 (I/O > 100ms):
  Phase 1 (Prepare):  ~2-5%
  Phase 2 (Execute):  ~90-95%  ← 工具执行主导
  Phase 3 (Finalise): ~2-5%
```

### 3.4 max_workers 调优评测

**目标**: 确定 `ThreadPoolExecutor` 的最优 worker 数

#### 实验设计

固定 N=8 个 READONLY 工具调用，每个延迟 100ms，遍历 max_workers:

| max_workers | 预期耗时 | 预期加速比 |
|-------------|---------|-----------|
| 1 | ~800ms | 1.0x |
| 2 | ~400ms | 2.0x |
| 4 (当前默认) | ~200ms | 4.0x |
| 6 | ~200ms | 4.0x (CPU核数饱和) |
| 8 | ~100ms | 8.0x (如果无竞争) |
| 16 | ~100ms+ | <=8.0x (过多线程反而有开销) |

#### 测量矩阵

```python
WORKER_CONFIGS = [1, 2, 4, 6, 8, 16]
BATCH_SIZES = [2, 4, 8, 16]
DELAYS = [0.01, 0.05, 0.1, 0.5]  # 10ms ~ 500ms

for workers in WORKER_CONFIGS:
    for batch_size in BATCH_SIZES:
        for delay in DELAYS:
            result = benchmark_with_workers(workers, batch_size, delay)
```

---

## 4. 集成评测

### 4.1 真实工具调用场景模拟

从典型 Agent 对话中提取常见的工具调用模式，构造端到端测试场景。

#### 场景定义

| ID | 场景名称 | 工具序列 | 预期分组 | 说明 |
|----|---------|---------|---------|------|
| S-01 | 多文件探索 | `read_file x5` | `[P(5)]` | 模型同时请求读取多个文件 |
| S-02 | 探索后编辑 | `read x3 → edit x1 → read x2` | `[P(3), S(1), P(2)]` | 典型的"先读后改再验证"模式 |
| S-03 | 搜索并深入 | `search x1 → read x4` | `[P(1), P(4)]` or `[P(5)]` | 搜索结果决定后续读取 |
| S-04 | 纯破坏性操作 | `write x1 → run_command x1` | `[S(1), S(1)]` | 无并行机会，基线场景 |
| S-05 | 大规模代码审查 | `read_file x10` | `[P(10)]` | 验证 max_workers=4 的吞吐天花板 |
| S-06 | 交替读写 | `read, write, read, write, read` | `[P(1), S(1), P(1), S(1), P(1)]` | 频繁切换无法并行 |
| S-07 | 目录扫描+搜索 | `list_directory x3 → search x2 → read x5` | `[P(3), P(2), P(5)]` or `[P(10)]` | 全 READONLY 链 |
| S-08 | 单次操作 | `read_file x1` | `[P(1)]` | 不进入并行路径，验证无额外开销 |

#### 端到端延迟测量

```python
def run_scenario(scenario_id: str, tool_calls: list[ToolCall]) -> ScenarioResult:
    """执行一个场景并采集完整指标."""
    # A 组: 并行
    rt_parallel = _make_runtime(parallel=True)
    t0 = time.perf_counter()
    rt_parallel._execute_tool_calls(tool_calls)
    parallel_ms = (time.perf_counter() - t0) * 1000

    # B 组: 串行
    rt_serial = _make_runtime(parallel=False)
    t0 = time.perf_counter()
    rt_serial._execute_tool_calls(tool_calls)
    serial_ms = (time.perf_counter() - t0) * 1000

    return ScenarioResult(
        scenario_id=scenario_id,
        parallel_ms=parallel_ms,
        serial_ms=serial_ms,
        speedup=serial_ms / parallel_ms,
        tool_count=len(tool_calls),
    )
```

### 4.2 真实文件系统测试

使用项目自身的源码作为测试目标，执行真实的文件读取操作：

```python
# 读取 src/ 下的前 5 个 .py 文件
import glob

py_files = sorted(glob.glob("src/**/*.py", recursive=True))[:5]
tool_calls = [
    ToolCall("read_file", {"path": f}, id=str(i))
    for i, f in enumerate(py_files)
]

# 对比并行 vs 串行的真实 I/O 延迟
```

**注意事项**:
- 首次读取受文件系统缓存影响，应先做 warmup 轮
- Windows NTFS 和 Linux ext4 的并发读取性能差异显著
- 记录操作系统和磁盘类型（SSD/HDD）作为环境变量

### 4.3 取消(Cancellation)场景

验证并行执行期间的取消安全性：

| ID | 场景 | 预期行为 |
|----|------|---------|
| C-01 | Phase 1 期间取消 | 已完成的权限检查结果被丢弃，无工具执行 |
| C-02 | Phase 2 期间取消 | 运行中的工具完成执行，但结果全部记录为 "Cancelled" |
| C-03 | Phase 3 期间取消 | 部分结果已记录，剩余记录为 "Cancelled" |
| C-04 | 组间取消 | 当前组完成，后续组不执行 |

---

## 5. 正确性验证

### 5.1 结果一致性测试

**原则**: 对于相同的工具调用序列，`parallel=True` 和 `parallel=False` 必须产生完全一致的结果。

#### 验证方法

```python
def test_result_consistency(tool_calls, config):
    """并行与串行结果必须完全一致."""
    # A 组: 并行
    rt_a = _make_runtime(parallel=True)
    rt_a._execute_tool_calls(tool_calls)
    results_a = _extract_tool_results(rt_a._context._history)

    # B 组: 串行
    rt_b = _make_runtime(parallel=False)
    rt_b._execute_tool_calls(tool_calls)
    results_b = _extract_tool_results(rt_b._context._history)

    # 逐个对比
    assert len(results_a) == len(results_b)
    for (id_a, res_a), (id_b, res_b) in zip(results_a, results_b):
        assert id_a == id_b, f"tool_call_id mismatch: {id_a} vs {id_b}"
        assert res_a.success == res_b.success
        assert res_a.output == res_b.output
        assert res_a.error == res_b.error
```

#### 测试用例

| ID | 场景 | 验证重点 |
|----|------|---------|
| RC-01 | 5 个 read_file 读取同一文件 | 输出内容完全一致 |
| RC-02 | 5 个 read_file 读取不同文件 | 各结果正确对应 tool_call_id |
| RC-03 | read_file 读取不存在的文件 | error 信息一致 |
| RC-04 | search 搜索相同 pattern | 结果集一致（不要求顺序） |
| RC-05 | 混合 read + search | 各类型结果分别一致 |

### 5.2 有序性保证测试

**原则**: `_execute_parallel_batch` Phase 3 必须按原始 `tool_calls` 顺序记录结果。

```python
def test_result_ordering():
    """工具结果在 context._history 中的顺序必须匹配原始 tool_calls 顺序."""
    tool_calls = [
        ToolCall("read_file", {"path": f"file_{i}.py"}, id=f"tc_{i}")
        for i in range(5)
    ]

    rt = _make_runtime(parallel=True)
    rt._execute_parallel_batch(tool_calls)

    tool_messages = [m for m in rt._context._history if m.role == "tool"]
    for i, (tc, msg) in enumerate(zip(tool_calls, tool_messages)):
        assert msg.tool_call_id == tc.id, (
            f"Position {i}: expected tool_call_id={tc.id}, got {msg.tool_call_id}"
        )
```

#### 时序陷阱验证

```python
class VariableDelayTool:
    """每次调用延迟不同，验证快工具不会"抢占"慢工具的结果位置."""
    safety_label = SafetyLabel.READONLY

    def run(self, *, arguments, config):
        delay = arguments.get("delay", 0)
        time.sleep(delay)
        return ToolResult(True, f"delay={delay}")


def test_fast_doesnt_overtake_slow():
    tool_calls = [
        ToolCall("var_delay", {"delay": 0.3}, id="slow"),   # 第 1 个：慢
        ToolCall("var_delay", {"delay": 0.01}, id="fast"),   # 第 2 个：快
    ]
    rt = _make_runtime(parallel=True, registry=_make_registry(VariableDelayTool()))
    rt._execute_parallel_batch(tool_calls)

    tool_msgs = [m for m in rt._context._history if m.role == "tool"]
    assert tool_msgs[0].tool_call_id == "slow"   # 慢的在前
    assert tool_msgs[1].tool_call_id == "fast"   # 快的在后
```

### 5.3 线程安全验证

| ID | 测试场景 | 验证目标 |
|----|---------|---------|
| TS-01 | 并发 read_file 同一个大文件 | 无文件读取竞争 |
| TS-02 | 并发 search 同一目录树 | 无 glob/regex 状态污染 |
| TS-03 | 并发 list_directory 嵌套目录 | 无路径解析竞争 |
| TS-04 | 并行执行中 config 对象不可变性 | `AgentConfig` frozen + `DynamicPathConfig` 无并发写 |
| TS-05 | 并行执行后 context._history 完整性 | 无消息丢失或重复 |

#### config 不可变性验证

```python
def test_config_immutability_during_parallel():
    """并行 tool.run() 期间，config 状态不被修改."""
    import copy

    config_before = copy.deepcopy(rt._config.__dict__)
    rt._execute_parallel_batch(tool_calls)
    config_after = rt._config.__dict__

    # AgentConfig 是 frozen dataclass，但 DynamicPathConfig 有可变状态
    assert config_before == config_after
```

### 5.4 幂等性验证

```python
def test_idempotent_parallel_execution():
    """相同输入连续执行两次，结果完全一致."""
    for _ in range(2):
        rt = _make_runtime(parallel=True)
        rt._execute_parallel_batch(tool_calls)
        results = _extract_results(rt)
    # 对比两次结果
```

---

## 6. SWE-bench A/B 评测

### 6.1 评测设计

在现有 SWE-bench 评测框架 (`eval/`) 上扩展，执行受控 A/B 实验。

#### 实验配置

| 参数 | A 组 (并行) | B 组 (串行) |
|------|-----------|-----------|
| `parallel_tool_execution` | `True` | `False` |
| `dataset` | SWE-bench Lite | SWE-bench Lite |
| `split` | test | test |
| `count` | 30 | 30 |
| `seed` | 42 | 42 |
| `max_tool_iterations` | 25 | 25 |
| `timeout_per_task` | 600s | 600s |
| `model` | (同一模型) | (同一模型) |

#### 执行命令

```bash
# A 组: 并行 (默认配置)
python -m eval run --count 30 --seed 42 \
  --output-dir eval_output/parallel

# B 组: 串行 (需临时修改配置或新增 CLI 参数)
# 方案 1: 环境变量覆盖
FORGECODE_PARALLEL=false python -m eval run --count 30 --seed 42 \
  --output-dir eval_output/serial

# 方案 2: 新增 --no-parallel 参数 (推荐)
python -m eval run --count 30 --seed 42 --no-parallel \
  --output-dir eval_output/serial
```

### 6.2 对比指标

#### 效率指标 (应有差异)

| 指标 | 定义 | A 组预期 | B 组预期 | 说明 |
|------|------|---------|---------|------|
| avg_duration_seconds | 平均任务耗时 | 更低 | 更高 | 核心加速指标 |
| total_wall_time | 全部任务总耗时 | 更低 | 更高 | 端到端效率 |
| avg_parallel_batches | 平均并行批次数 | >0 | 0 | 并行利用率 |
| avg_batch_speedup | 平均批次加速比 | >1.0 | N/A | 并行效果 |

#### 质量指标 (不应有差异)

| 指标 | 定义 | 预期 | 容忍偏差 |
|------|------|------|---------|
| patch_generated_rate | 生成 patch 的比例 | A==B | 0% (确定性指标) |
| avg_tool_iterations | 平均工具调用轮数 | A==B | 0 (模型行为不变) |
| avg_total_tokens | 平均 token 消耗 | A==B | 0 (模型输入不变) |
| error_rate | 错误率 | A==B | 0% |

> **关键验证**: 如果质量指标出现差异，说明并行执行引入了非确定性行为，需要排查原因。

### 6.3 扩展 eval/runner.py

在现有 `metrics` 字段中新增并行专用指标：

```python
# eval/runner.py — run_single_task() 中的 prediction["metrics"] 扩展

"parallel_metrics": {
    # 汇总
    "total_tool_calls": 47,          # 总工具调用次数
    "total_parallel_batches": 8,     # 并行批次数
    "total_serial_groups": 12,       # 串行组数
    "max_batch_size": 5,             # 最大并行批次大小

    # 延迟
    "parallel_wall_time_ms": 1200,   # 并行批次总耗时
    "serial_estimate_ms": 3400,      # 如果全部串行的估算耗时
    "saved_time_ms": 2200,           # 节省的时间

    # 效率
    "overall_speedup": 2.83,         # 整体加速比
    "avg_batch_speedup": 3.1,        # 平均批次加速比
    "parallel_utilization": 0.71,    # 平均并行度 / max_workers

    # 三阶段分解 (聚合)
    "total_prepare_ms": 45,          # Phase 1 总耗时
    "total_execute_ms": 1100,        # Phase 2 总耗时
    "total_finalize_ms": 55,         # Phase 3 总耗时
}
```

### 6.4 扩展 eval/report.py

在现有报告中新增并行效率章节：

```python
# eval/report.py — generate_report() 新增章节

## Parallel Execution Metrics

| Metric | Value |
|--------|-------|
| Total tool calls | {total_tool_calls} |
| Parallel batches | {total_parallel_batches} |
| Max batch size | {max_batch_size} |
| Time saved | {saved_time_ms}ms |
| Overall speedup | {overall_speedup:.2f}x |
| Avg batch speedup | {avg_batch_speedup:.2f}x |
| Parallel utilization | {parallel_utilization:.0%} |

### Phase Breakdown
| Phase | Total Time | % of Total |
|-------|-----------|------------|
| Prepare (permissions) | {prepare_ms}ms | {prepare_pct:.1f}% |
| Execute (parallel I/O) | {execute_ms}ms | {execute_pct:.1f}% |
| Finalise (record) | {finalize_ms}ms | {finalize_pct:.1f}% |
```

---

## 7. 指标采集方案

### 7.1 数据结构

```python
# src/main/parallel_metrics.py (新增)

import dataclasses
from typing import Any


@dataclasses.dataclass
class ExecutionTrace:
    """单次并行批次的执行跟踪记录."""

    batch_index: int              # 批次序号
    batch_size: int               # 批次中的工具数
    parallel: bool                # 是否并行执行

    # 三阶段耗时 (毫秒)
    prepare_ms: float = 0.0       # Phase 1: 权限检查
    execute_ms: float = 0.0       # Phase 2: 工具执行
    finalize_ms: float = 0.0      # Phase 3: 结果记录
    total_ms: float = 0.0         # 总耗时

    # 单工具耗时 (用于计算串行估算)
    individual_times_ms: list[float] = dataclasses.field(default_factory=list)

    @property
    def serial_estimate_ms(self) -> float:
        """如果串行执行的估算耗时."""
        return sum(self.individual_times_ms) if self.individual_times_ms else self.total_ms

    @property
    def speedup(self) -> float:
        """加速比."""
        if self.total_ms <= 0:
            return 1.0
        return self.serial_estimate_ms / self.total_ms


@dataclasses.dataclass
class ParallelSessionMetrics:
    """一次会话的并行执行统计."""

    traces: list[ExecutionTrace] = dataclasses.field(default_factory=list)

    @property
    def total_tool_calls(self) -> int:
        return sum(t.batch_size for t in self.traces)

    @property
    def parallel_batches(self) -> int:
        return sum(1 for t in self.traces if t.parallel)

    @property
    def serial_groups(self) -> int:
        return sum(1 for t in self.traces if not t.parallel)

    @property
    def max_batch_size(self) -> int:
        return max((t.batch_size for t in self.traces), default=0)

    @property
    def total_parallel_ms(self) -> float:
        return sum(t.total_ms for t in self.traces if t.parallel)

    @property
    def total_serial_estimate_ms(self) -> float:
        return sum(t.serial_estimate_ms for t in self.traces if t.parallel)

    @property
    def overall_speedup(self) -> float:
        est = self.total_serial_estimate_ms
        act = self.total_parallel_ms
        return est / act if act > 0 else 1.0

    @property
    def phase_breakdown(self) -> dict[str, float]:
        p = [t for t in self.traces if t.parallel]
        return {
            "prepare_ms": sum(t.prepare_ms for t in p),
            "execute_ms": sum(t.execute_ms for t in p),
            "finalize_ms": sum(t.finalize_ms for t in p),
        }

    def to_dict(self) -> dict[str, Any]:
        breakdown = self.phase_breakdown
        total_phase = sum(breakdown.values()) or 1
        return {
            "total_tool_calls": self.total_tool_calls,
            "parallel_batches": self.parallel_batches,
            "serial_groups": self.serial_groups,
            "max_batch_size": self.max_batch_size,
            "total_parallel_ms": round(self.total_parallel_ms, 2),
            "total_serial_estimate_ms": round(self.total_serial_estimate_ms, 2),
            "saved_time_ms": round(self.total_serial_estimate_ms - self.total_parallel_ms, 2),
            "overall_speedup": round(self.overall_speedup, 2),
            "prepare_ms": round(breakdown["prepare_ms"], 2),
            "execute_ms": round(breakdown["execute_ms"], 2),
            "finalize_ms": round(breakdown["finalize_ms"], 2),
            "prepare_pct": round(breakdown["prepare_ms"] / total_phase * 100, 1),
            "execute_pct": round(breakdown["execute_ms"] / total_phase * 100, 1),
            "finalize_pct": round(breakdown["finalize_ms"] / total_phase * 100, 1),
        }
```

### 7.2 计时插桩点

在 `_execute_parallel_batch` 中注入计时逻辑：

```
runtime.py:1276  _execute_parallel_batch()
│
├── t0 = perf_counter()
│
├── Phase 1: Prepare ──────────────────────────
│   for tc in tool_calls:
│       registry.get()          # 工具查找
│       _check_and_approve_path()  # 路径审批
│       permissions.check()     # 权限规则链
│       validate_required()     # 参数校验
│
├── t1 = perf_counter()
│
├── Phase 2: Execute ──────────────────────────
│   ThreadPoolExecutor.submit()  # 提交任务
│   (并行执行 tool.run())
│   # 需要记录每个 future 的单独耗时
│
├── t2 = perf_counter()
│
├── Phase 3: Finalise ─────────────────────────
│   for tc, _ in prepared:
│       future.result()          # 获取结果
│       context.add_tool_result()  # 记录到上下文
│
├── t3 = perf_counter()
│
└── record ExecutionTrace(prepare=t1-t0, execute=t2-t1, finalize=t3-t2)
```

### 7.3 单工具耗时采集

为计算串行估算值，需要包装 `tool.run()` 以记录每次调用的实际耗时：

```python
def _timed_run(tool, arguments, config):
    """Wrapper that records execution time."""
    t0 = time.perf_counter()
    result = tool.run(arguments=arguments, config=config)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return result, elapsed_ms
```

---

## 8. 报告模板

### 8.1 微基准报告格式

```markdown
# 并行执行引擎微基准报告

## 环境
- OS: Windows 11 Pro / Linux x.y
- CPU: AMD Ryzen 7 5800X (8C/16T)
- Disk: NVMe SSD
- Python: 3.12.x
- ForgeCode: vX.Y.Z
- 测试日期: 2026-MM-DD

## 分组逻辑测试
通过: 12/12 用例
失败: 0

## 延迟对比

| 并行数 | 单工具延迟 | 串行耗时 | 并行耗时 | 加速比 | 效率 |
|--------|-----------|---------|---------|-------|------|
| 2 | 100ms | 201ms | 103ms | 1.95x | 97.5% |
| 4 | 100ms | 403ms | 108ms | 3.73x | 93.3% |
| 8 | 100ms | 804ms | 215ms | 3.74x | 93.5% |

## 三阶段开销

| 批次大小 | Phase 1 | Phase 2 | Phase 3 | 总计 |
|---------|---------|---------|---------|------|
| 2 | 0.3ms (3%) | 102ms (95%) | 0.8ms (1%) | 103ms |
| 4 | 0.5ms (0.5%) | 107ms (99%) | 0.6ms (0.5%) | 108ms |

## max_workers 调优

| workers | 批次=4 | 批次=8 | 批次=16 |
|---------|--------|--------|---------|
| 2 | 202ms | 403ms | 804ms |
| 4 | 108ms | 215ms | 425ms |
| 8 | 108ms | 115ms | 220ms |

**结论**: 当前 max_workers=4 在大多数场景下已接近最优。
```

### 8.2 SWE-bench A/B 报告格式

```markdown
# SWE-bench 并行执行 A/B 测试报告

## 实验配置
- 数据集: SWE-bench Lite (30 tasks, seed=42)
- 模型: claude-sonnet-4-20250514
- A 组: parallel_tool_execution=True
- B 组: parallel_tool_execution=False

## 效率对比

| 指标 | A 组 (并行) | B 组 (串行) | 差异 |
|------|-----------|-----------|------|
| 平均任务耗时 | 45.2s | 52.8s | -14.4% |
| 总耗时 | 22.6min | 26.4min | -14.4% |
| 平均并行批次 | 3.2/task | 0 | - |
| 平均批次加速比 | 2.8x | N/A | - |

## 质量对比 (应无差异)

| 指标 | A 组 | B 组 | 差异 |
|------|------|------|------|
| patch_generated | 24/30 | 24/30 | 0 |
| avg_tokens | 12,340 | 12,340 | 0 |
| avg_iterations | 8.3 | 8.3 | 0 |
| errors | 2/30 | 2/30 | 0 |

## 并行效率分析

### 批次大小分布
| 批次大小 | 出现次数 | 占比 |
|---------|---------|------|
| 1 | 12 | 12.5% |
| 2 | 35 | 36.5% |
| 3 | 28 | 29.2% |
| 4 | 15 | 15.6% |
| 5+ | 6 | 6.3% |

### 三阶段耗时分布
| Phase | 总计 | 占比 |
|-------|------|------|
| Prepare | 1.2s | 2.8% |
| Execute | 40.5s | 94.5% |
| Finalise | 1.1s | 2.6% |

## 结论
- 并行执行在不影响输出质量的前提下，降低了约 14% 的端到端延迟
- 并行利用率 71%，说明模型经常发出 2-3 个连续 READONLY 工具调用
- Phase 1 (权限检查) 开销可忽略，不构成瓶颈
- 建议保持 max_workers=4 的默认值
```

### 8.3 综合评分卡

```
┌─────────────────────────────────────────────┐
│         并行执行引擎评测评分卡               │
├──────────────────┬──────────┬───────────────┤
│ 维度             │ 评分     │ 状态          │
├──────────────────┼──────────┼───────────────┤
│ 分组逻辑正确性    │ 12/12   │ PASS          │
│ 结果一致性        │ 5/5     │ PASS          │
│ 有序性保证        │ 3/3     │ PASS          │
│ 线程安全          │ 5/5     │ PASS          │
│ 取消安全          │ 4/4     │ PASS          │
│ 加速比 (N=4)     │ 3.73x   │ >= 3.2x PASS  │
│ 并行效率          │ 93.3%   │ >= 80% PASS   │
│ SWE-bench 延迟改善│ -14.4%  │ >= -5% PASS   │
│ SWE-bench 质量差异│ 0%      │ == 0% PASS    │
├──────────────────┼──────────┼───────────────┤
│ 总体             │ 9/9     │ ALL PASS      │
└──────────────────┴──────────┴───────────────┘
```

---

## 9. 实施路径

### 阶段 1: 基础测试 (预计 1-2 天)

**目标**: 覆盖分组逻辑和结果正确性

| 步骤 | 任务 | 产出 |
|------|------|------|
| 1.1 | 创建 `tests/test_parallel.py` | 分组逻辑测试 (G-01~G-12) |
| 1.2 | 实现 mock 工具 (SlowReadTool, VariableDelayTool) | 测试工具集 |
| 1.3 | 编写结果一致性测试 (RC-01~RC-05) | 正确性验证 |
| 1.4 | 编写有序性保证测试 | 顺序验证 |
| 1.5 | 编写线程安全测试 (TS-01~TS-05) | 安全性验证 |

**验证**: `pytest tests/test_parallel.py -v` 全绿

### 阶段 2: 延迟基准测试 (预计 1 天)

**目标**: 量化加速比和三阶段开销

| 步骤 | 任务 | 产出 |
|------|------|------|
| 2.1 | 创建 `tests/benchmark_parallel.py` | 延迟对比脚本 (L-01~L-08) |
| 2.2 | 实现三阶段计时插桩 | `ExecutionTrace` 数据采集 |
| 2.3 | 实现 max_workers 扫描 | worker 调优数据 |
| 2.4 | 生成微基准报告 | `eval_output/micro_benchmark.md` |

**验证**: 报告中加速比数据合理 (N<=4 时效率 >= 80%)

### 阶段 3: 集成评测框架 (预计 1-2 天)

**目标**: 真实场景端到端性能

| 步骤 | 任务 | 产出 |
|------|------|------|
| 3.1 | 创建 `src/main/parallel_metrics.py` | 指标数据结构 |
| 3.2 | 在 `_execute_parallel_batch` 中注入计时 | 运行时指标采集 |
| 3.3 | 实现集成测试场景 (S-01~S-08) | 场景化评测 |
| 3.4 | 实现取消安全测试 (C-01~C-04) | 取消场景覆盖 |

**验证**: 真实文件 I/O 场景的加速数据合理

### 阶段 4: SWE-bench A/B 评测 (预计 1-2 天)

**目标**: 端到端效果验证

| 步骤 | 任务 | 产出 |
|------|------|------|
| 4.1 | 扩展 `eval/runner.py` 采集并行指标 | 数据采集 |
| 4.2 | 扩展 `eval/report.py` 新增并行章节 | 报告增强 |
| 4.3 | 新增 `--no-parallel` CLI 参数 | A/B 测试开关 |
| 4.4 | 执行 30-task A/B 实验 | 对比数据 |
| 4.5 | 生成最终评测报告 | `eval_output/parallel_ab_report.md` |

**验证**: 质量指标 A==B, 延迟指标 A<B

### 文件清单

```
新增:
  docs/parallel-execution-eval.md     ← 本文档
  tests/test_parallel.py              ← 阶段 1: 基础测试
  tests/benchmark_parallel.py         ← 阶段 2: 延迟基准
  src/main/parallel_metrics.py        ← 阶段 3: 指标数据结构

修改:
  src/main/runtime.py                 ← 阶段 3: 计时插桩
  eval/runner.py                      ← 阶段 4: 采集并行指标
  eval/report.py                      ← 阶段 4: 报告并行章节
  eval/__main__.py                    ← 阶段 4: --no-parallel 参数
```

---

## 附录

### A. 工具安全标签速查表

| 工具名 | 安全标签 | 源码位置 | 并行策略 |
|--------|---------|---------|---------|
| `read_file` | `READONLY` | `tools/builtin.py` | 可并行 |
| `list_directory` | `READONLY` | `tools/builtin.py` | 可并行 |
| `search` | `READONLY` | `tools/search.py` | 可并行 |
| `write_file` | `DESTRUCTIVE` | `tools/file_write.py` | 串行+确认 |
| `edit_file` | `DESTRUCTIVE` | `tools/file_write.py` | 串行+确认 |
| `run_command` | `CONCURRENT_SAFE` | `tools/shell.py` | 串行(无确认) |
| `EnterPlanMode` | `READONLY` | `tools/enter_plan_mode.py` | 可并行 |
| `ExitPlanMode` | `READONLY` | `tools/exit_plan_mode.py` | 可并行 |

### B. 配置参数对照表

| 参数 | 类型 | 默认值 | 影响 |
|------|------|-------|------|
| `parallel_tool_execution` | `bool` | `True` | 总开关 |
| `max_workers` | hardcoded | `min(N, 4)` | 最大并发线程数 |
| `max_tool_iterations` | `int` | `100` | 单次对话最大工具调用轮数 |

### C. 环境变量约定

| 变量 | 用途 | 示例 |
|------|------|------|
| `FORGECODE_PARALLEL` | 运行时覆盖并行开关 | `false` |
| `FORGECODE_MAX_WORKERS` | 运行时覆盖 worker 数 | `8` |
| `FORGECODE_TRACE_PARALLEL` | 启用三阶段计时输出 | `1` |

### D. 相关源码入口

| 功能 | 文件 | 行号 | 函数/类 |
|------|------|------|--------|
| 分组逻辑 | `src/main/runtime.py` | 44 | `_group_tool_calls()` |
| 执行分发 | `src/main/runtime.py` | 1233 | `_execute_tool_calls()` |
| 三阶段执行 | `src/main/runtime.py` | 1276 | `_execute_parallel_batch()` |
| 单工具执行 | `src/main/runtime.py` | 1135 | `_execute_tool()` |
| 安全标签 | `src/safety/permissions.py` | 13 | `SafetyLabel` |
| 权限检查 | `src/safety/permissions.py` | 106 | `PermissionChecker` |
| 并行开关 | `src/main/config.py` | - | `AgentConfig.parallel_tool_execution` |
| 工具注册 | `src/tools/builtin.py` | 146 | `register_builtin_tools()` |
| Token 跟踪 | `src/main/token_tracker.py` | 23 | `TokenUsageTracker` |
| eval 运行器 | `eval/runner.py` | 196 | `run_single_task()` |
| eval 报告 | `eval/report.py` | 9 | `generate_report()` |
