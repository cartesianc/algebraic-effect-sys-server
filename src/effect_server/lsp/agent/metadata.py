"""SQLite translations keyed by registration name or asset:<contract> in a namespace."""

from dataclasses import dataclass
import json
import os
from time import time
from typing import Annotated
from uuid import uuid4

from peewee import BooleanField, FloatField, ForeignKeyField, Model, SqliteDatabase, TextField
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ..analysis.sources import revision
from ..runtime.snapshot import RegistrationConflict, console_home


db = SqliteDatabase(None, pragmas={"foreign_keys": 1, "journal_mode": "wal"}, timeout=10)
MAX_ALIAS_LENGTH = 14
CardAlias = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ALIAS_LENGTH,
                                            pattern=r"^[^\u0000-\u001f\u007f-\u009f\u2028\u2029]+$")]


class Namespace(Model):
    project = TextField()
    name = TextField()
    folder = TextField()
    signature = TextField(default="")

    class Meta:
        database = db
        indexes = ((("project", "name"), True),)


class EffectNode(Model):
    namespace = ForeignKeyField(Namespace, on_delete="CASCADE")
    name = TextField()
    alias = TextField(default="")
    description = TextField(default="")
    manual_alias = BooleanField(default=False)
    manual_description = BooleanField(default=False)
    locked = BooleanField(default=False)
    context = TextField()
    source_key = TextField()
    generated_key = TextField(default="")
    revision = TextField(default=lambda: uuid4().hex)
    next_attempt = FloatField(default=0)
    error = TextField(default="")

    class Meta:
        database = db
        indexes = ((("namespace", "name"), True),)


class MetadataEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    alias: CardAlias | None = None
    description: str | None = Field(default=None, min_length=1, max_length=1200)
    locked: bool = False


class GenerationRequest(Model):
    """A single explicit request for a node at its current source version."""
    node = ForeignKeyField(EffectNode, primary_key=True, on_delete="CASCADE")
    source_key = TextField()

    class Meta:
        database = db


class AgentModelSetting(Model):
    purpose = TextField(primary_key=True)
    model = TextField()
    reasoning_effort = TextField()

    class Meta:
        database = db


def automatic_projects():
    return {v.strip() for v in os.getenv("EFFECT_METADATA_PROJECTS", "").split(",") if v.strip()}


