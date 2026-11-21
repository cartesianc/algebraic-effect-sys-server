import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from ...config import get_app_config
from ...lsp.analysis.sources import facade, namespace_folder, save_facade, source_document, source_namespaces
from ...lsp.codegen.registration import add_registration
from ...lsp.framework import Framework
from ...lsp.project.model import constructor_name
from ...lsp.runtime.commands import command_output
from ...lsp.runtime.snapshot import RegistrationConflict, directory, stamp
from ...lsp.runtime.workflow import AstLoadError, ast_namespaces, generated_workflow, optimize_workflow


def check_browser_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    allowed = os.environ.get(
        "EFFECT_CONSOLE_ORIGINS", "http://127.0.0.1:3000,http://localhost:3000"
    )
    if origin and origin not in {entry.strip() for entry in allowed.split(",")}:
        raise HTTPException(403, "Origin cannot read console snapshots")


router = APIRouter(
    prefix=f"{get_app_config().api_prefix}/console",
    tags=["Console"],
    dependencies=[Depends(check_browser_origin)],
)


def require_origin(request: Request):
    if not request.headers.get("origin"):
        raise HTTPException(403, "此操作需要工作台的 Origin。")


@router.get("/namespaces")
def namespaces(request: Request, workflow: bool = False) -> list[str]:
    names = {path.stem for path in directory().glob("*.json") if path.is_file()}
    if workflow:
        require_origin(request)
        if root := os.getenv("EFFECT_FRAMEWORK_ROOT"):
            workspace = Path(root).resolve()
            names = set(ast_namespaces(workspace)) | set(source_namespaces(workspace))
    return sorted(names)


@router.get("/namespaces/{namespace}")
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


def bound_framework(request: Request, namespace: str, *, allow_ast: bool = False) -> Framework:
    require_origin(request)
    if not all(map(constructor_name, namespace.split("."))):
        raise HTTPException(404, "无效的命名空间。")
    root, sdk = os.getenv("EFFECT_FRAMEWORK_ROOT"), os.getenv("EFFECT_CODEGEN")
    if not root or not sdk:
        raise HTTPException(503, "后端尚未配置 EFFECT_FRAMEWORK_ROOT 和 EFFECT_CODEGEN。")
    framework = Framework(Path(sdk).resolve(), Path(root).resolve())
    if not published(namespace):
        ast = framework.root.joinpath("demo", *namespace.split("."), "AST.hs").resolve()
        if not (allow_ast and ast.is_relative_to(framework.root / "demo") and
                any((ast.parent / file).is_file() for file in ("AST.hs", "Effects.hs", "Generated/Handlers.hs"))):
            raise HTTPException(404, "找不到工作流。" if allow_ast else "该命名空间尚未发布。")
    return framework


def workbench_call(fn, *args):
    try:
        return fn(*args)
    except FileNotFoundError as error:
        raise HTTPException(404, "找不到绑定的文件或工具。") from error
    except RegistrationConflict as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/namespaces/{namespace}/facade")
def declarations(namespace: str, request: Request):
    return workbench_call(facade, bound_framework(request, namespace, allow_ast=True), namespace)


def bound_folder(namespace: str, request: Request) -> Path:
    require_origin(request)
    if not (root := os.getenv("EFFECT_FRAMEWORK_ROOT")):
        raise HTTPException(503, "后端尚未配置 EFFECT_FRAMEWORK_ROOT。")
    return workbench_call(namespace_folder, Path(root).resolve(), namespace)


@router.get("/namespaces/{namespace}/gened")
def generated_module(namespace: str, request: Request, file: str | None = None):
    return workbench_call(source_document, bound_folder(namespace, request), True, file)


@router.get("/namespaces/{namespace}/facade/source")
def handwritten_module(namespace: str, request: Request, file: str | None = None):
    return workbench_call(source_document, bound_folder(namespace, request), False, file)


@router.put("/namespaces/{namespace}/facade/source")
def write_module(namespace: str, file: str, request: Request, source: str = Body(), if_match: str = Header()):
    return workbench_call(save_facade, bound_folder(namespace, request), namespace, file, source, if_match.strip('"'))


@router.get("/namespaces/{namespace}/plan")
def gen_plan(namespace: str, request: Request):
    framework = bound_framework(request, namespace, allow_ast=True)
    return stamp(namespace, workbench_call(framework.read_json, "console", namespace))


@router.get("/namespaces/{namespace}/workflow")
def workflow(namespace: str, request: Request):
    framework = bound_framework(request, namespace, allow_ast=True)
    try:
        return generated_workflow(framework.root, namespace)
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise HTTPException(500, f"无法读取工作流报告：{error}") from error


@router.post("/namespaces/{namespace}/workflow/optimize")
async def optimize(namespace: str, request: Request):
    framework = bound_framework(request, namespace, allow_ast=True)
    try:
        return {"report": await optimize_workflow(framework, namespace)}
    except AstLoadError as error:
        return PlainTextResponse(str(error), status_code=422)
    except TimeoutError as error:
        raise HTTPException(504, "AST 优化超过 120 秒。") from error
    except (OSError, ValueError) as error:
        raise HTTPException(502, f"无法取得 SDK 的工作流报告：{error}") from error


@router.post("/namespaces/{namespace}/commands/{action}")
async def command(namespace: str, action: Literal["compile", "run"], request: Request):
    framework = bound_framework(request, namespace, allow_ast=True)
    return StreamingResponse(command_output(request, framework, namespace, action == "run"),
                             media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/namespaces/{namespace}/registrations", status_code=201)
def create_registration(namespace: str, document: dict, request: Request, if_match: str = Header()):
    framework = bound_framework(request, namespace, allow_ast=True)
    try:
        return add_registration(namespace, if_match.strip('"'), document, framework)
    except RegistrationConflict as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
