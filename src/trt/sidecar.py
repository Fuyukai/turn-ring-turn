from __future__ import annotations

import errno
import os
import warnings
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from os import PathLike
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import anyio
import attr
import century_ring
import sniffio
from anyio.streams.memory import MemoryObjectSendStream
from century_ring import CompletionEvent, FileOpenFlag, FileOpenMode, IoUring, raise_for_cqe

from trt.files import CloseableRawFileHandle, IntoRawHandle, RawFileHandle

if TYPE_CHECKING:
    from trio.abc import Instrument


class _CurrentDirectory:
    pass


CURRENT_DIRECTORY = _CurrentDirectory()


class InstrumentToken:
    def __init__(self) -> None:
        self._actual_instrument: Instrument | None = None


def _add_submit_hook(loop: UringSidecar, token: InstrumentToken) -> InstrumentToken:
    if sniffio.current_async_library() == "trio":
        from trio.lowlevel import add_instrument

        from trt.instrument import SubmissionInstrument

        s = SubmissionInstrument(loop)
        add_instrument(s)
        token._actual_instrument = s

    return token


def _remove_submit_hook(token: InstrumentToken):
    if sniffio.current_async_library() == "trio" and token._actual_instrument is not None:
        from trio.lowlevel import remove_instrument

        remove_instrument(token._actual_instrument)


@attr.define(slots=True, kw_only=True)
class UringSidecar:
    """
    A special "sidecar" event loop that provides asynchronous file I/O.
    """

    ring: IoUring = attr.field()
    efd: int = attr.field()
    submit_on_action: bool = attr.field()

    # internal tracking
    # used to wake up secondary tasks
    _task_mapping: dict[int, MemoryObjectSendStream[CompletionEvent]] = attr.field(
        init=False, factory=dict
    )

    def _dispatch_completion(self, completion: CompletionEvent):
        task = self._task_mapping.pop(completion.user_data, None)
        if task is None:
            warnings.warn(
                f"Found completion #{completion.user_data} with no matching task", stacklevel=1
            )
            return

        try:
            task.send_nowait(completion)
        except anyio.WouldBlock:
            warnings.warn(
                f"Found completion #{completion.user_data} with nobody listening", stacklevel=1
            )
        else:
            task.close()

    async def _dispatch_completion_events(self):
        while True:
            await anyio.wait_socket_readable(self.efd)  # type: ignore
            os.read(self.efd, 8)  # discard, we don't care what it actually says
            completions = self.ring.get_completion_entries()

            for completion in completions:
                self._dispatch_completion(completion)

    async def _wait(self, user_data: int, *, auto_raise: bool = True) -> CompletionEvent:
        if self.submit_on_action:
            self.submit()

        write, read = anyio.create_memory_object_stream[CompletionEvent](0)
        self._task_mapping[user_data] = write

        with write, anyio.CancelScope(shield=True):
            cqe = await read.receive()

            if auto_raise:
                raise_for_cqe(cqe)

            return cqe

    def submit(self):
        """
        Submits any outstanding submission queue entries to the kernel.

        This only needs to be called if autosubmission is disabled. When it is enabled, this method
        will be called automatically.
        """

        self.ring.submit()

    async def open_raw_file_handle(
        self,
        relative_to: IntoRawHandle | _CurrentDirectory | None,
        file_path: PurePosixPath | bytes | PathLike[bytes],
        open_mode: FileOpenMode,
        flags: Iterable[FileOpenFlag],
        permissions: int = 0o666,
    ) -> RawFileHandle:
        """
        Opens a new raw file handle to the provided path. See
        :func:`century_ring.IoUring.prep_openat` for more extensive documentation.

        You probably want the higher-level helper functions.

        :param relative_to:

            A file descriptor that signifies the directory that this file should be opened relative
            to. If this is the special constant ``CURRENT_DIRECTORY``, then the path will be
            treated as relative to the current working directory. If the path provided is an
            absolute path, this parameter will be ignored.
        """

        handle: int | None = None
        if isinstance(relative_to, _CurrentDirectory):
            handle = century_ring.AT_FDCWD

        elif relative_to is not None:
            handle = relative_to.as_raw_handle().fd

        ud = self.ring.prep_openat(
            relative_to=handle,
            path=os.fsencode(file_path),
            open_mode=open_mode,
            flags=flags,
            permissions=permissions,
        )
        completion = await self._wait(ud, auto_raise=False)

        match completion.result:
            case value if completion.result >= 0:
                return CloseableRawFileHandle(value)

            case errno.ENOENT:
                raise FileNotFoundError(os.fspath(file_path))

            case errno.EISDIR:
                raise IsADirectoryError(os.fspath(file_path))

            case errno.ENOTDIR:
                raise NotADirectoryError(os.fspath(file_path))

            case errno.EEXIST:
                raise FileExistsError(os.fspath(file_path))

            case _:
                raise_for_cqe(completion)
                raise RuntimeError("?")

    async def do_async_read(self, fd: IntoRawHandle, size: int, offset: int = -1) -> bytes:
        """
        Performs an asynchronous read operation on the provided file handle.

        You probably want the higher-level operations from :class:`.UnbufferedFile`.
        """

        ud = self.ring.prep_read(fd.as_raw_handle().fd, size, offset=-1)
        completion = await self._wait(ud)
        return completion.buffer or b""

    async def do_async_write(
        self,
        fd: IntoRawHandle,
        buffer: bytes,
        file_offset: int = -1,
        count: int | None = None,
        buffer_offset: int | None = None,
    ) -> int:
        """
        Performs an asynchronous write operation on the provided file handle.

        You probably want the higher-level operations from :class:`.UnbufferedFile`.
        """

        ud = self.ring.prep_write(
            fd.as_raw_handle().fd,
            buffer,
            file_offset=file_offset,
            count=count,
            buffer_offset=buffer_offset,
        )
        completion = await self._wait(ud)
        return completion.result


