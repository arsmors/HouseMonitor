"""HTTP integration tests against a temporary in-process service."""

import unittest
import os
from unittest.mock import patch

from aiohttp import BasicAuth
from aiohttp.test_utils import TestClient, TestServer

from backend.app import create_app
from backend.store import Store


class APITests(unittest.IsolatedAsyncioTestCase):
    """Validate API persistence, input handling and cross-origin protections."""

    async def asyncSetUp(self):
        """Create an isolated service without starting any external collectors."""
        self.store = Store(":memory:")
        with patch.dict(os.environ, {"WEB_PASSWORD": ""}):
            self.client = TestClient(TestServer(create_app(self.store, start_tasks=False)))
        await self.client.start_server()

    async def asyncTearDown(self):
        """Close both the HTTP server and database."""
        await self.client.close()
        self.store.close()

    async def test_real_state_and_search_lifecycle(self):
        """Keep state server-side through create, edit, pause and delete operations."""
        response = await self.client.get("/api/state")
        data = await response.json()
        self.assertEqual(len(data["searches"]), 1)
        self.assertEqual(data["listings"], [])
        headers = {"X-House-Monitor": "1"}
        response = await self.client.post("/api/searches", json={"name": "Test", "min": 100, "max": 250, "locations": []}, headers=headers)
        self.assertEqual(response.status, 201)
        search = await response.json()
        response = await self.client.put(f'/api/searches/{search["id"]}', json={**search, "active": False}, headers=headers)
        self.assertFalse((await response.json())["active"])
        response = await self.client.delete(f'/api/searches/{search["id"]}', headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.store.searches()), 1)

    async def test_security_and_validation(self):
        """Reject cross-origin requests, missing mutation headers and invalid filters."""
        response = await self.client.post("/api/searches", json={"name": "x"})
        self.assertEqual(response.status, 403)

    async def test_password_auth_and_local_health(self):
        """Require configured owner credentials while allowing local Docker health probes."""
        with patch.dict(os.environ, {"WEB_PASSWORD": "verification-password"}):
            client = TestClient(TestServer(create_app(self.store, start_tasks=False)))
        await client.start_server()
        try:
            response = await client.get("/api/state")
            self.assertEqual(response.status, 401)
            response = await client.get("/api/health")
            self.assertEqual(response.status, 200)
            response = await client.get("/api/state", auth=BasicAuth("owner", "verification-password"))
            self.assertEqual(response.status, 200)
        finally:
            await client.close()
        response = await self.client.post("/api/searches", json={"name": "x"}, headers={"X-House-Monitor": "1", "Origin": "https://evil.example"})
        self.assertEqual(response.status, 403)
        response = await self.client.post("/api/searches", json={"name": "x", "min": -5}, headers={"X-House-Monitor": "1"})
        self.assertEqual(response.status, 400)
        response = await self.client.get("/api/state", headers={"Host": "evil.example"})
        self.assertEqual(response.status, 403)


if __name__ == "__main__":
    unittest.main()