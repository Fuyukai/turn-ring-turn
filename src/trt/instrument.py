from __future__ import annotations

from typing import TYPE_CHECKING, override

import attr
import trio

if TYPE_CHECKING:
    from trt.sidecar import UringSidecar


@attr.define(slots=True, frozen=True, hash=False, eq=False)
class SubmissionInstrument(trio.abc.Instrument):
    sidecar: UringSidecar

    @override
    def before_io_wait(self, timeout: float) -> None:
        self.sidecar.submit()
