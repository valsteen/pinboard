"""Filesystem acquisition for application-owned brief source selection."""

from pathlib import Path

from pinboard.application.brief_source_models import (
    AuthoritySelector,
    BriefSourceErrorCode,
    BriefSourceFailure,
    BriefSourceResult,
    SelectedBriefSource,
)
from pinboard.application.brief_sources import select_brief_source_bytes


def select_checkout_brief_source(
    source_checkout_root: Path,
    selector: AuthoritySelector,
    require_utf8: bool,
) -> BriefSourceResult[SelectedBriefSource]:
    """Read one checkout-relative authority and delegate pure selection."""
    path = source_checkout_root / Path(*selector.relative_path.parts)
    try:
        raw = path.read_bytes()
    except OSError as error:
        return BriefSourceFailure(
            BriefSourceErrorCode.SOURCE_UNREADABLE,
            f"Cannot read authority at '{path}': {error}",
        )
    return select_brief_source_bytes(selector, raw, require_utf8)
