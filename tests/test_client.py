from __future__ import annotations

import asyncio
import importlib
import json
import sys
import unittest
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PACKAGE_NAME = Path(__file__).resolve().parents[1].name
client_module = importlib.import_module(f"{PACKAGE_NAME}.client")
image_module = importlib.import_module(f"{PACKAGE_NAME}.image_utils")
SearchParameters = client_module.SearchParameters
SoutubotClient = client_module.SoutubotClient
PreparedImage = image_module.PreparedImage


class SoutubotClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.home_requests = 0
        self.search_requests = 0
        self.block_first_search = False
        app = web.Application()
        app.router.add_get("/", self._home)
        app.router.add_post("/api/search", self._search)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        sockets = self.site._server.sockets  # type: ignore[union-attr]
        port = sockets[0].getsockname()[1]
        self.client = SoutubotClient(
            base_url=f"http://127.0.0.1:{port}", timeout_seconds=10
        )

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.runner.cleanup()

    async def _home(self, request: web.Request) -> web.Response:
        self.home_requests += 1
        return web.Response(text="ok")

    async def _search(self, request: web.Request) -> web.Response:
        self.search_requests += 1
        if self.block_first_search and self.search_requests == 1:
            return web.json_response({"detail": "blocked"}, status=403)
        form = await request.post()
        self.assertEqual(form["factor"], "1.2")
        self.assertEqual(form["metadata_mode"], "display")
        self.assertEqual(form["top_k"], "25")
        upload = form["file"]
        self.assertEqual(upload.filename, "test.jpg")
        self.assertEqual(upload.file.read(), b"jpeg-data")
        body = json.dumps(
            {
                "result_id": "result-1",
                "results": [
                    {
                        "score": 88.5,
                        "path_segments": [
                            {
                                "metadata": {
                                    "title": {"primary": "Example"},
                                    "source": {"key": "nhentai"},
                                }
                            }
                        ],
                    }
                ],
            }
        ).encode()
        response = web.StreamResponse(
            status=200, headers={"Content-Type": "application/json"}
        )
        await response.prepare(request)
        midpoint = len(body) // 2
        await response.write(body[:midpoint])
        await asyncio.sleep(0.01)
        await response.write(body[midpoint:])
        await response.write_eof()
        return response

    @staticmethod
    def _image() -> PreparedImage:
        return PreparedImage(
            data=b"jpeg-data",
            filename="test.jpg",
            content_type="image/jpeg",
            original_size=9,
            changed=False,
        )

    async def test_multipart_request_and_response(self) -> None:
        result = await self.client.search(self._image(), SearchParameters())
        self.assertEqual(self.home_requests, 1)
        self.assertEqual(self.search_requests, 1)
        self.assertEqual(result.result_id, "result-1")
        self.assertEqual(result.items[0].title, "Example")

    async def test_http_403_rewarms_once(self) -> None:
        self.block_first_search = True
        result = await self.client.search(self._image(), SearchParameters())
        self.assertEqual(result.result_id, "result-1")
        self.assertEqual(self.home_requests, 2)
        self.assertEqual(self.search_requests, 2)


if __name__ == "__main__":
    unittest.main()
