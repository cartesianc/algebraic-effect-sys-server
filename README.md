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

## Facade 编辑与 Gened 审阅

接口前缀为 `/api/v1/console/namespaces/{namespace}`。工作树请求需要工作台的 Origin。绑定的框架和本地构建的 SDK 应当来自同一检出。

| 入口 | 数据来源与用途 |
| --- | --- |
| `GET /facade` | SDK 读取手写注册。响应中的 `graph.cards`、`graph.edges` 和 `graph.handlers` 描述注册、资产、Input、demand 与手写方法绑定。源码定位携带文件指纹和起止行。 |
| `GET /facade/source?file=Effects.hs` | 读取业务 Haskell 模块的原文、定义索引及语法诊断。 |
| `PUT /facade/source?file=Effects.hs` | 以 JSON 字符串提交源码，`If-Match` 使用读取时的文件 SHA-256。冲突返回 409。 |
| `GET /gened?file=Handlers.hs` | tree-sitter-haskell 解析磁盘上的 Generated 模块，返回原文、模块名、定义行范围、模块内同名引用及语法诊断。 |
| `GET /plan` | tree-sitter-haskell 读取 `Generated/Prototype.hs` 的 effect、方法图、选择分支和资产关系，结合可用的 `Generated/Handlers.hs` 显示实现。已有原型直接返回图，缺文件返回 JSON `null`，语法或结构错误返回 422。 |
| `POST /plan` | 用户点击「生成图分析」时调用。持有命名空间锁期间，缺原型才运行已有的 `effect-codegen workflow Namespace`，再读取生成的 Haskell 文件。 |

命令行运行 `poetry run python -m effect_server.lsp --root $framework --sdk $sdk inspect FacadeProbe --cards` 可取得同一份卡片模型。

保存 Facade 文件与生成程序是两个独立操作。「编译」执行 SDK 生成及编译，之后重新读取 Gened 可看到新产物。也可在命令行直接读取磁盘模块：

```powershell
poetry run python -m effect_server.lsp --root $framework gened Crawler --file Handlers.hs
```

## 发布分析面板快照

按[绑定框架工作树](#framework-root)构建 SDK 后，用同一组路径发布：

```powershell
poetry run python -m effect_server.lsp --sdk $sdk --root $framework snapshot LinesDemo
```

SDK 的 `console Namespace` 复用组合分析，从同一个 Prototype 导出注册、直接方法引用和有序工作流，并从本次加载的手写 Handlers 提取方法体。此命令只读框架工作树。Python 成功后原子替换 `.console/<Namespace>.json`。源码变更后重新发布，再点击页面右上角刷新。可用 `EFFECT_CONSOLE_DIR` 为发布工具和服务指定同一快照目录。

FastAPI 的 `GET /api/v1/console/namespaces` 列出已发布的命名空间，`GET /api/v1/console/namespaces/{namespace}` 返回快照及分析时间。分析由本地发布命令触发。

工作台用 `GET /api/v1/console/namespaces?workflow=true` 发现绑定工作树中有 AST、Effects 声明或生成契约的命名空间。`GET …/{namespace}/workflow` 返回生成报告的状态，并在 `current` 中给出命名空间内各手写模块当前的 MD5（CRLF 先换成 LF），网页据此丢弃源码改动前的优化预览；`POST …/{namespace}/workflow/optimize` 运行只读 AST 分析，源码加载失败时以 422 返回 GHC 文本。这些工作树请求使用与编译相同的 Origin 校验。

工作流中的 `chain [...]` 对应快照节点 `{"kind": "Chain", ...}`，`children` 保留声明顺序。更新旧工作树的顺序组合名称后，重新构建 SDK 并发布相应命名空间的快照。

使用 `feat/request-chains` 时，将 SDK、工作树和快照目录绑定在同一版本，并重新发布快照。Python 原样发布 v1 的 `chains`、`bindings[].before` 与 `raw`。Gen 审阅端通过 `/plan` 另行读取已生成的 Haskell 原型。普通 Context 由请求方驱动，单独执行上游句柄不会发布资产。普通读取缺少请求方时，注册写入接口返回 422，并保留 `Asset never requested` 诊断；持续资产的 requests 启动或加入会话，requires 读取已有会话，lifelong 的 streams 也可驱动交付。持续资产有 requires、缺少 requests 和 streams 时也会被拒绝。alternative 判定资产由选择入口驱动。[框架规则](https://github.com/cartesianc/algebraic-effect-sys-in-haskell/blob/f36bb4e/docs/CORE_DESIGN.md#request-production)

从 Console 添加注册、编译或执行程序前，先按[绑定框架工作树](#framework-root)启动服务。

`POST /api/v1/console/namespaces/{namespace}/registrations` 接收 beforegen v1 的注册 JSON。`If-Match` 携带 `/facade` 的 `revision`（旧客户端可使用已发布快照的 `analyzedAt`），浏览器从已允许的 Origin 提交请求。服务定位 `effects` 显式列表的结束位置，在临时副本插入注册并运行 SDK 组合分析。成功后写入业务 `Effects.hs` 并返回新快照。重名或快照冲突返回 409，SDK 拒绝时返回 422。

浏览器默认只允许 `http://127.0.0.1:3000` 和 `http://localhost:3000` 这两个源。更换网页地址时设置 `EFFECT_CONSOLE_ORIGINS`。

对已发布的命名空间，工作台可向 `/api/v1/console/namespaces/{namespace}/commands/compile` 或 `…/commands/run` 发送带允许 Origin 的 POST。编译先用 SDK 的 `workflow` 更新生成文件，再构建该命名空间的程序。执行在编译成功后启动程序。响应是 NDJSON 流，依次报告阶段、合并的标准输出与错误输出，以及退出码或启动错误。停止或断开输出连接会终止当前命令及其子进程。
