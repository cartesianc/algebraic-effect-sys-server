import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from ...config import get_app_config
from ...lsp.codegen.registration import add_registration
from ...lsp.framework import Framework
from ...lsp.runtime.commands import command_output
from ...lsp.runtime.snapshot import RegistrationConflict, directory


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


@router.get("/namespaces")
def namespaces() -> list[str]:
    return sorted(path.stem for path in directory().glob("*.json") if path.is_file())


@router.get("/namespaces/{namespace}")
def snapshot(namespace: str) -> FileResponse:
    if namespace not in namespaces():
        raise HTTPException(404, "Namespace has no published snapshot")
    return FileResponse(
        directory() / (namespace + ".json"),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def bound_framework(request: Request, namespace: str) -> Framework:
    if not request.headers.get("origin"):
        raise HTTPException(403, "此操作需要工作台的 Origin。")
    if namespace not in namespaces():
        raise HTTPException(404, "该命名空间尚未发布。")
    root, sdk = os.getenv("EFFECT_FRAMEWORK_ROOT"), os.getenv("EFFECT_CODEGEN")
    if not root or not sdk:
        raise HTTPException(503, "后端尚未配置 EFFECT_FRAMEWORK_ROOT 和 EFFECT_CODEGEN。")
    return Framework(Path(sdk).resolve(), Path(root).resolve())


@router.post("/namespaces/{namespace}/commands/{action}")
async def command(namespace: str, action: Literal["compile", "run"], request: Request):
    framework = bound_framework(request, namespace)
    return StreamingResponse(command_output(request, framework, namespace, action == "run"),
                             media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/namespaces/{namespace}/registrations", status_code=201)
def create_registration(namespace: str, document: dict, request: Request, if_match: str = Header()):
    framework = bound_framework(request, namespace)
    try:
        return add_registration(namespace, if_match.strip('"'), document, framework)
    except RegistrationConflict as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
