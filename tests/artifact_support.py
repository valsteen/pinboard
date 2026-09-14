"""Fixture publication through the supported artifact repository."""

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.application.artifacts import ArtifactRef, NewArtifact


def write_revision(roots: DurableRoots, artifact: NewArtifact) -> ArtifactRef:
    return ArtifactRepository(roots).publish(artifact).reference
