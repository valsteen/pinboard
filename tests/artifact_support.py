"""Fixture publication through the supported artifact repository."""

from pinboard.adapters.files.artifacts import ArtifactRepository
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.application.artifacts import ArtifactPublication, ArtifactRef, NewArtifact


def write_revision(roots: DurableRoots, artifact: NewArtifact) -> ArtifactRef:
    publication = ArtifactRepository(roots).publish(artifact)
    if not isinstance(publication, ArtifactPublication):
        raise AssertionError(f"Fixture publication failed: {publication}")
    return publication.reference
