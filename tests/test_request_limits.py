import unittest

from request_limits import RequestBodyLimitMiddleware


class RequestBodyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, *, headers=(), chunks=(), limit=5, path="/api/upload-preview"):
        messages = [
            {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
            for index, chunk in enumerate(chunks)
        ]
        app_called = False
        sent = []

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        async def app(scope, receive, send):
            nonlocal app_called
            app_called = True
            while True:
                message = await receive()
                if message["type"] == "http.disconnect" or not message.get("more_body"):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RequestBodyLimitMiddleware(app, max_upload_body_bytes=limit)
        scope = {"type": "http", "path": path, "method": "POST", "headers": list(headers)}
        await middleware(scope, receive, send)
        status = next(item["status"] for item in sent if item["type"] == "http.response.start")
        return status, app_called

    async def test_rejects_oversized_content_length_before_parsing(self):
        status, app_called = await self.invoke(headers=[(b"content-length", b"6")], chunks=[b"123456"])
        self.assertEqual(status, 413)
        self.assertFalse(app_called)

    async def test_rejects_oversized_chunked_upload(self):
        status, app_called = await self.invoke(chunks=[b"123", b"456"])
        self.assertEqual(status, 413)
        self.assertTrue(app_called)

    async def test_allows_requests_within_limit(self):
        status, app_called = await self.invoke(chunks=[b"12", b"345"])
        self.assertEqual(status, 200)
        self.assertTrue(app_called)

    async def test_leaves_other_routes_unchanged(self):
        status, app_called = await self.invoke(chunks=[b"123456"], path="/api/login")
        self.assertEqual(status, 200)
        self.assertTrue(app_called)


if __name__ == "__main__":
    unittest.main()
