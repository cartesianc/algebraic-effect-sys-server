# Effect Server

本仓库是 Haskell EDSL 网页编辑器的 Python 后端。当前可运行 FastAPI 服务。项目目录、LSP 模块分工和接入步骤见 [Python LSP 构建指南](LSP.md)。

## 启动开发服务器

使用 Poetry 在项目虚拟环境中启动 Granian：

```bash
poetry install
poetry run granian --interface asgi --host 127.0.0.1 effect_server.http:app
```

服务默认监听 `http://127.0.0.1:8000`。

## 发布分析面板快照

在受信任的框架工作树构建 `effect-codegen`。从本仓库根目录运行以下 PowerShell 命令，路径按工作区调整。

```powershell
$framework = (Resolve-Path ../algebraic-effect-sys-in-haskell).Path
Push-Location $framework
try {
  cabal build exe:effect-codegen
  $sdk = cabal list-bin exe:effect-codegen
} finally { Pop-Location }
poetry run python -m effect_server.lsp --sdk $sdk --root $framework snapshot LinesDemo
```

SDK 的 `console Namespace` 复用组合分析，从同一个 Prototype 导出注册、直接方法引用和有序工作流，并从本次加载的手写 Handlers 提取方法体。此命令只读框架工作树。Python 成功后原子替换 `.console/<Namespace>.json`。源码变更后重新发布，再点击页面右上角刷新。可用 `EFFECT_CONSOLE_DIR` 为发布工具和服务指定同一快照目录。

FastAPI 的 `GET /api/v1/console/namespaces` 列出已发布的命名空间，`GET /api/v1/console/namespaces/{namespace}` 返回快照及分析时间。分析由本地发布命令触发。

Workflow 用 `GET /api/v1/console/namespaces?workflow=true` 同时列出绑定工作树中含 `AST.hs` 的命名空间。`GET …/{namespace}/workflow` 返回生成报告的状态，并在 `current` 中给出命名空间内各手写模块当前的 MD5（CRLF 先换成 LF），网页据此丢弃源码改动前的优化预览；`POST …/{namespace}/workflow/optimize` 运行只读 AST 分析，源码加载失败时以 422 返回 GHC 文本。这些工作树请求使用与编译相同的 Origin 校验。

工作流中的 `chain [...]` 对应快照节点 `{"kind": "Chain", ...}`，`children` 保留声明顺序。更新旧工作树的顺序组合名称后，重新构建 SDK 并发布相应命名空间的快照。

从 Console 添加注册、编译或执行程序前，在启动服务的终端绑定本地工作树和已构建的 SDK。

```powershell
$env:EFFECT_FRAMEWORK_ROOT = $framework
$env:EFFECT_CODEGEN = $sdk
poetry run granian --interface asgi --host 127.0.0.1 effect_server.http:app
```

`POST /api/v1/console/namespaces/{namespace}/registrations` 接收 beforegen v1 的注册 JSON。`If-Match` 携带当前快照的 `analyzedAt`，浏览器从已允许的 Origin 提交请求。服务定位 `effects` 显式列表的结束位置，在临时副本插入注册并运行 SDK 组合分析。成功后写入业务 `Effects.hs` 并返回新快照。重名或快照冲突返回 409，SDK 拒绝时返回 422。

浏览器默认允许从 `http://127.0.0.1:3000` 和 `http://localhost:3000` 读取快照。更换网页地址时，用 `EFFECT_CONSOLE_ORIGINS` 指定允许的源，多个源以逗号分隔。

对已发布的命名空间，工作台可向 `/api/v1/console/namespaces/{namespace}/commands/compile` 或 `…/commands/run` 发送带允许 Origin 的 POST。编译先用 SDK 的 `workflow` 更新生成文件，再构建该命名空间的程序。执行在编译成功后启动程序。响应是 NDJSON 流，依次报告阶段、合并的标准输出与错误输出，以及退出码或启动错误。停止或断开输出连接会终止当前命令及其子进程。
