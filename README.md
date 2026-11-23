# Effect Server

本仓库是 aged donkey 的 Python 后端，通过 FastAPI 为 Haskell EDSL 工作台提供注册、工作流和诊断。项目目录、LSP 模块分工和接入步骤见 [Python LSP 构建指南](LSP.md)。

## 启动开发服务器

使用 Poetry 在项目虚拟环境中启动 Granian：

```bash
poetry install
poetry run granian --interface asgi --host 127.0.0.1 effect_server.http:app
```

服务默认监听 `http://127.0.0.1:8000`。

## MCP 与节点资料

本机 MCP 客户端通过 Streamable HTTP 连接 `http://127.0.0.1:8000/mcp/`。用 `tools/list` 查看工具及参数，通过 `prompts/list` 和 `prompts/get` 获取每项操作的提示词、输入结构及相关只读上下文。可以从 `list_projects`、`list_namespaces` 和 `read_facade` 开始。修改源码或节点资料时，使用相应读取工具返回的 `revision`。

`read_handler_context` 和 `draft_handler` 共用写作上下文。指定 Effect 注册名、role 和 method 后，服务会沿 input、资产契约、共享 Handler、故障处理和有效报告中的挂载范围收集关联注册，并从磁盘上该方法的 Layer 提取对应的 SDK 类方法签名。MCP 客户端可以调用 `inspect_symbol`，草稿请求也可携带 `symbol: {file, line, character}`，按需用 HLS 查询手写模块里的符号类型。行和 UTF-16 列均从零开始。网页的 Handler 按钮通过本机 Codex CLI 生成方法体，`save_handler` 在临时副本中调用 SDK 编译验证后写入。服务端提示词集中在 `src/effect_server/lsp/agent/prompt.py`。

FastAPI 启动时会在 `EFFECT_CONSOLE_DIR`（默认 `.console`）下的 `projects.sqlite3` 创建命名空间表和节点表。后台通过 LSP 的命名空间入口和 SDK 读取已保存的源码，按项目、命名空间和注册名核对节点。每张 Effect 或资产卡片独占一次 Codex 会话，每个后端进程最多同时翻译 4 张，逐张写回。优先领取资产；相关资产正在翻译时，Effect 等待其结果，再读取已有的资产译名。注册重命名视为旧标识删除、新标识创建。Workflow 中的多次引用共用同一条记录。

翻译提示词带上框架的执行规则，以及该卡片绑定的各角色、Handler 实现、磁盘上的 Layer 与对应 SDK 类方法签名。缺少生成契约时，会按手写声明筛选候选类方法并注明来源。Effect 说明按实现里的值操作顺序，用业务语言描述读取、条件、变换和交付。资产说明从生产者的 parser 解释值的来历，标题须能独立读作名词，有动宾歧义时用“的”消歧或改写。

在启动环境中将 `EFFECT_METADATA_PROJECTS` 设为允许自动发送至 Codex 的项目 ID（多个 ID 用逗号分隔，可从 `list_projects` 读取）。其他项目也会建立本地索引，用户可以在卡片上单独请求翻译。

React 将翻译和 Handler 草稿请求交给 FastAPI，由服务端调用本机 Codex CLI。Effect 和资产的单卡、批量及后台翻译共用 `translation` 设置，默认为 `gpt-6.1-terra` / `medium`。Handler 草稿使用 `codegen` 设置，默认为 `gpt-6.1-sol` / `high`。

### 在新设备上使用 Codex

