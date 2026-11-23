"""MCP tools share the console's request models, project binding and verified writes."""

import asyncio
from dataclasses import dataclass
from ipaddress import ip_address
import json
import os
from urllib.parse import quote

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.responses import PlainTextResponse

from . import codex, model_settings
from .metadata import CardAlias, MAX_ALIAS_LENGTH
from .prompt import HandlerPrompt, NodeMetadataPrompt, WorkbenchPrompt
from ...config import get_app_config


P = "/projects/{project_id}"
N = P + "/namespaces/{namespace}"


@dataclass(frozen=True)
class Tool:
    name: str
    method: str
    path: str
    description: str
    body_model: str = ""
    preset: tuple = ()  # fixed body or route parameters for this operation


TOOLS = [
    Tool("list_agent_models", "get", "/agent/models", "列出服务端 Codex 提供的 GPT 模型及其推理强度。"),
    Tool("read_agent_model", "get", "/agent/models/{purpose}", "读取翻译或 Handler 生成当前使用的模型和推理强度。"),
    Tool("set_agent_model", "put", "/agent/models/{purpose}", "设置翻译或 Handler 生成使用的模型和推理强度，对该服务的所有项目生效。"),
    Tool("list_projects", "get", "/projects", "列出已绑定的项目及 SDK 状态。"),
    Tool("import_project", "post", "/projects/import", "按绝对路径导入已有项目。"),
    Tool("create_project", "post", "/projects", "在指定目录创建项目和初始 Workflow。", "NewProject"),
    Tool("browse_path", "post", "/browse", "在服务所在桌面打开目录或工具选择器。"),
    Tool("build_sdk", "post", P + "/sdk/build", "构建项目 SDK。"),
    Tool("list_namespaces", "get", P + "/namespaces", "从工作流入口发现命名空间。", preset=(("workflow", True),)),
    Tool("create_namespace", "post", P + "/namespaces", "创建含 main 和根 Workflow 的命名空间。"),
    Tool("read_snapshot", "get", N, "读取已发布的命名空间分析快照。"),
    Tool("read_facade", "get", N + "/facade", "读取声明图、Effect 注册和注册写入所需的 revision。"),
    Tool("read_source", "get", N + "/facade/source", "读取手写模块、语法信息及 revision。"),
    Tool("save_source", "put", N + "/facade/source", "按文件 revision 保存手写模块。"),
    Tool("read_generated_source", "get", N + "/gened", "读取已生成的 Haskell 模块。"),
    Tool("read_effect_graph", "get", N + "/plan", "从 Prototype.hs 读取生成图。"),
    Tool("generate_effect_graph", "post", N + "/plan", "使用项目 SDK 补齐生成图。"),
    Tool("read_workflow", "get", N + "/facade/workflow", "读取手写 Workflow 和各分支的编辑目标。"),
    Tool("analyze_workflow", "get", N + "/workflow", "读取生成的 Workflow 报告。"),
    Tool("optimize_workflow", "post", N + "/workflow/optimize", "生成 AST 优化报告。"),
    Tool("compile", "post", N + "/commands/{action}", "生成并编译命名空间。", preset=(("action", "compile"),)),
    Tool("run", "post", N + "/commands/{action}", "编译并执行命名空间，返回命令输出。", preset=(("action", "run"),)),
    Tool("authoring_context", "get", N + "/authoring", "读取可用 Effect、Handler、类型、分支和编辑 revision。"),
    Tool("create_effect", "post", N + "/registrations", "注册 Effect。body 使用 schemaVersion 和 effect，revision 来自 read_facade。"),
    Tool("create_effect_with_handlers", "post", N + "/authoring", "同时注册 Effect 和新 Handler。revision 来自 read_facade。", "Registration", (("kind", "registration"),)),
    Tool("save_handler", "post", N + "/authoring", "新建、绑定或更新 Handler。", "Handler", (("kind", "handler"),)),
    Tool("create_asset", "post", N + "/authoring", "创建资产及其生产注册。", "Asset", (("kind", "asset"),)),
    Tool("add_workflow_node", "post", N + "/authoring", "向已有 Workflow 分支添加 Effect 引用。", "WorkflowEdit", (("kind", "workflow"), ("action", "append"))),
    Tool("create_workflow_branch", "post", N + "/authoring", "在 Workflow 建立可命名的组合分支。", "WorkflowEdit", (("kind", "workflow"), ("action", "append_branch"))),
    Tool("name_workflow_branch", "post", N + "/authoring", "命名 Workflow 分支，可抽取单独模块。", "WorkflowEdit", (("kind", "workflow"), ("action", "name"))),
    Tool("draft_handler", "post", N + "/authoring/handler-draft", "从源码上下文生成可编辑的 Handler 草稿。"),
    Tool("read_handler_context", "post", N + "/authoring/handler-context", "读取指定 Handler 的关系图、方法契约和可用的 SDK 类方法。"),
    Tool("inspect_symbol", "get", N + "/facade/hover", "按手写文件及零基 UTF-16 位置请求 HLS 类型信息。"),
    Tool("list_node_aliases", "get", N + "/aliases", "读取命名空间中已保存的节点别名。"),
    Tool("read_node_metadata", "get", N + "/nodes/{name}/metadata", "读取节点别名、说明及人工编辑状态。"),
    Tool("edit_node_metadata", "patch", N + "/nodes/{name}/metadata", "编辑卡片译文或设置 locked。资产用 asset:契约名 定位。"),
    Tool("generate_node_metadata", "post", N + "/nodes/{name}/metadata", "参考人工措辞重译一张未锁定卡片。资产用 asset:契约名 定位。"),
    Tool("list_card_translations", "get", N + "/translations/cards", "按卡片标识读取已有别名、翻译锁及编辑版本。"),
    Tool("translation_status", "get", N + "/translations", "读取命名空间翻译队列状态。"),
    Tool("regenerate_namespace_translations", "post", N + "/translations", "通过 Codex 批量重新生成当前命名空间的自动翻译。"),
]