@asynccontextmanager
async def open_io_uring_loop(
    entries: int = 256,
    cq_size: int | None = None,
    sqpoll_idle_ms: int | None = None,
    autosubmission: bool = True,
) -> AsyncIterator[UringSidecar]:
    """
    Creates a new ``io_uring`` sidecar event loop. This is an asynchronous context manager.

    :param entries:

        The maximum number of submission queue entries in the ring before a call to
        ``io_uring_enter`` must take place.

        If the submission queue is full, and something attempts to place a new entry in the
        submission queue, then an automatic call to ``io_uring_enter`` will take place.

    :param cq_size:

        The maximum number of entries in the completion queue. This only bounds the maximum
        number that will be copied into userspace across a single call; any completion queue
        entries that would not fit are buffered in kernelspace memory first.

    :param sqpoll_idle_ms:

        The number of milliseconds the kernel submission queue polling thread should wait for a new
        submission queue entry before returning to idle.

        If this value is zero or lower, then submission queue polling will be disabled entirely.

    :param autosubmission:

        If true, this enables *automatic submission* to the ``io_uring``.

        This has differing behaviour depending on asynchronous backend:

        - For the Trio backend, an instrument is installed that will automatically submit all
          entries in the submission queue when the Trio event loop begins performing I/O.
        - For the Asyncio backend, all operations will force a submit.
    """

    is_trio = sniffio.current_async_library() == "trio"
    token = InstrumentToken()

    submit_on_action = (not is_trio) if autosubmission else False

    if autosubmission and is_trio:
        submit_on_action = False

    with (
        century_ring.make_io_ring(
            entries=entries, cq_size=cq_size, sqpoll_idle_ms=sqpoll_idle_ms, autosubmit=True
        ) as ring,
    ):
        efd = ring.register_eventfd()
        sidecar = UringSidecar(ring=ring, submit_on_action=submit_on_action, efd=efd)

        async with anyio.create_task_group() as group:
            group.start_soon(sidecar._dispatch_completion_events)
            try:
                if is_trio and autosubmission:
                    _add_submit_hook(sidecar, token)

                yield sidecar
            finally:
                for channel in sidecar._task_mapping.values():
                    channel.close()

                _remove_submit_hook(token)
