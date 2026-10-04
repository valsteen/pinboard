"""Exact historical run format, readable without inventing scenario registration.

Retained private v2 evidence supports descriptive reports and accounting. It cannot
qualify a current comparison; remove this reader only after that evidence is retired.
"""

from typing import Literal

from evals.behavioral.records import RunEvidence


class CompatibilityRunRecord(RunEvidence, frozen=True):
    schema: Literal["pinboard-behavioral-run/v2"]
