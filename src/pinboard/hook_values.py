"""Native hook text and identity shapes shared by every runtime's hook entries."""

from typing import Annotated

import msgspec

type NativeText = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\x00]+\z")]
type NativeIdentity = Annotated[
    str,
    msgspec.Meta(min_length=1, pattern=r"\A(?!\s)(?!\.{1,2}\z)[^/\r\n\x00]*[^\s/\r\n\x00]\z"),
]
