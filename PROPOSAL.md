# 框架 LSP 工作提案

本提案沿用 [LSP.md](LSP.md) 的保存检查、显式生成、`planEdits / applyEdits`、`framework/context` 和快照设计。框架规则以本文核对的源码为准。截至 2026-11-21，LSP 同级框架仓库的本地 `main` 已到 `33fd82b`，远端 `main` 为 `dad6458`。下文示例采用本地 `main`；功能分支 `33e88e8` 的三处修正单列在第 5 节。适配器按所配置框架 checkout 的实际 revision 标识结果。[本地合并基线](https://github.com/cartesianc/algebraic-effect-sys-in-haskell/tree/33fd82b) · [后续修复](https://github.com/cartesianc/algebraic-effect-sys-in-haskell/commit/33e88e8)

当前 SDK 提供会写入 `Generated/` 的 `effect-codegen workflow Namespace`。本文的 `read`、只读 `check`、版本化 JSON 导出和 `agentic` 占位仍是拟交付接口。

## 1. 原则

1. **LSP 有一套面向 EDSL 的固定解析协议。** SDK 的 `checkTopLevel` 检查顶层值绑定：`Effects` 中的 `effects`、`AST` 与 `Workflow/*` 中的具名 `AST`、`Hanging` 中的 `hangingTree`、`Main` 中的 `main`。`Types.hs` 还受 `checkFixedForm` 的资产表示规则约束。第 2 节的 v1 协议再规定 LSP 可以逐项编辑的写法。SDK 已接受、超出 v1 编辑范围的源码可只读展示。`Types.hs` 违反固定形式时应展示 SDK 诊断。
2. **Haskell → JSON 是代码可读、可视化的一部分。** 源码是真相，JSON 是它的投影。网页上的 effect 卡片、AST 树、挂载与资产关系都来自这份投影。JSON → Haskell 的生成必须能再读回预期的 JSON。
3. **Python 不重算组合规则。** Prototype、资产目录和契约来自 SDK。Python 做投影、索引、调度和编辑。
4. **LSP 生成的代码不写注释。** 读取器忽略注释。编辑只替换目标实体的源码范围，范围外的手写注释保持原样。
5. **agent 编排暂缓，书写前提先做。** agentic 部分限制在单个方法体，模型不能自己去找上下文或随意书写。所以先把“书写前提”做成可审阅的上下文包和提示词模板，接入 LLM API 的调度、迭代和权限放到后续阶段。

## 2. LSP 解析协议

协议规定 LSP 能逐项解析、定位和改写的写法，带版本号，首版为 v1。其中 `Types.hs` 的固定形式由 gen 强制执行。`Effects.hs` 的列表字面量等约定界定编辑器首版能安全改写的源码范围。读取器接受空白和换行的差异，结构上按下表判定。

| 文件 | 协议 v1 |
| --- | --- |
| `Effects.hs` | `effects :: [EffectSpec]` 的值是列表字面量。每项写成 `EffectSpec { name, typing, input, demands }`，字段齐全。`typing` 是七个构造器之一（`Pure / Excu / Scoped / Error / Context / Stream / Middleware`），字段全部写出。`Maybe` 字段写 `Nothing` 或 `Just "名字"`。资产写 `Asset @T "契约名"`，v1 生成 `String`、`[String]` 或本命名空间的表示类型，如 `OpenFilesDemo` 的 `Asset @[String] "FileNames"`。`RaisedBy` 引用 handler 名，`ScopeFaultIn` 和 `ContextFaultIn` 分别引用 Scoped 和 Context 注册名。Middleware 写 `input = "None"`、`demands = []`，`intercept` 使用允许的类别。demands 只用 `requests / requires / streams "契约名"`。名字都是字符串字面量，没有 `where` 辅助函数。 |
| `AST.hs`、`Workflow/*.hs` | 用 `chained / parallel / alternative / lifelong` 组合具名分支。每个直接子列表全为 `node "X"` 或全为 `AST` 分支。可引用本模块或从 `N.AST`、`N.Workflow.*` 导入的具名绑定。`alternative` 的有序候选保留在源码模型中，判定来源与 case 的对应取自 Prototype（3.2）。 |
| `Hanging.hs` | `hangingTree = hanging [middleware [node "M", ..] [分支, ..], ..]`，目标引用 `N.AST` 或 `N.Workflow.*` 的具名绑定，保留 GHC 定义身份及该绑定在 `ast` 中的全部挂载路径。 |
| `Main.hs` | `main = interpreter ast effects`。 |
| `Types.hs` | 遵守 SDK 固定形式。每个 `data / newtype` 恰好一个前缀或记录构造器，至少一个字段。字段只能写 `String`、`[String]` 或本模块的表示类型。无类型参数、存在量、构造器上下文、严格标记、GADT 或中缀构造器。省略导出表，或用 `T(..)` 导出所有类型与构造器。无 imports、手写 instance、类型同义词或独立 deriving。生成 `deriving (Eq, Show)`，读取器也接受 SDK 已接受的 `(Show, Eq)`。 |
| `Handlers.hs` | 自由区域。协议只规定 instance 与方法的识别，以及方法体中 SDK 类方法的用法抽取（3.4）。 |

### 2.1 代码长什么样

以下节选来自同一个 `LinesDemo`。从 `Effects.hs` 的契约点进 `Types.hs` 可以看到表示类型，从 `streams` 点进 handler 可以区分订阅与历史读取。

```haskell
-- Types.hs
newtype Line = Line String
  deriving (Eq, Show)
```

`Effects.hs` 的 `effects` 列表中有这两项，分别声明生产与订阅。

```haskell
[ EffectSpec
    { name = "LineParser"
    , typing = Context { asset = Asset @Line "TextLine", parser = Just "parseLine", require = True, stream = True }
    , input = "LineFeed"
    , demands = []
    }
, EffectSpec
    { name = "TextCounter"
    , typing = Excu { handler = Just "countText" }
    , input = "Root"
    , demands = [streams "TextLine"]
    }
]
```

```haskell
-- Handlers.hs
instance CountTextLayer m => CountTextHandler m where
  countText = do
    seen <- stream @Line
    rawoutput [show (length seen), show (sum [length text | Line text <- toList seen])]
```

gen 要求持续驱动链上的 Context 声明 `stream = True` 和 `require = True`。来源可以是 Stream 类别的上游，也可以是 Input 链上的一份 `streams` 订阅。普通 Context 的 `require` 字段控制能否被 `requires` 读取。

`PrintLine` 也声明 `streams "TextLine"`，方法体只写 `Line text <- require @Line`。两者都随交付运行；只有 `countText` 读取本条及更早的发布。`CountParser` 把它输出的两个十进制文本解析成 `Total`。`OpenFilesDemo` 的 `FileHandles [String]` 同理只保存路径列表。原生文件句柄的寿命见第 5 节。[LinesDemo 注册](https://github.com/cartesianc/algebraic-effect-sys-in-haskell/blob/33fd82b/demo/LinesDemo/Effects.hs) · [手写 handler](https://github.com/cartesianc/algebraic-effect-sys-in-haskell/blob/33fd82b/demo/LinesDemo/Handlers.hs)

**facade（handler 复用）在协议内的写法。** 多个 effect 可在注册中写同一个 handler 名。`FacadeProbe` 的 `ReportFile` 和 `ReplyFile` 都用 `acquireFile` 作 parser，产生 `OpenedReport` 和 `OpenedReply`。网页按 handler 聚合展示，保留每次绑定各自的读取路由。

### 2.2 编辑、只读与诊断

| 情况 | LSP 的处理 |
| --- | --- |
| 协议内 | 完整解析为 JSON，每个实体有源码范围，可以可视化、补全、重命名和改写。 |
| 协议外、gen 通过 | 用 SDK 的求值结果展示注册和关系，定位至可确认的手写绑定，标为只读。若能保留可观察的注册、具名引用及挂载身份，可提供字面量改写的预览。身份或自由区域无法保留时，仅展示原因。 |
| gen 拒绝 | 按现有诊断报告。 |

例如 `Types.hs` 内的 `instance Show T` 会被 gen 拒绝。`FacadeProbe/Effects.hs` 用手写的 `where` 辅助函数（`context`、`continuous`、`excu` 等）构造注册，属于第二种情况。网页能看到它的全部注册和 facade 关系，但要通过 LSP 修改，需先转换为协议写法。

## 3. 新增与修改的模块

标“SDK”的工作改框架仓库，需要单独确认（见第 5 节）。

### 3.1 读取：Haskell → JSON

| 编号 | 模块 | 内容 | 位置 |
| --- | --- | --- | --- |
| R1 | 声明读取器 | 拟新增 `effect-codegen read Namespace`。用 GHC 9.12 解析声明模块，记录注册、具名 AST、Hanging、固定形式资产及源码范围，返回带 schema 版本的 JSON。用 GHC 的重命名结果解析引用和导入别名。依赖出错时可先返回已解析的范围，未确认链接标明状态。读取过程无需启动求值用的 GHCi。 | SDK 子命令 |
| R2 | 与 SDK 语义对照 | 同一 revision 上对照按序的 `EffectSpec`、从 `N.AST.ast` 展开的工作流，以及每条 Hanging 的 hook 和目标。`loadComposition` 当前经 GHCi 运行 `beginComposition`，`loadHanging` 另用 GHC 定义身份取得目标限定名及全部挂载路径。拟议 `check` 导出这些事实用于对照。语义相等还需单独核对具名引用、重复挂载和未挂载绑定。协议外结构沿用 SDK 的结果展示。 | SDK |
| R3 | Handler 读取与用法索引 | 定位手写 instance、方法及调用范围。文本预检提供即时提示，最终按 GHC 在调用位置解析出的 SDK 方法及其形状确认 `require @T`、`stream @T` 和 `rawinput`，按绑定解析资产路由。见 3.4。 | Python + SDK 导出 |
| R4 | 源码模型与符号索引 | `project/model.py` 分别记录契约名与唯一生产 Context、表示类型的定义身份，以及使用位置的原始类型写法。`project/index.py` 按定义建立 effect、契约、handler、AST 绑定、类型与引用的双向索引。限定名、导入别名和重导出保留各自的源码范围，跨模块同名类型各有独立索引项。 | Python |

网页通过 `framework/model {namespace}` 取得投影。LSP.md 中的 `framework/read {uri}` 继续专指读取文件原文。

### 3.2 Runtime、校验与查询

| 编号 | 模块 | 内容 |
| --- | --- | --- |
| V1 | Runtime 与源码联动 | `scope_tree.py`、`effect_graph.py` 索引同一 revision 的 Prototype。`protoScopeNames` 关联 open 方法与注册名；`effectHistory` 位于产出契约的 Context；方法图上的同一读取边可对应当前值或历史，具体读法见 R3。`AlternativeProto` 提供 `alternativeSource`、`alternativeSelector` 和按声明顺序排列的 `alternativeCases`。实际 scope 实例、选中的发布和 close 时刻只在运行时产生。 |
| V2 | 分阶段诊断 | Python 检查输入 JSON。SDK 先从声明、workflow 和 Hanging 得到候选契约；GHC 用该契约加载手写 Handlers，Shape 提取方法形状，Composition 完成图与来源检查，`checkAssets` 最终联合类检并检查顶层声明和资产固定形式。诊断带来源与 revision，失败后标出尚未到达的阶段。 |
| V3 | 变更影响与差异 | `analysis/impact.py`：比较两个 revision 的 JSON 模型和 Prototype，给出增删改的实体、图上的高亮，以及 Layer 变化、需要重新生成或重写的 handler。`planEdits` 的预览附带这份影响。 |

**`alternative` 的结构与运行。** `FileDemo` 的 `WriteFirstLine` 声明 `requires "FileBlank"`，对应 `BlankText`；`AppendLine` 声明 `requires "FileWritten"`，对应 `FilledText`。两个判定 Context 解析同一次 `ReadFile` 的输出。gen 要求共同 Excu 唯一，每个候选有且只有一个来自该 Excu 的判定 Context，各候选的判定 Context 不同。同一个 Excu 上的多个选择使用相同的判定 Context 顺序。

运行时依序尝试两个 parser，首个成功即选中。判定 parser 的 `raise` 跳过该尝试。若都未得值，选择器按候选顺序检查已有绑定；仍无值且有许可来源时等待发布或进度变化，再按同一顺序检查。已证明来源耗尽且重读仍无值时报 `ContextNotInitialized`。因此两个绑定均已可读时，第一候选优先。不同上游之间的 race 仍由 gen 拒绝。[选择执行](https://github.com/cartesianc/algebraic-effect-sys-in-haskell/blob/33fd82b/sdk/Framework/Runtime/Context.hs) · [FileDemo 生成的选择表](https://github.com/cartesianc/algebraic-effect-sys-in-haskell/blob/33fd82b/demo/FileDemo/Generated/Prototype.hs)

### 3.3 生成：JSON → Haskell

各生成器自己产出所负责实体的源码文本，不另设通用打印层。每份候选补丁先在隔离副本中应用，用 R1（handler 使用 R3）读回可编辑结构，与预期 JSON 比较，再运行拟议的 `check`。比较保留声明顺序、限定引用和自由方法体，源码范围按新文本重新计算。读回不一致时拒绝补丁。

| 编号 | 生成器 | 内容 |
| --- | --- | --- |
| G1 | Effect | LSP.md 表格所列的注册字段，替换或插入单条注册，维护 imports。 |
| G2 | 资产 | 一次编辑包含表示类型、唯一生产 Context 和 parser 骨架。可直接注册 `String`、`[String]`，或按定义身份复用本命名空间的类型。两个契约可以使用同一表示类型，每个 effect 的该表示类型须能路由到唯一契约。生成 `Types.hs` 时维护导出及 `deriving (Eq, Show)`；持续资产仍注册元素 T。 |
| G3 | AST | 支持两种组织方式，由网页选择。**不模块化**：全部具名绑定写在 `AST.hs`。**模块化**：选定的绑定写入 `Workflow/X.hs`，`AST.hs` 导入并引用。另提供“抽出为模块”和“合并回 AST.hs”两个编辑操作，同时更新 Hanging 对该分支的导入。 |
| G4 | Hanging | 中间件 effect 与目标分支，分支用具名绑定的限定名。 |
| G5 | Handler | 写 instance 头和方法。方法体来自用户实现或 agentic 委派。委派元数据在 LSP 侧以 namespace 与方法名为键。约定的源码占位是 `agentic (Agentic "任务键")`，依赖 SDK 增加该入口，见第 5 节。 |
| G6 | 脚手架 | 新建命名空间的 Types、Effects、AST、Main 和空 Handlers，以及 cabal executable 片段。cabal 片段以普通补丁预览，用户确认后应用。 |

对于含自由表达式的手写源码，补丁只涉及能定位且能验证的区域。`Effects.hs` 的局部 helper 可在注册语义对照通过后转换为字面量，保留用户确认预览的步骤。

### 3.4 handler 类方法用法检查

方法体保留原文。R3 可在语法或导入还不完整时，用正则扫描给出临时提示。注释、字符串、同名局部绑定和 import 别名都会影响按拼写判定的结果。已加载的代码以 `handlerNames` 的 GHC 名字身份为准，结构规则复用 `shapeHandlers` 和 `composeMethods`。

| 类方法 | 抽取 | 检查 |
| --- | --- | --- |
| `require @T` | 表示类型 T、当前 effect 绑定、契约 | `requires` 或 `streams` 提供 `Reads T m`。普通资产优先使用本次捕获，否则读取共享的最新发布，缺值走等待／耗尽协议。持续资产读固定交付或有限快照的当前 T。`requests` 只驱动生产。每个 `requires`／`streams` 的读取依据由 SDK 汇总该 effect 的方法和控制流。 |
| `stream @T` | 表示类型 T、持续契约、生产 Context | 仅在以 `streams` 读取该持续资产的绑定上提供 `Streams T m`，返回 `Flow T`。`x :> xs` 的头是与 `require @T` 一致的本次值，尾部为同一会话自开始以来更早发布的值，最新在前，包含本读取方登记前的发布。gen 从已解析的方法体识别历史读取，按绑定解析契约，在生产 Context 上设置 `effectHistory = True`。 |
| `rawinput` | 实际绑定与直接前驱 | 挂接的 Context parser 或有合法 Input 的 Pure 可读本次原始输出。若上游是 Stream，单次 `rawemit` 的输入以单元素列表交付。 |
| `rawemit`、`streamEnd` | 生产与封口的位置 | Stream 可逐条交付。有限来源耗尽时调用 `streamEnd`，正常返回可继续下一步。持续运行的来源（例如 `StreamDemo.nextReading`）允许持续发布，仅凭未找到 `streamEnd` 不产生缺失诊断。 |
| `endStream` | 订阅退出位置 | 给自身通过 `streams` 订阅的 Excu，结束自己的订阅。来源封口或退役等停止条件满足、读者全部离开后可回收会话历史，保留最新值供后续有限读取。 |
| `hook` | Middleware 绑定、Hanging 范围、`intercept` | `hook :: m Operation` 提供 `Enter`、`RawInput`、`RawOutput`、`RawEmit`、`Raise`。`TraceLine` 只选 `StreamKind`、`ScopedKind`；方法里的通配模式也受挂载范围和类别约束。`allEffects` 当前展开为 Excu、Scoped、Pure、Stream 四类。 |
| `raise` | 构造器 | 应使用本 handler 的错误类型。 |
| `rawoutput` | 方法与输出位置 | 按 Layer 记录原子输出。查询下游时从 SDK 装配找出零个或多个 parser，以及经 Pure 转换的路径。 |

`requires` / `streams` 的读取依据、Scoped close 对资源的读取、`stream @T` 的权限由 SDK 按真实绑定检查。`alternative` 选择和持续交付可以构成控制流读取依据。文本预检的命中不能替代这些判定，也不能据此将正式错误降为警告。`effectHistory` 汇总所有实际绑定的方法体，只要有一个能静态识别的 `stream @T` 读取就置位。取得当前交付可能等待；遍历这份固定前缀不会请求下一条。会话停止且读者全部离开后释放历史，有发布的会话仍保留最新值供合法后继读取。R3 对 `rawemit`、结束和 hook 的调用位置可另建源码索引；Shape 当前只把 `require`、`stream`、`rawinput` 投影为这类方法调用边。流结束后的历史再读取、未来尾部和背压仍属后续设计。

### 3.5 agentic 书写前提

agentic 限制在单个方法体，模型拿到的就是它能知道的全部。这一模块产出两件可审阅的东西：上下文包，以及由它填充的提示词。提示词的措辞由你完善，结构固定如下。

| 部分 | 内容与来源 |
| --- | --- |
| 任务 | namespace、handler 方法名、它绑定的全部 effect 及类别（R4、`unitBindings`）。 |
| 签名与可用操作 | 从 `unitSignatures` 和本次绑定的 Layer 列出方法结果类型及可用操作，包括 `require @T`、有 `Streams T m` 时的 `stream @T` 与 `Flow T`、`rawinput`、`rawemit`、两个不同的结束操作、`hook`、`rawoutput`、本 handler 的 `raise`，以及具备 `MonadIO` 时的 `liftIO`。每次绑定单列契约路由，能力取自当前 checkout 的真实 Layer；`MonadMask` 的版本差异见第 5 节。 |
| 数据形状 | 可读资产的表示类型、构造器、字段和方法结果类型。例如 `parseTotal` 用两个十进制文本构造 `Total`。历史视图 `Flow T` 属于读取结果，资产声明仍写元素表示类型 T。 |
| 上下游关系 | 按每个绑定列出直接原始输入来源、下游 parser 或 Pure 链，以及同一资产的其他读取方。当有多个消费者时逐一列出访问方式。对已实现且与任务相关的端点附方法体原文，说明字符串格式。 |
| 类别义务 | 按类别与绑定列出行为。有限 Stream 在来源耗尽时 `streamEnd`。有资源的 Scoped 要在 close 方法本身读取该资源的表示类型。Middleware 可通过 `hook` 查看本次操作。 |
| 书写限制 | 只返回方法体。可用 imports 为该模块已有的 imports 加一份白名单。不新增顶层定义（SDK 会拒绝）。 |
| 输出契约 | 仅交回方法体文本。LSP 将它放回 instance，在候选工作副本中运行 R3 和 SDK 检查，GHC 或框架诊断可作为下一轮输入。 |

位置是 `agent/context.py`（组包）和 `agent/prompt.py`（模板与版本）。这一阶段只提供 `framework/agenticContext {namespace, method}` 查询，返回上下文包和填好的提示词，用于审阅和调试。不调用 LLM API。

### 3.6 编辑与辅助

| 编号 | 模块 | 内容 |
| --- | --- | --- |
| E1 | 编辑事务 | 沿用 LSP.md 的 `applyEdits`。补充撤销：每次成功的事务记录反向补丁，最近一次可撤回，撤回本身也是一次事务。 |
| E2 | 补全 | 按 R4 和 SDK 注册角色过滤。`input` 提供合法前驱及适用的 `Root` / `None`，`streams` 提供持续资产，`requests` 只提供可被请求的普通资产。方法体按真实 Layer 区分可 `require @T` 与可 `stream @T` 的类型。 |
| E3 | 重命名 | effect、契约、handler、AST 绑定，生成一组 `planEdits` 跨文件一次应用，同时迁移 agentic 标记的键。 |
| E4 | 快速修复 | 为未绑定的 handler 生成骨架、为缺生产方的契约生成 Context、补 import、AST 抽出或合并模块。 |

### 3.7 预留位置

`runtime/mock.py`、`runtime/trace.py`（运行程序、收集 trace、把 scope 实例叠加到静态树）、`agent/runner.py`（LLM API 调用、迭代与权限边界）。只建位置，不注册请求。

## 4. 分期与完成条件

验收在本机运行，结果在对话中报告，不在仓库中保存验收记录。

**阶段 A：读得懂。** R1–R4、V1、V2，加上 LSP.md 第一步。

- 读取现有 8 个命名空间。7 个 demo 有 Cabal 可执行目标，`FacadeProbe` 独立检验手写复用场景。协议内的声明按 R2 对照，`FacadeProbe/Effects.hs` 的 helpers 只读显示，能区分 `acquireFile` 的两次绑定：在 `ReportFile` 中 `require @String` 指向 `ReportPath`，在 `ReplyFile` 中指向 `ReplyPath`。
- 打开 `LinesDemo`，能看到 `ast`、`monitorLines`、未挂载的 `snapshotAst`，以及挂在 `monitorLines` 上的 `TraceLine`。`InputSession` 仍是 scope 根。当前方法图含 `RunTextCounterGen → StreamTextLineGen`，R3 将此读取标为 `stream @Line`。`Crawler` 中的 `parsePages`、`printPages` 可分别跳到 `Workflow/` 下的定义。
- `report` 列出 `require @Total`，`countText` 列出 `stream @Line`，`LineParser` 的 `effectHistory = True`。在候选副本中把 `report` 的方法体换成 `rawoutput []`，并保留 `Report` 原有的 `requires "RunningTotal"`。GHC 仍能给方法体定型，SDK 应指出该 effect 声明的资产没有读取依据。恢复方法体后诊断消失。
- `OpenFilesDemo` 读回 `Asset @[String] "FileNames"` 与 `Asset @FileHandles "OpenedFiles"`；前者标明内建表示，后者可跳转到本模块的 `FileHandles` 定义。`FileDemo` 的 option 视图显示来源 `ReadFile` 和按序的 `BlankText`、`FilledText` 两个判定 Context。
- 把 `LinesDemo.AST.ast` 中的 `node "Report"` 改成 `node "Reprot"`，保存后得到框架的未知目标错误。备用导出 `snapshotAst` 保留原文。

**阶段 B：写得出。** G1–G6、E1–E4，加上 LSP.md 第二、三步。

- 在 `LinesDemo` 用一组 `planEdits` 新增资产（表示类型 + Context + parser）和一个读取它的 Excu，预览给出多文件 diff，读回与预期 JSON 一致，SDK 支持 agentic 占位后，查询应列出准确的待填方法和仍适用的独立错误。补齐方法并通过完整检查后，才将实现标为就绪。
- 把 `monitorLines` 抽出为 `Workflow/MonitorLines.hs` 再合并回来，两次的 `check` 都通过，Hanging 的导入随之更新。
- 重命名 `TextLine` 后 `check` 通过，撤销后恢复原状。
- 用脚手架新建一个命名空间，生成、构建、运行都通过。

**阶段 C：书写前提与变化。** 3.5、V3，3.7 只建位置。

- 查询 `LinesDemo.parseTotal` 的 agentic 上下文，应看到 `Total String String` 的定义、`TextCounter` 绑定的 `countText` 以 `stream @Line` 统计并 `rawoutput` 两段文本的实现，以及 `RunningTotal` 的两个消费方：`ShowTotal` 声明 `streams`、`Report` 声明 `requires`，二者的方法体当前均使用 `require @Total`。默认 `monitorLines` 内，`CountParser` 复用 `TextCounter` 的同次输出（`ReuseOutput`）。若按示例一起切换 `ast` 和 Hanging 目标为 `snapshotAst`，有限的 `Report` 读取可启动独立的 `Derive "TextLine"`，`Report` 自身仍只执行一次。
- 在候选补丁中把 `countText` 从 `stream @Line` 改为 `require @Line`，并将统计逻辑改为只处理本行。预览应显示 `LineParser.effectHistory` 取消，读取方法边仍为 `RunTextCounterGen → StreamTextLineGen`。若再单独将 `TextCounter` 的声明改为 `requires`，当前 workflow 会失去这个 target 的持续驱动，`CountParser` 的 `stream = True` 也会失去依据。预览须显示当前 revision 的 SDK 诊断，把上次成功的图标为旧快照。

## 5. 已定规则、版本差异与待交付接口

1. **资产固定形式已定。** 以第 2 节的形状生成和编辑 beforegen 源码。`Asset @FilePath` 与 `Asset @String` 表示同一 Haskell 类型；`Types.hs` 的字段按源码检查，须写 `String`。在 `Types.hs` 增加手写 instance、`Handle` 字段或和类型时，展示 gen 诊断。源码占位与脚手架也遵守此规则。[资产规范](https://github.com/cartesianc/algebraic-effect-sys-in-haskell/blob/33fd82b/docs/ASSETS.md#fixed-form)
2. **Effects 的局部 helper 仍合法。** gen 接受 `effects` 的 `where`。`FacadeProbe/Effects.hs` 保留其手写形式，LSP v1 按协议外的只读注册展示。后续可在新协议版本中加入固定形状的 helper。
3. **`33e88e8` 已修复的三个边界。** 以下用例在包含该提交的 checkout 上验收。`33fd82b` 上已经定下相同的资产契约，但生成器对同名类型仍有漏检，示例和 runtime 也有对应差异。[修复提交](https://github.com/cartesianc/algebraic-effect-sys-in-haskell/commit/33e88e8)

   | 涉及模块 | 可核对的行为 |
   | --- | --- |
   | 注册、Check.Assets 与生成的 Prototype | 在 FileDemo 注册 `ScopeInstancesDemo.Types.FileHandle` 会被 gen 拒绝，即使本地也有同名、同字段的 `FileHandle`。改为经别名或重导出引用 `FileDemo.Types.FileHandle` 则通过。DTO 分开保留源码拼写和由定义单元、模块、类型名确定的身份。旧版只按短名核对注册，LSP 应如实标注检查版本。 |
   | Stream Layer 与 `LinesDemo.nextLine` | 该版本的 Stream Layer 提供 `MonadMask`。`nextLine` 用 `Control.Monad.Catch.bracket` 包住文件打开、逐行 `rawemit` 和释放；EOF、解析失败或取消后文件均可重新打开。`33fd82b` 的示例只在 EOF 分支显式 `hClose`。`InputSession.close` 是 scope 回调，两者在上下文页分别列出。 |
   | Session、执行交接与 scope 待用 | `stream @T` 的历史仍可读。判断本次 scope 开启参数及执行交接时只取当前 Binding 的生产身份，尚未领取的交付及伴随绑定按自己的义务保留。图页展示静态计划；若以后接入 trace，再叠加实际持有和 close。 |

4. **Agentic 占位的表示已选定，SDK 接口待实现。** 约定在 SDK 的 handler 门面导出以下类型与函数。占位不使用源码注释。

   ```haskell
   newtype Agentic = Agentic String

   agentic :: Agentic -> a
   agentic (Agentic task) = error ("agentic method not written yet: " ++ task)
   ```

   目标写法是 `report = agentic (Agentic "LinesDemo.report")`。任务说明和提示词由 LSP 保存。适配器应按 SDK 的绑定身份和任务键识别占位，报告“等待填写”。这个表达式在导出后可有预期的 Haskell 类型，执行到占位仍会失败。未填写的方法没有真实读取形状，依赖方法体的检查应标为待完成，其他可检查的错误仍须报告。
5. **需要单独确认实施的 SDK 工作。** 新增 `read`、只读 `check`、R2 所需的语义对照和 Prototype 等结构化输出，以及 `Agentic` 的导出与占位识别。当前生成命令仍是 `effect-codegen workflow Namespace`，它会写入 Generated 区。
