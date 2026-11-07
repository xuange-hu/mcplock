# mcplock · 中文说明

[English README](README.md) · MCP 服务器的**能力锁文件**与漂移检测。

**一句话**：你批准的是今天这个工具集，明天模型读到的是另一份描述。
mcplock 把「agent 能调用什么」变成可 diff、可卡 CI 的锁文件。

## 为什么存在

MCP 的工具、prompt、resource 表面是**运行时协商**出来的，不在任何包锁文件里。
于是三类变更会静默发生：

1. **Rug pull**：工具描述是模型决定「要不要调、怎么调」的唯一依据。审批之后改这段文案，
   是一次供应链动作，普通 JSON diff 不会标红。
2. **Schema 漂移**：新增一个必填参数，让所有既有 prompt 直接失效；删掉一个 enum 值，
   让某条工作流断掉。两者都看不到。
3. **CI 没有闸门**：`mcplock verify` 在 breaking 漂移时退出 1，「agent 的工具集变了没有」
   才第一次变成可检查的构建步骤。

官方 MCP inspector（约 11k★）是 GUI，CI 里跑不了；已有的 drift 工具多是 20★ 级的
零散脚本。零依赖、离线可 diff、分级判定、直接做 composite action 的这个组合空着。

## 三步跑通（全部可离线复现）

仓库里自带 `examples/baseline.json` 与 `examples/drifted.json`，不需要启动任何服务器：

```bash
# 1) 安装（核心零运行时依赖）
pip install git+https://github.com/xuange-hu/mcplock

# 2) 离线比较两个锁文件（实测输出：breaking=4 risky=5 safe=2 new=3）
mcplock diff examples/baseline.json examples/drifted.json

# 3) 离线扫描锁文件里声明的能力面
mcplock risk examples/baseline.json
```

接真实服务器时：

```bash
mcplock snap --out mcplock.json -- npx -y @modelcontextprotocol/server-everything
mcplock verify --lock mcplock.json -- npx -y @modelcontextprotocol/server-everything
```

`verify` 退出码：`0` 无 breaking，`1` 检测到 breaking 漂移，`2` runner 启动失败
（**不把环境故障误报成供应链变更**）。

## 差异化

| 能力 | 说明 |
|---|---|
| **四级漂移** | breaking / risky / new / safe，不是一句「有变化」 |
| **覆盖三类表面** | tools + prompts + resources（含模板） |
| **stdio 与 HTTP/SSE** | 自写 MCP 客户端，`snap --` 跟命令、`--url` 跟远端 |
| **离线风险扫描** | 只看锁文件就能发现危险能力，不必连服务器 |
| **GitHub Action** | `uses: xuange-hu/mcplock@v1`，首跑自动建基线，带 job summary 与 outputs |
| **MCP 4 工具** | `uvx --from "mcplock[mcp]" mcplock-mcp` |

## 命令

| 命令 | 作用 | 是否需要起服务器 |
|---|---|---|
| `snap` | 抓取声明的能力面写入锁文件 | 是 |
| `diff` | 离线比较两个锁文件 | 否 |
| `verify` | 实时服务器对比锁文件，breaking 时退出 1 | 是 |
| `risk` | 离线扫描锁文件里的危险能力 | 否 |
| `inspect` | 人可读形式打印锁文件 | 否 |

**注意**：mcplock 没有 `rules` 也没有 `demo` 子命令，别照家族其它工具的习惯敲。

## 已知边界

- 只能看到服务器**声明**的表面；工具实现里藏的行为变化不在能力面里；
- stdio 客户端靠读线程 + 队列实现超时，不依赖 readline 的阻塞语义；
- HTTP/SSE 传输需要服务端支持对应握手，不支持时 `verify` 报运行失败而非漂移。

贡献见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题见 [SECURITY.md](SECURITY.md)。MIT。
