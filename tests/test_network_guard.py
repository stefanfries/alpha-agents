from pathlib import Path

pytest_plugins = ("pytester",)


def test_network_guard_blocks_requests_even_when_errors_are_caught(pytester):
    pytester.makeconftest(Path(__file__).with_name("conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(
        """
        import asyncio
        import socket

        import httpx


        def test_block_socket():
            try:
                socket.socket().connect(("127.0.0.1", 1))
            except RuntimeError:
                pass


        def test_block_dns():
            try:
                socket.getaddrinfo("example.invalid", 443)
            except RuntimeError:
                pass


        def test_block_http():
            try:
                httpx.get("https://example.invalid")
            except RuntimeError:
                pass


        def test_block_async_http():
            async def request():
                async with httpx.AsyncClient() as client:
                    await client.get("https://example.invalid")

            try:
                asyncio.run(request())
            except RuntimeError:
                pass


        def test_mock_transport():
            transport = httpx.MockTransport(lambda request: httpx.Response(200))
            with httpx.Client(transport=transport) as client:
                assert client.get("https://example.invalid").status_code == 200
        """
    )
    result = pytester.runpytest_subprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=5, errors=4)
    result.stdout.fnmatch_lines([
        "*Unexpected network access attempted: socket connect*",
        "*Unexpected network access attempted: DNS lookup*",
        "*Unexpected network access attempted: httpx request*",
        "*Unexpected network access attempted: httpx async request*",
    ])
