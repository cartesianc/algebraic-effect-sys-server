import asyncio
import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, WebSocket, WebSocketException
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from ...config import get_app_config
from ...lsp.analysis.facade import facade
from ...lsp.analysis.prototype import read_prototype
from ...lsp.analysis.sources import namespace_folder, save_facade, source_document
from ...lsp.analysis.source_workflow import source_workflow
from ...lsp.codegen.registration import add_registration
from ...lsp.codegen.authoring import Asset, Handler, Registration, WorkflowEdit, editor, fingerprint, save_edit, source_files
from ...lsp.framework import Framework
from ...lsp.editor import editor_session
from ...lsp.project.entries import entries, entry_for
from ...lsp.project.model import constructor_name
from ...lsp.project.picker import browse
from ...lsp.project.scaffold import create_namespace
from ...lsp.project.workspaces import absolute, build_sdk, create_in_folder, import_project, lookup, new_project, project_target, projects, startup_project
from ...lsp.runtime.commands import command_output, generate_graph
from ...lsp.runtime.snapshot import RegistrationConflict, console_home, directory, project_directory
from ...lsp.runtime.workflow import AstLoadError, generated_workflow, optimize_workflow


def check_browser_origin(request: HTTPConnection) -> None:
    origin = request.headers.get("origin")
    allowed = os.environ.get(
        "EFFECT_CONSOLE_ORIGINS", "http://127.0.0.1:3000,http://localhost:3000"
    )
    origins = {entry.strip() for entry in allowed.split(",") if entry.strip()}
    if request.scope["type"] == "websocket" and origin not in origins:
        raise WebSocketException(code=1008)
    if origin and origin not in origins:
        raise HTTPException(403, "Origin cannot read console snapshots")


router = APIRouter(
    prefix=f"{get_app_config().api_prefix}/console",
    tags=["Console"],
    dependencies=[Depends(check_browser_origin)],
)
workspace = APIRouter()


def require_origin(request: Request):
    if not request.headers.get("origin"):
        raise HTTPException(403, "此操作需要工作台的 Origin。")


class ImportProject(BaseModel):
    root: str
    name: str = ""
    executable: str | None = None


class TemplateProject(ImportProject):
    source_dir: str = "src"
    sdk_root: str
    namespace: str


class NamedProject(BaseModel):
    parent: str
    name: str
    install_sdk: bool = True
    sdk_path: str | None = None


class NewProject(BaseModel):
    root: str
    namespace: str | None = None
    install_sdk: bool = True
    sdk_path: str | None = None
    source_dir: str | None = None


@router.get("/projects", dependencies=[Depends(require_origin)])
def list_projects():
    return projects()


@router.post("/projects/import", dependencies=[Depends(require_origin)])
def register_project(body: ImportProject):
    return workbench_call(import_project, body.root, body.name, body.executable)


@router.post("/browse", dependencies=[Depends(require_origin)])
def browse_local(kind: Literal["directory", "executable"] = Body(), initial: str = Body("")):
    return workbench_call(browse, kind, initial)


@router.post("/projects", status_code=201, dependencies=[Depends(require_origin)])
def initialize_project(body: NewProject | TemplateProject | NamedProject):
    if isinstance(body, NewProject):
        return workbench_call(create_in_folder, body.root, body.namespace, body.install_sdk, body.sdk_path, body.source_dir)
    if isinstance(body, NamedProject):
        target = workbench_call(project_target, body.parent, body.name)
        return workbench_call(new_project, target, body.name, "App", body.sdk_path, None, "src",
                              body.install_sdk or bool(body.sdk_path))
    return workbench_call(new_project, workbench_call(absolute, body.root), body.name, body.namespace,
                          body.sdk_root, body.executable, body.source_dir, True)


async def bind_project(project_id: str, request: Request):
    require_origin(request)
    project = workbench_call(lookup, project_id)
    request.state.project = project
    default = startup_project()
    dest = console_home()
    if not default or default.id != project.id:
        dest = dest / "workspaces" / project.id
    token = project_directory.set(dest)
    try:
        yield
    finally:
        project_directory.reset(token)


def project_root(request: Request):
    project = getattr(request.state, "project", None)
    return project.root if project else os.getenv("EFFECT_FRAMEWORK_ROOT")


@router.websocket("/projects/{project_id}/namespaces/{namespace}/hls")
async def handwritten_editor(ws: WebSocket, project_id: str, namespace: str):
    try:
        project = await asyncio.to_thread(lookup, project_id)
        root = Path(project.root).resolve()
        folder = await asyncio.to_thread(namespace_folder, root, namespace)
    except (OSError, ValueError):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        await editor_session(ws, root, folder)
    finally:
        if ws.client_state.name == "CONNECTED" and ws.application_state.name == "CONNECTED":
            await ws.close()


@router.post("/projects/{project_id}/sdk/build", dependencies=[Depends(bind_project)])
def build_project_sdk(request: Request):
    return workbench_call(build_sdk, request.state.project)


@workspace.get("/namespaces")
def namespaces(request: Request, workflow: bool = False) -> list[str]:
    names = {path.stem for path in directory().glob("*.json") if path.is_file()}
    if workflow:
        require_origin(request)
        if root := project_root(request):
            workspace = Path(root).resolve()
            names = set(workbench_call(entries, workspace))
    return sorted(names)


