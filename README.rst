.. code-block:: python

    import trio
    from anyio import CancelScope
    from century_ring import FileOpenFlag, FileOpenMode

    from trt.sidecar import CURRENT_DIRECTORY, open_io_uring_loop


    async def test():
        with CancelScope() as scope:
            async with open_io_uring_loop() as loop:
                fd = await loop.open_raw_file_handle(
                    CURRENT_DIRECTORY,
                    b"test.xt",
                    FileOpenMode.WRITE_ONLY,
                    {FileOpenFlag.CREATE_IF_NOT_EXISTS},
                )
                await loop.do_async_write(fd, b"test")
                scope.cancel()


    trio.run(test)