def initialize():
    folder = console_home()
    folder.mkdir(exist_ok=True, parents=True)
    db.init(str(folder / "projects.sqlite3"))
    with db.connection_context(), db.atomic("IMMEDIATE"):
        db.create_tables([Namespace, EffectNode, GenerationRequest, AgentModelSetting])
        if "locked" not in {column.name for column in db.get_columns(EffectNode._meta.table_name)}:
            db.execute_sql("ALTER TABLE effectnode ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
            # Existing manual translations remain current until their next requested or source-driven translation.
            EffectNode.update(generated_key=EffectNode.source_key).where(
                EffectNode.manual_alias | EffectNode.manual_description,
                EffectNode.id.not_in(GenerationRequest.select(GenerationRequest.node))).execute()


def publish(project, namespace, folder, signature, contexts):
    with db.connection_context(), db.atomic("IMMEDIATE"):
        ns, _ = Namespace.get_or_create(project=project, name=namespace, defaults={"folder": str(folder)})
        existing = {row.name: row for row in EffectNode.select().where(EffectNode.namespace == ns)}
        for name, context in contexts.items():
            raw = json.dumps(context, ensure_ascii=False, sort_keys=True)
            key = revision(raw.encode())
            row = existing.pop(name, None)
            if row is None:
                EffectNode.create(namespace=ns, name=name, source_key=key, context=raw)
            elif row.source_key != key:
                GenerationRequest.delete().where(GenerationRequest.node == row).execute()
                row.context, row.source_key, row.next_attempt, row.error = raw, key, 0, ""
                row.revision = uuid4().hex
                row.save()
        for obsolete in existing.values():
            obsolete.delete_instance()
        ns.folder, ns.signature = str(folder), signature
        ns.save()


def namespaces(project):
    with db.connection_context():
        return {ns.name: ns for ns in Namespace.select().where(Namespace.project == project)}


def remove_namespace(row):
    with db:
        row.delete_instance()


def record(node):
    outdated = node.source_key != node.generated_key and not node.locked
    pending = outdated and (node.namespace.project in automatic_projects() or
                            GenerationRequest.select().where(GenerationRequest.node == node,
                                                             GenerationRequest.source_key == node.source_key).exists())
    return {"name": node.name, "alias": node.alias, "aliasMaxLength": MAX_ALIAS_LENGTH, "description": node.description,
            "manualAlias": node.manual_alias, "manualDescription": node.manual_description,
            "edited": node.manual_alias or node.manual_description, "locked": node.locked,
            "pending": pending, "error": node.error if outdated else "", "revision": node.revision}


def find(project, namespace, name):
    query = EffectNode.select().join(Namespace).where(
        Namespace.project == project, Namespace.name == namespace, EffectNode.name == name)
    node = query.first()
    if not node:
        raise FileNotFoundError(name)
    return node


def read(project, namespace, name):
    with db.connection_context():
        return record(find(project, namespace, name))


def aliases(project, namespace):
    with db.connection_context():
        rows = EffectNode.select(EffectNode.name, EffectNode.alias).join(Namespace).where(
            Namespace.project == project, Namespace.name == namespace, EffectNode.alias != "")
        return dict(rows.tuples())


def card_translations(project, namespace):
    with db.connection_context():
        pending = (~EffectNode.locked & (EffectNode.source_key != EffectNode.generated_key) & (EffectNode.error == ""))
        if project not in automatic_projects():
            requested = GenerationRequest.select(GenerationRequest.node).where(GenerationRequest.source_key == EffectNode.source_key)
            pending &= EffectNode.id.in_(requested)
        rows = EffectNode.select(EffectNode.name, EffectNode.alias, EffectNode.locked, EffectNode.revision, pending.converter(bool)).join(
            Namespace).where(Namespace.project == project, Namespace.name == namespace)
        return {name: {"alias": alias, "locked": locked, "revision": version, "pending": waiting}
                for name, alias, locked, version, waiting in rows.tuples()}


def edit(project, namespace, name, expected, changes):
    with db.connection_context(), db.atomic("IMMEDIATE"):
        node = find(project, namespace, name)
        if node.revision != expected:
            raise RegistrationConflict("节点资料已更新，请重新读取。")
        fields = changes.model_dump(exclude_unset=True)
        if "locked" in fields:
            node.locked = fields.pop("locked")
        for field, value in fields.items():
            setattr(node, "manual_" + field, value is not None)
            if value is not None:
                setattr(node, field, value)
        if fields:
            node.generated_key = "" if None in fields.values() else node.source_key
        GenerationRequest.delete().where(GenerationRequest.node == node).execute()
        node.next_attempt, node.error, node.revision = 0, "", uuid4().hex
        node.save()
        return record(node)


def regenerate(project, namespace, name, expected):
    with db.connection_context(), db.atomic("IMMEDIATE"):
        node = find(project, namespace, name)
        if node.revision != expected:
            raise RegistrationConflict("节点资料已更新，请重新读取。")
        if node.locked:
            raise RegistrationConflict("这张卡片的翻译已锁定。")
        enqueue(node)
        return record(node)


def enqueue(node):
    GenerationRequest.replace(node=node, source_key=node.source_key).execute()
    # A worker may already hold this version. Keep its lease and accept its result.
    if node.generated_key == node.source_key or node.error:
        node.generated_key, node.next_attempt, node.error, node.revision = "", 0, "", uuid4().hex
        node.save()


def translations(project, namespace, regenerate_all=False):
    with db.connection_context(), db.atomic("IMMEDIATE" if regenerate_all else "DEFERRED"):
        ns = Namespace.get_or_none(Namespace.project == project, Namespace.name == namespace)
        if ns is None:
            raise RegistrationConflict("命名空间的节点索引尚未就绪，请稍后重试。")
        rows = list(EffectNode.select().where(EffectNode.namespace == ns))
        eligible = [row for row in rows if not row.locked]
        if regenerate_all:
            for row in eligible:
                enqueue(row)
        explicit = dict(GenerationRequest.select(GenerationRequest.node, GenerationRequest.source_key)
                        .join(EffectNode).where(EffectNode.namespace == ns).tuples())
        auto = project in automatic_projects()
        outdated = [row for row in eligible if row.generated_key != row.source_key]
        return {"eligible": len(eligible), "locked": len(rows) - len(eligible),
                "pending": sum(not row.error and (auto or explicit.get(row.id) == row.source_key) for row in outdated),
                "failed": sum(bool(row.error) for row in outdated)}


@dataclass(frozen=True)
class TranslationJob:
    ident: int
    source_key: str
    lease: float
    project: str
    namespace: str
    folder: str
    context: dict


def claim() -> TranslationJob | None:
    with db.connection_context(), db.atomic("IMMEDIATE"):
        now = time()
        allowed = Namespace.select(Namespace.id).where(Namespace.project.in_(automatic_projects()))
        requested = GenerationRequest.select(GenerationRequest.node).where(GenerationRequest.source_key == EffectNode.source_key)
        pending = EffectNode.select().where(EffectNode.namespace.in_(allowed) | EffectNode.id.in_(requested),
                                            EffectNode.source_key != EffectNode.generated_key, ~EffectNode.locked)
        is_asset = EffectNode.name.startswith("asset:")
        # Due assets sort first. Wait for a referenced asset being translated by another worker.
        active_assets = set(pending.select(EffectNode.namespace, EffectNode.name).where(
            is_asset, EffectNode.next_attempt > now, EffectNode.error == "").tuples())
        for row in pending.where(EffectNode.next_attempt <= now).order_by(is_asset.desc(), EffectNode.id).iterator():
            context = json.loads(row.context)
            context["name"] = row.name
            if effect := context.get("effect"):
                names = {"asset:" + d["contract"] for d in effect["demands"]}
                if asset := effect["typing"].get("asset"):
                    names.add("asset:" + asset["contract"])
                if any((row.namespace_id, name) in active_assets for name in names):
                    continue
                terms = EffectNode.select(EffectNode.name, EffectNode.alias).where(
                    EffectNode.namespace == row.namespace_id, EffectNode.name.in_(names), EffectNode.alias != "")
                context["assetTerms"] = {name.removeprefix("asset:"): alias for name, alias in terms.tuples()}
            context["editedText"] = {field: getattr(row, field) for field in ("alias", "description")
                                     if getattr(row, "manual_" + field)}
            row.next_attempt, row.error = now + 330, ""
            row.save(only=[EffectNode.next_attempt, EffectNode.error])
            ns = row.namespace
            return TranslationJob(row.id, row.source_key, row.next_attempt, ns.project, ns.name, ns.folder, context)


def finish(job, generated, error=""):
    with db.connection_context(), db.atomic("IMMEDIATE"):
        node = EffectNode.get_or_none(EffectNode.id == job.ident, EffectNode.name == job.context["name"],
                                     EffectNode.source_key == job.source_key, EffectNode.next_attempt == job.lease,
                                     ~EffectNode.locked)
        if node is None:
            return
        GenerationRequest.delete().where(GenerationRequest.node == node).execute()
        if not error:
            node.alias, node.description = generated.alias, generated.description
            node.manual_alias = node.manual_description = False
            node.generated_key = job.source_key
        node.next_attempt = time() + 300 if error else 0
        node.error, node.revision = error, uuid4().hex
        node.save()