class GeneratedNode(BaseModel):
    name: str
    alias: CardAlias
    description: str = Field(min_length=1, max_length=1200)


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


class WorkbenchMCP:
    handler_context = staticmethod(HandlerPrompt.prepare)

    def __init__(self, app):
        self.app = app
        self.prefix = get_app_config().api_prefix + "/console"
        self.origins = [v.strip() for v in os.getenv("EFFECT_CONSOLE_ORIGINS",
                        "http://127.0.0.1:3000,http://localhost:3000").split(",") if v.strip()]
        self.server = Server("aged-donkey", instructions="先读取项目、命名空间和当前 revision，再提交修改。")
        security = TransportSecuritySettings(allowed_hosts=["localhost:*", "127.0.0.1:*", "[::1]:*"],
                                             allowed_origins=self.origins)
        self.transport = StreamableHTTPSessionManager(self.server, stateless=True, json_response=True,
                                                     security_settings=security)
        self.server.list_tools()(self.list_tools)
        self.server.call_tool()(self.call_tool)
        self.server.list_prompts()(self.list_prompts)
        self.server.get_prompt()(self.get_prompt)

    async def __call__(self, scope, receive, send):
        peer = scope.get("client")
        try:
            local = bool(peer) and ip_address(peer[0]).is_loopback
        except ValueError:
            local = False
        if not local:
            return await PlainTextResponse("Local MCP only", 403)(scope, receive, send)
        await self.transport.handle_request(scope, receive, send)

    def operation(self, spec):
        return self.app.openapi()["paths"][self.prefix + spec.path][spec.method]

    def schema(self, spec):
        operation = self.operation(spec)
        models = self.app.openapi()["components"]["schemas"]
        fixed = dict(spec.preset)
        def expand(value):
            if isinstance(value, list):
                return [expand(v) for v in value]
            if isinstance(value, dict):
                if "$ref" in value:
                    return expand(models[value["$ref"].split("/")[-1]])
                return {k: expand(v) for k, v in value.items()}
            return value

        fields, required = {}, []
        for p in operation.get("parameters", []):
            key = "revision" if p["in"] == "header" else p["name"]
            if key not in fixed:
                fields[key] = expand(p["schema"])
                if p.get("required"):
                    required.append(key)
        if content := operation.get("requestBody"):
            body = expand(models[spec.body_model] if spec.body_model else content["content"]["application/json"]["schema"])
            for key in fixed:
                body.get("properties", {}).pop(key, None)
            if "required" in body:
                body["required"] = [k for k in body["required"] if k not in fixed]
            if spec.name == "create_effect":
                body["examples"] = [{"schemaVersion": 1, "effect": {"name": "Report", "input": "Root",
                                    "typing": {"kind": "Excu", "handler": None}, "demands": []}}]
            fields["body"] = body
            if content.get("required"):
                required.append("body")
        return {"type": "object", "properties": fields, "required": required, "additionalProperties": False}

    async def list_tools(self):
        return [types.Tool(name=s.name, description=s.description, inputSchema=self.schema(s)) for s in TOOLS]

    async def list_prompts(self):
        prompts = []
        for spec in TOOLS:
            arguments = [types.PromptArgument(name=p["name"], required=True)
                         for p in self.operation(spec).get("parameters", []) if p["in"] == "path"
                         and p["name"] not in dict(spec.preset)]
            if spec.name in {"save_source", "read_source", "read_generated_source", "inspect_symbol"}:
                arguments.append(types.PromptArgument(name="file", required=spec.name in {"save_source", "inspect_symbol"}))
            if spec.name == "inspect_symbol":
                arguments.extend(types.PromptArgument(name=key, required=True) for key in ("line", "character"))
            if spec.name in {"draft_handler", "read_handler_context", "save_handler", "create_effect_with_handlers"}:
                focus = spec.name in {"draft_handler", "read_handler_context"}
                arguments.extend(types.PromptArgument(name=key, required=focus) for key in ("effect", "role", "method"))
                arguments.append(types.PromptArgument(name="proposed_effect", description="新注册的 EffectSpec（JSON）"))
            arguments.append(types.PromptArgument(name="instruction", required=True))
            prompts.append(types.Prompt(name=spec.name, description=WorkbenchPrompt.operations[spec.name][1], arguments=arguments))
        return prompts

    async def get_prompt(self, name, arguments):
        spec = next(s for s in TOOLS if s.name == name)
        args = dict(arguments or {})
        instruction = args.pop("instruction")
        readers = WorkbenchPrompt.operations[name][0].split()
        if name in {"save_handler", "create_effect_with_handlers"} and any(k in args for k in ("effect", "role", "method", "proposed_effect")):
            readers.append("read_handler_context")
        context = {}
        for reader in readers:
            parameters = dict(args)
            if reader == "read_handler_context":
                if not all(args.get(k) for k in ("effect", "role", "method")):
                    raise ValueError("读取方法上下文需要 effect、role 和 method。")
                body = {k: args[k] for k in ("effect", "role", "method")}
                if args.get("proposed_effect"):
                    body["proposed_effect"] = json.loads(args["proposed_effect"])
                parameters["body"] = {**body, "instruction": instruction}
            data = await self.call_tool(reader, parameters)
            if reader == "list_projects" and "project_id" in args:
                data = [p for p in data["result"] if p["id"] == args["project_id"]]
            context[reader] = data
        text = WorkbenchPrompt.prepare(name, self.schema(spec), context, instruction)
        return types.GetPromptResult(messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))])

    async def call_tool(self, name, arguments):
        spec = next(s for s in TOOLS if s.name == name)
        fixed = dict(spec.preset)
        args = {**arguments, **fixed}
        path = self.prefix + spec.path
        headers, query = {"Origin": self.origins[0], "Accept": "application/json"}, {}
        for p in self.operation(spec).get("parameters", []):
            key = p["name"]
            if p["in"] == "path":
                value = args[key]
                if not value or value in (".", "..") or any(c in value for c in "/\\%?#"):
                    raise ValueError("非法路径参数")
                path = path.replace("{" + key + "}", quote(value, safe=""))
            elif p["in"] == "header":
                headers[key] = args["revision"]
            elif key in args:
                query[key] = args[key]
        body = args.get("body")
        if spec.body_model and fixed:
            body = {**body, **fixed}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1") as client:
            async with asyncio.timeout(600):
                response = await client.request(spec.method, path, params=query, headers=headers, json=body)
        if response.is_error:
            raise ValueError(f"HTTP {response.status_code} {response.text}")
        result = response.json()
        if name in {"compile", "run"} and result["exitCode"] != 0:
            raise ValueError(json.dumps(result, ensure_ascii=False))
        if isinstance(result, dict) and result.get("error") and name == "draft_handler":
            raise ValueError(result["error"])
        return result if isinstance(result, dict) else {"result": result}

    @staticmethod
    async def generate_metadata(job):
        tool = await asyncio.to_thread(codex.command)
        if not tool:
            raise ValueError("服务端未找到 Codex CLI。")
        name = job.context["name"]
        is_asset = "asset" in job.context
        title = {"type": "string", "maxLength": MAX_ALIAS_LENGTH}
        fields = {"noun": title, "qualifier": title} if is_asset else {"alias": title}
        schema = object_schema({"name": {"type": "string", "enum": [name]}, **fields, "description": {"type": "string"}})
        prompt = await asyncio.to_thread(NodeMetadataPrompt.prepare, job)
        selection = await asyncio.to_thread(model_settings.current, "translation")
        result = await codex.generate(tool, NodeMetadataPrompt.instructions(job.context), prompt, schema,
                                      model=selection.model, reasoning_effort=selection.reasoning_effort)
        if is_asset and isinstance(result, dict):
            noun, qualifier = result.get("noun"), result.get("qualifier")
            if not isinstance(noun, str) or not noun.strip() or not isinstance(qualifier, str):
                raise ValueError("资产译文缺少中心词或定语。")
            qualifier = qualifier.strip().removesuffix("的")
            result["alias"] = f"{qualifier}的{noun.strip()}" if qualifier else noun.strip()
        generated = GeneratedNode.model_validate(result)
        if generated.name != name:
            raise ValueError("译文与请求的卡片不一致。")
        return generated

    @staticmethod
    async def generate_handler(tool, prompt, metadata):
        selection = await asyncio.to_thread(model_settings.current, "codegen")
        result = await codex.generate(tool, HandlerPrompt.instructions, prompt, object_schema({
            "body": {"type": "string"}, "problem": {"type": "string", "description": "成功时为空，否则说明缺少的证据。"}}),
            model=selection.model, reasoning_effort=selection.reasoning_effort)
        body = result.get("body")
        if result.get("problem") or not isinstance(body, str) or not body.strip():
            raise ValueError(result.get("problem") or "Codex 未返回有效的方法体。")
        return {"body": body.strip(), **metadata}
