# Effect Server

本仓库是 aged donkey 的 Python 后端，通过 FastAPI 为 Haskell EDSL 工作台提供注册、工作流和诊断。项目目录、LSP 模块分工和接入步骤见 [Python LSP 构建指南](LSP.md)。

## 启动开发服务器

使用 Poetry 在项目虚拟环境中启动 Granian：

```bash
poetry install
poetry run granian --interface asgi --host 127.0.0.1 effect_server.http:app
```

服务默认监听 `http://127.0.0.1:8000`。

<a id="framework-root"></a>
## 绑定框架工作树

服务分析和改动的是一个本地框架工作树，即 `algebraic-effect-sys-in-haskell` 的检出目录。启动时用环境变量指定，网页和 HTTP 接口都不能更改。一个服务进程只绑定一个工作树，换工作树要重启服务。

| 环境变量 | 内容 | 未设置时 |
| --- | --- | --- |
| `EFFECT_FRAMEWORK_ROOT` | 框架工作树的根目录 | 编译、执行、添加注册、Workflow 报告和 AST 优化返回 503；Workflow 只列出已发布快照的命名空间 |
| `EFFECT_CODEGEN` | 在同一工作树构建的 `effect-codegen` 可执行文件 | 同上 |
| `EFFECT_CONSOLE_DIR` | 快照目录，发布工具与服务须一致 | 当前目录下的 `.console` |
| `EFFECT_CONSOLE_ORIGINS` | 允许的网页源，逗号分隔 | `http://127.0.0.1:3000,http://localhost:3000` |

从本仓库根目录运行以下 PowerShell 命令，构建 SDK 并以绑定方式启动。路径按工作区调整：

```powershell
$framework = (Resolve-Path ../algebraic-effect-sys-in-haskell).Path
Push-Location $framework
try {
  cabal build exe:effect-codegen
  $sdk = cabal list-bin exe:effect-codegen
} finally { Pop-Location }
$env:EFFECT_FRAMEWORK_ROOT = $framework
$env:EFFECT_CODEGEN = $sdk
poetry run granian --interface asgi --host 127.0.0.1 effect_server.http:app
```

使用时注意以下几点：

- **命名空间的位置**：命名空间位于工作树的 `demo/<命名空间>/`，例如 `demo/LinesDemo/AST.hs`。点号分隔的命名空间对应子目录。Workflow 视图列出 `demo/` 下所有含 `AST.hs` 的目录；Console 视图只列出已发布快照的命名空间。
- **SDK 要与工作树一致**：`EFFECT_CODEGEN` 须与工作树同一版本。其他分支构建的 SDK 可能缺少 `ast`、`console` 等命令，或者生成格式不同。切换工作树或分支后，重新构建 SDK 并重启服务。
- **快照来源**：快照来自发布命令的 `--root`，服务不核对它是否就是 `EFFECT_FRAMEWORK_ROOT`。两者应指向同一工作树，否则 Console 显示的是一个工作树，编译和执行却作用于另一个。
- **前提条件**：编译、执行和添加注册要求该命名空间已有快照；Workflow 报告与 AST 优化只要求 `AST.hs` 存在。

## 发布分析面板快照

按[绑定框架工作树](#framework-root)构建 SDK 后，用同一组路径发布：

```powershell
poetry run python -m effect_server.lsp --sdk $sdk --root $framework snapshot LinesDemo
```

SDK 的 `console Namespace` 复用组合分析，从同一个 Prototype 导出注册、直接方法引用和有序工作流，并从本次加载的手写 Handlers 提取方法体。此命令只读框架工作树。Python 成功后原子替换 `.console/<Namespace>.json`。源码变更后重新发布，再点击页面右上角刷新。可用 `EFFECT_CONSOLE_DIR` 为发布工具和服务指定同一快照目录。

FastAPI 的 `GET /api/v1/console/namespaces` 列出已发布的命名空间，`GET /api/v1/console/namespaces/{namespace}` 返回快照及分析时间。分析由本地发布命令触发。

Workflow 用 `GET /api/v1/console/namespaces?workflow=true` 同时列出绑定工作树中含 `AST.hs` 的命名空间。`GET …/{namespace}/workflow` 返回生成报告的状态，并在 `current` 中给出命名空间内各手写模块当前的 MD5（CRLF 先换成 LF），网页据此丢弃源码改动前的优化预览；`POST …/{namespace}/workflow/optimize` 运行只读 AST 分析，源码加载失败时以 422 返回 GHC 文本。这些工作树请求使用与编译相同的 Origin 校验。

工作流中的 `chain [...]` 对应快照节点 `{"kind": "Chain", ...}`，`children` 保留声明顺序。更新旧工作树的顺序组合名称后，重新构建 SDK 并发布相应命名空间的快照。

使用 `feat/request-chains` 时，将 SDK、工作树和快照目录绑定在同一版本，并重新发布快照。工作台从 v1 注册数据沿 Input 和 `requests` 展开消费链。普通 Context 由请求方驱动，单独执行上游句柄不会发布资产。普通读取缺少请求方时，注册写入接口返回 422，并保留 `Asset never requested` 诊断；持续资产与 alternative 判定资产使用各自的生产协议。[框架规则](https://github.com/cartesianc/algebraic-effect-sys-in-haskell/blob/d10d717/docs/CORE_DESIGN.md#request-production)

从 Console 添加注册、编译或执行程序前，先按[绑定框架工作树](#framework-root)启动服务。

`POST /api/v1/console/namespaces/{namespace}/registrations` 接收 beforegen v1 的注册 JSON。`If-Match` 携带当前快照的 `analyzedAt`，浏览器从已允许的 Origin 提交请求。服务定位 `effects` 显式列表的结束位置，在临时副本插入注册并运行 SDK 组合分析。成功后写入业务 `Effects.hs` 并返回新快照。重名或快照冲突返回 409，SDK 拒绝时返回 422。

浏览器默认只允许 `http://127.0.0.1:3000` 和 `http://localhost:3000` 这两个源。更换网页地址时设置 `EFFECT_CONSOLE_ORIGINS`。

对已发布的命名空间，工作台可向 `/api/v1/console/namespaces/{namespace}/commands/compile` 或 `…/commands/run` 发送带允许 Origin 的 POST。编译先用 SDK 的 `workflow` 更新生成文件，再构建该命名空间的程序。执行在编译成功后启动程序。响应是 NDJSON 流，依次报告阶段、合并的标准输出与错误输出，以及退出码或启动错误。停止或断开输出连接会终止当前命令及其子进程。