在运行 FastAPI 的机器上安装 Codex CLI，用运行服务的系统账户执行 `codex login`，再用 `codex login status` 确认。已登录的账户可直接使用。浏览器只请求 FastAPI。无桌面的 Linux 可使用 `codex login --device-auth` 完成登录。[官方安装方式](https://developers.openai.com/codex/cli) · [登录与设备授权](https://developers.openai.com/codex/auth)

工作台优先使用可选的 `EFFECT_CODEX`，平时可以留空。服务会从自己的 `PATH` 查找 CLI，也支持以下默认安装位置。

| 环境 | 默认查找位置 |
| --- | --- |
| Windows | 官方独立安装目录、当前用户的 npm 全局安装目录，以及该用户已注册的 Codex 桌面应用 |
| Linux / WSL | 当前 Linux 用户的 `~/.local/bin/codex` |
| macOS | `~/.local/bin/codex`、Homebrew 标准目录，以及 `/Applications/Codex.app` 内的 CLI |

自定义安装目录可将 `EFFECT_CODEX` 设为可执行文件的绝对路径。npm 安装的 CLI 还需要服务能找到 Node.js。用 systemd、容器或其他账户运行时，从该服务环境检查登录、可写的临时目录和网络。已有自定义 `CODEX_HOME` 的账户应让服务继承同一设置。模型列表可用 `codex debug models` 核对；生成还要求该账户有权使用所选模型。

可在服务的 Python 环境运行以下只读检查，输出实际选中的启动程序。

```sh
poetry run python -c "from effect_server.lsp.agent.codex import command; print(command())"
```

迁移工作台时在新机器重新导入项目，并用本机的 GHC/Cabal 构建 SDK。项目绑定包含本机绝对路径。需要自动翻译时，用新机器返回的项目 ID 配置 `EFFECT_METADATA_PROJECTS`。

模型设置保存在工作台的 `projects.sqlite3`，对该服务的所有项目生效。每次启动模型调用时读取对应设置。通过以下 FastAPI 接口管理，HTTP 请求携带工作台允许的 `Origin`，本地默认为 `http://127.0.0.1:3000`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/console/agent/models` | 列出服务端 Codex 的 GPT 模型和各模型支持的推理强度 |
| GET | `/api/v1/console/agent/models/{purpose}` | 读取 `translation` 或 `codegen` 的当前设置 |
| PUT | `/api/v1/console/agent/models/{purpose}` | 更新指定用途。请求体格式为 `{"model":"gpt-6.1-sol","reasoning_effort":"high"}` |

三个接口分别对应 MCP 工具 `list_agent_models`、`read_agent_model` 和 `set_agent_model`。

工作台顶栏的「重新生成翻译」对应 `regenerate_namespace_translations`，为当前命名空间的未锁定 Effect 和资产排队重译。用 `translation_status` 查询进度。Effect 按注册名、资产按 `asset:契约名` 定位。业务分析的生成图和优化后的 Workflow 共享 SQLite 译名，可通过「翻译」切换显示。网页通过 FastAPI 轮询卡片状态，发现待翻译的卡片时每秒查询，空闲时每 8 秒查询。已完成的译文随下一次响应显示，尚未完成的卡片沿用已有译名或注册名。

`read_node_metadata` 返回译文、编辑标记、锁和版本。`edit_node_metadata` 可以修改译文或设置 `locked`。下一次翻译会参考人工修改过的字段，成功写回后清除编辑标记；失败时保留原文和标记。`generate_node_metadata` 单独翻译一张卡片。加锁会跳过自动、单卡和批量翻译，也会阻止已发起的旧任务覆盖译文。

手写源码页可以使用服务端的 Haskell Language Server（HLS）检查未保存的代码、查看类型和跳转到定义。打开手写源码页时才启动 HLS，同一编辑页中的文件共享进程；退出页面时释放。服务端从 PATH 查找 `haskell-language-server-wrapper`，也可用 `EFFECT_HLS` 指定可执行文件。缺少 HLS 时仍可编辑和保存，SDK 编译与执行使用项目自身的 GHC/Cabal 环境。安装支持项目 GHC 版本的 HLS 后，可在编辑页重新连接。参见 [HLS 安装指南](https://haskell-language-server.readthedocs.io/en/stable/installation.html)。

<a id="framework-root"></a>
## 绑定框架工作树

工作台通过项目 ID 绑定本机工作树，支持从网页或 MCP 新建、导入和切换项目。下面的环境变量可指定启动时可用的默认工作树。

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

## 项目与命名空间

在工作台选「导入已有项目」登记现有 SDK 工作树，或选「新建项目」初始化独立目录。项目登记保存在服务端 `EFFECT_CONSOLE_DIR/projects.sqlite3`。服务可直接启动，已有的 `EFFECT_FRAMEWORK_ROOT`、`EFFECT_CODEGEN` 绑定会作为初始项目显示。

以下路径以 `/api/v1/console` 为前缀，请求须携带允许的工作台 Origin。目录字段使用服务所在机器的绝对路径。

| 请求 | 请求体或结果 |
| --- | --- |
| `GET /projects` | 返回项目 ID、显示名、根目录、工具路径及 `sdkReady`。 |
| `POST /browse` | `{"kind":"directory","initial":"D:/work"}` 打开服务端桌面的系统目录选择器。选择工具时使用 `executable`，取消返回 `{"path":null}`。 |
| `POST /projects/import` | `{"root":"D:/work/existing","name":"业务系统"}`。项目保留 `sdk/Framework/SDK.hs`，工作台根据调用 `interpreter ast effects` 的入口及模块导入识别命名空间和源码目录。可传 `executable` 指定生成器。 |
| `POST /projects` | 只需 `{"root":"D:/work/orders"}`，直接在该目录创建工作区，项目名和首个命名空间均为 `Orders`。可传 `namespace` 指定名称，无法从目录名生成合法模块名时默认 `App`。对同一目录提交 `{"root":"D:/work/orders","namespace":"Billing"}` 会在同一项目添加命名空间，沿用业务源码目录和 SDK，返回原项目 ID 和新命名空间。存在多个源码目录时可指定 `source_dir`。默认解压构建内置 SDK，`sdk_path` 可指定已有 SDK，`install_sdk:false` 可暂缓安装。 |
| `POST /projects/{id}/sdk/build` | 从新项目的内置压缩包补齐缺少的 `sdk/`，执行 `cabal build exe:effect-codegen` 并登记本地工具。 |
| `POST /projects/{id}/namespaces` | `{"namespace":"Payments"}`。沿用已有入口的源码目录。存在多个目录时指定 `source_dir`。创建可编辑的 `Types.hs`、`Effects.hs`、`AST.hs`、`Handlers.hs`、`Main.hs`。已有目录返回 409。 |

图、源码、注册和编译运行均可通过 `/projects/{id}/namespaces/…` 访问。项目中的草稿、编译请求和工作台分析快照按项目区分。模板提供一个 `Start` 节点及 handler；首次生成时由 SDK 写入标准 Generated 模块。新项目的首个命名空间有 Cabal executable，后加的命名空间可由工作台通过现有独立程序构建入口运行。

入口可以单独放在 `app/Main.hs`。直接导入或使用模块别名时，工作台从语法树定位声明，首次生成前也可打开。经过重导出时，工作台通过项目的 HLS cradle 查询 `interpreter`、`ast` 和 `effects` 的定义位置。服务从 PATH 查找 `haskell-language-server-wrapper`，也可通过 `EFFECT_HLS` 指定可执行文件。HLS 需要能加载相关模块，查询结果随业务源码和 cradle 配置的变更失效。

## Facade 编辑与 Gened 审阅

接口前缀为 `/api/v1/console/namespaces/{namespace}`。工作树请求需要工作台的 Origin。绑定的框架和本地构建的 SDK 应当来自同一版本。

| 入口 | 数据来源与用途 |
| --- | --- |
| `GET /facade` | SDK 读取手写注册。响应中的 `graph.cards`、`graph.edges` 和 `graph.handlers` 描述注册、资产、Input、demand 与手写方法绑定。源码定位携带文件指纹和起止行。 |
| `GET /facade/source?file=Effects.hs` | 读取业务 Haskell 模块的原文、定义索引及语法诊断。 |
| `GET /facade/workflow` | 从手写的 `AST.hs:ast` 读取组合与节点，跟随本命名空间的 Workflow 模块引用。返回分支的源码位置标识与版本，供画布通过 `POST /authoring` 追加节点或分支、为分支命名。 |
| `PUT /facade/source?file=Effects.hs` | 以 JSON 字符串提交源码，`If-Match` 使用读取时的文件 SHA-256。冲突返回 409。 |
| `GET /authoring` | 返回手写源码版本、注册、已实现方法、资产表示类型及可编辑分支。 |
| `POST /authoring` | 使用上述版本作为 `If-Match`，按 `kind` 提交 asset、handler 或 workflow。注册时同时新建方法使用 `kind=registration`、Facade 的版本和按方法名索引的 `handlers`。服务在候选副本中运行 SDK 编译器，成功后写回业务文件。 |
| `GET /gened?file=Handlers.hs` | tree-sitter-haskell 解析磁盘上的 Generated 模块，返回原文、模块名、定义行范围、模块内同名引用及语法诊断。 |
| `GET /plan` | tree-sitter-haskell 读取 `Generated/Prototype.hs` 的 effect、方法图、选择分支和资产关系，结合可用的 `Generated/Handlers.hs` 显示实现。已有原型直接返回图，缺文件返回 JSON `null`，语法或结构错误返回 422。 |
| `POST /plan` | 用户点击「生成图分析」时调用。持有命名空间锁期间，缺原型才运行已有的 `effect-codegen compiler Namespace`，再读取生成的 Haskell 文件。 |

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
