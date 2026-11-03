"""Resolve generated, verified instruction documents for a recorded route and native caller."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, assert_never

import msgspec

from pinboard.application.dispatch_models import NativeInstruction
from pinboard.domain import decision_models, history, work_models
from pinboard.mcp import host_profiles
from pinboard.mcp.instruction_models import ActionInstructions


class Document(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    path: str
    sha256: str
    size_bytes: int
    owners: tuple[str, ...]


class Catalog(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-instruction-catalog/v1"]
    native_read_bytes: int
    role_budgets: dict[str, int]
    documents: dict[str, Document]


@dataclass(frozen=True, slots=True)
class InstructionReader:
    """One request's catalog and verified documents; repeated actions share the same package reads."""

    root: Path
    catalog: Catalog
    verified_documents: dict[tuple[str, str, int], NativeInstruction]


def load_catalog() -> InstructionReader:
    root = Path(__file__).resolve().parent.parent / "instructions"
    catalog = msgspec.json.decode((root / "catalog.json").read_bytes(), type=Catalog)
    return InstructionReader(root, catalog, {})


def native_document(key: str) -> NativeInstruction:
    return read_document(load_catalog(), key)


def read_document(reader: InstructionReader, key: str) -> NativeInstruction:
    """Verify packaged bytes before advertising them; stale generation is an installation defect."""
    document = reader.catalog.documents[key]
    identity = document.path, document.sha256, document.size_bytes
    if identity in reader.verified_documents:
        return reader.verified_documents[identity]
    path = reader.root / document.path
    body = path.read_bytes()
    if len(body) != document.size_bytes or hashlib.sha256(body).hexdigest() != document.sha256:
        raise ValueError(f"Installed instruction document differs from its catalog: {path}")
    if len(body) > reader.catalog.native_read_bytes:
        raise ValueError(f"Installed instruction document exceeds one native read: {path}")
    selected = NativeInstruction(str(path), document.sha256, document.size_bytes, "one-complete-native-read")
    reader.verified_documents[identity] = selected
    return selected


def action_instructions(
    reader: InstructionReader,
    kind: decision_models.ActionKind,
    role: decision_models.Role,
    profile: host_profiles.HostProfile,
    route: work_models.RouteFacts | None,
) -> ActionInstructions:
    facts = (
        None
        if route is None
        else history.RouteFactsPayload(
            route.size_class, route.architecture_impact, route.external_effects, route.declares_checkpoints
        )
    )
    if profile == host_profiles.HostProfile.UNMEASURED:
        return ActionInstructions(role, profile.value, facts, None, "protocol-client-no-native-runtime")
    match role:
        case decision_models.Role.WORKER:
            key = f"worker-{profile.value}"
        case decision_models.Role.PROJECT | decision_models.Role.OBSERVER | decision_models.Role.PREPARER:
            size = "small" if route is not None and route.size_class == work_models.SizeClass.SMALL else "standard"
            checkpoints = "checkpoints" if route is not None and route.declares_checkpoints else "single"
            key = f"{kind.value}-{profile.value}-{size}-{checkpoints}"
        case _ as unreachable:
            assert_never(unreachable)

    return ActionInstructions(role, profile.value, facts, read_document(reader, key), "native-document")
