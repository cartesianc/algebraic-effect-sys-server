"""Server-wide model selection for the two Codex-backed workbench jobs."""

import json
import os
import subprocess
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import codex
from .metadata import AgentModelSetting, db


Purpose = Literal["translation", "codegen"]


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)


DEFAULTS = {
    "translation": ModelSelection(model="gpt-6.1-terra", reasoning_effort="medium"),
    "codegen": ModelSelection(model="gpt-6.1-sol", reasoning_effort="high"),
}


class ModelOption(BaseModel):
    model: str
    reasoning_efforts: list[str]


def catalog() -> list[ModelOption]:
    tool = codex.command()
    if not tool:
        raise RuntimeError("服务端未找到 Codex CLI。")
    try:
        result = subprocess.run([*tool, "debug", "models"], capture_output=True, check=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        options = []
        for row in json.loads(result.stdout)["models"]:
            if row["visibility"] == "list" and row["slug"].startswith("gpt-"):
                efforts = [level["effort"] for level in row["supported_reasoning_levels"]]
                options.append(ModelOption(model=row["slug"], reasoning_efforts=efforts))
        return options
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("无法读取 Codex 模型目录，请以服务账户检查 codex debug models 和 codex login status。") from exc


def current(purpose: Purpose) -> ModelSelection:
    with db.connection_context():
        setting = AgentModelSetting.get_or_none(AgentModelSetting.purpose == purpose)
        if setting is None:
            return DEFAULTS[purpose]
        return ModelSelection(model=setting.model, reasoning_effort=setting.reasoning_effort)


def save(purpose: Purpose, selection: ModelSelection, options: list[ModelOption]) -> ModelSelection:
    option = next((item for item in options if item.model == selection.model), None)
    if option is None:
        raise ValueError("该模型不在服务端 Codex 模型目录中。")
    if selection.reasoning_effort not in option.reasoning_efforts:
        raise ValueError("该模型支持的推理强度为 " + "、".join(option.reasoning_efforts))
    with db.connection_context():
        AgentModelSetting.replace(purpose=purpose, **selection.model_dump()).execute()
    return selection
