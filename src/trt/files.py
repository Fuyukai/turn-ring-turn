from __future__ import annotations

import os
from typing import Protocol, Self, override

import attr


class IntoRawHandle(Protocol):
    def as_raw_handle(self) -> RawFileHandle: ...


class RawFileHandle(IntoRawHandle, Protocol):
    fd: int

    def fileno(self) -> int: ...


@attr.define(slots=True)
class CloseableRawFileHandle(RawFileHandle):
    """
    A raw file handle that only contains a file descriptor.
    """

    fd: int = attr.field()
    active: bool = attr.field(default=True)

    @override
    def fileno(self) -> int:  # I'm sure there's a protocol for this
        return self.fd

    @override
    def as_raw_handle(self) -> RawFileHandle:
        return self

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object):
        os.close(self.fd)
        return False

    def __del__(self):
        if self.active:
            os.close(self.fd)