@workspace.get("/namespaces/{namespace}")
def snapshot(namespace: str) -> FileResponse:
    if not published(namespace):
        raise HTTPException(404, "Namespace has no published snapshot")
    return FileResponse(
        directory() / (namespace + ".json"),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def published(namespace: str) -> bool:
    return (all(map(constructor_name, namespace.split(".")))
            and (directory() / (namespace + ".json")).is_file())


def bound_framework(request: Request, namespace: str) -> Framework:
    require_origin(request)
    if not all(map(constructor_name, namespace.split("."))):
        raise HTTPException(404, "无效的命名空间。")
    root = project_root(request)
    project = getattr(request.state, "project", None)
    sdk = project.executable if project else os.getenv("EFFECT_CODEGEN")
    if project and not Path(sdk).is_file():
        raise HTTPException(503, "该项目尚无可用的 effect-codegen，请在项目面板构建 SDK 或导入时指定工具。")
    if not root or not sdk:
        raise HTTPException(503, "后端尚未配置 EFFECT_FRAMEWORK_ROOT 和 EFFECT_CODEGEN。")
    folder = Path(root).resolve()
    entry = workbench_call(entry_for, folder, namespace)
    return Framework(Path(sdk).resolve(), folder, entry.source_dir)


def workbench_call(fn, *args):
    try:
        return fn(*args)
    except FileNotFoundError as error:
        raise HTTPException(404, "找不到绑定的文件或工具。") from error
    except RegistrationConflict as error:
        raise HTTPException(409, str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@workspace.get("/namespaces/{namespace}/facade")
def declarations(namespace: str, request: Request):
    return workbench_call(facade, bound_framework(request, namespace), namespace)


def bound_folder(namespace: str, request: Request) -> Path:
    require_origin(request)
    if not (root := project_root(request)):
        raise HTTPException(503, "后端尚未配置 EFFECT_FRAMEWORK_ROOT。")
    return workbench_call(namespace_folder, Path(root).resolve(), namespace)


@workspace.get("/namespaces/{namespace}/gened")
def generated_module(namespace: str, request: Request, file: str | None = None):
    return workbench_call(source_document, bound_folder(namespace, request), True, file)


@workspace.get("/namespaces/{namespace}/facade/source")
def handwritten_module(namespace: str, request: Request, file: str | None = None):
    return workbench_call(source_document, bound_folder(namespace, request), False, file)


@workspace.get("/namespaces/{namespace}/facade/workflow")
def handwritten_workflow(namespace: str, request: Request):
    files = workbench_call(source_files, bound_folder(namespace, request))
    return {"tree": workbench_call(source_workflow, files), "revision": fingerprint(files)}


@workspace.put("/namespaces/{namespace}/facade/source")
def write_module(namespace: str, file: str, request: Request, source: str = Body(), if_match: str = Header()):
    return workbench_call(save_facade, bound_folder(namespace, request), namespace, file, source, if_match.strip('"'))


@workspace.get("/namespaces/{namespace}/plan")
def gen_plan(namespace: str, request: Request):
    return workbench_call(read_prototype, bound_folder(namespace, request), namespace)


@workspace.post("/namespaces/{namespace}/plan")
def create_gen_plan(namespace: str, request: Request):
    framework = bound_framework(request, namespace)
    return workbench_call(generate_graph, namespace, bound_folder(namespace, request), framework)


@workspace.get("/namespaces/{namespace}/workflow")
def workflow(namespace: str, request: Request):
    framework = bound_framework(request, namespace)
    try:
        return generated_workflow(framework.root, namespace, framework.source_dir)
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise HTTPException(500, f"无法读取工作流报告：{error}") from error


@workspace.post("/namespaces/{namespace}/workflow/optimize")
async def optimize(namespace: str, request: Request):
    framework = await asyncio.to_thread(bound_framework, request, namespace)
    try:
        return {"report": await optimize_workflow(framework, namespace)}
    except AstLoadError as error:
        return PlainTextResponse(str(error), status_code=422)
    except TimeoutError as error:
        raise HTTPException(504, "AST 优化超过 120 秒。") from error
    except (OSError, ValueError) as error:
        raise HTTPException(502, f"无法取得 SDK 的工作流报告：{error}") from error


@workspace.post("/namespaces/{namespace}/commands/{action}")
async def command(namespace: str, action: Literal["compile", "run"], request: Request):
    framework = await asyncio.to_thread(bound_framework, request, namespace)
    return StreamingResponse(command_output(request, framework, namespace, action == "run"),
                             media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@workspace.post("/namespaces/{namespace}/registrations", status_code=201)
def create_registration(namespace: str, document: dict, request: Request, if_match: str = Header()):
    framework = bound_framework(request, namespace)
    try:
        return add_registration(namespace, if_match.strip('"'), document, framework)
    except RegistrationConflict as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@workspace.get("/namespaces/{namespace}/authoring")
def authoring_options(namespace: str, request: Request):
    return workbench_call(editor, namespace, bound_framework(request, namespace))


@workspace.post("/namespaces/{namespace}/authoring", status_code=201)
def edit_business(namespace: str, document: Asset | Handler | WorkflowEdit | Registration, request: Request, if_match: str = Header()):
    return workbench_call(save_edit, namespace, document, if_match.strip('"'), bound_framework(request, namespace))


@workspace.post("/namespaces", status_code=201)
def initialize_namespace(request: Request, namespace: str = Body(), source_dir: str | None = Body(None)):
    require_origin(request)
    if not (root := project_root(request)):
        raise HTTPException(503, "请先导入或新建项目。")
    return workbench_call(create_namespace, Path(root).resolve(), namespace, source_dir)


router.include_router(workspace, prefix="/projects/{project_id}", dependencies=[Depends(bind_project)])
router.include_router(workspace)
