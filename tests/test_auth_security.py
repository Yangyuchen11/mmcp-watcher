"""
Unit and Integration Tests for Web Authentication and IP Exemption
"""
import os
import json
import sys
import tempfile
import unittest
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from starlette.testclient import TestClient

from web.app import create_app
from core.config import ConfigManager

class TestAuthSecurity(unittest.TestCase):
    def setUp(self):
        # Create a temporary config file for testing
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.temp_dir.name, "test_config.json")
        init_config = {
            "shop": {"base_url": "https://mamachapptoy.cart.fc2.com"},
            "order": {"dry_run": True},
            "buyer": {},
            "notifications": {},
            "web": {
                "host": "0.0.0.0",
                "port": 8081,
                "password": "987654321",
                "remember_ips": True,
                "authenticated_ips": ["127.0.0.1", "::1"]
            }
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(init_config, f, indent=2)

        self.app = create_app(self.config_path)
        self.client = TestClient(self.app, base_url="http://testserver")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_unauthenticated_access_blocked(self):
        """Unauthenticated requests from public IP should be redirected to /login or get 401"""
        foreign_headers = {"x-forwarded-for": "198.51.100.22"}

        # Web page should redirect to /login
        resp_index = self.client.get("/", headers=foreign_headers, follow_redirects=False)
        self.assertEqual(resp_index.status_code, 303)
        self.assertEqual(resp_index.headers.get("location"), "/login")

        # API endpoint should return 401
        resp_api = self.client.get("/api/status", headers=foreign_headers)
        self.assertEqual(resp_api.status_code, 401)
        self.assertFalse(resp_api.json().get("authenticated"))

    def test_login_page_renders_with_ip(self):
        """GET /login should display HTML with detected client IP"""
        foreign_headers = {"x-forwarded-for": "198.51.100.33"}
        resp = self.client.get("/login", headers=foreign_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("198.51.100.33", resp.text)
        self.assertIn("987654321", resp.text)

    def test_login_invalid_password(self):
        """POST /api/login with wrong password should fail"""
        foreign_headers = {"x-forwarded-for": "198.51.100.44"}
        resp = self.client.post("/api/login", json={"password": "wrongpassword"}, headers=foreign_headers)
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(resp.json().get("success"))

    def test_login_and_ip_remember_flow(self):
        """Correct password should authenticate, remember IP in config, and allow subsequent requests without credentials"""
        foreign_headers = {"x-forwarded-for": "198.51.100.55"}

        # 1. Login with default password
        resp_login = self.client.post(
            "/api/login",
            json={"password": "987654321", "remember_ip": True},
            headers=foreign_headers
        )
        self.assertEqual(resp_login.status_code, 200)
        self.assertTrue(resp_login.json().get("success"))
        self.assertEqual(resp_login.json().get("client_ip"), "198.51.100.55")

        # Verify config file has the remembered IP written to disk
        with open(self.config_path, "r", encoding="utf-8") as f:
            saved_cfg = json.load(f)
        self.assertIn("198.51.100.55", saved_cfg["web"]["authenticated_ips"])

        # 2. Subsequent access from this IP without cookies/session
        clean_client = TestClient(self.app, base_url="http://testserver")
        resp_index = clean_client.get("/", headers=foreign_headers, follow_redirects=False)
        self.assertEqual(resp_index.status_code, 200)

        resp_api = clean_client.get("/api/status", headers=foreign_headers)
        self.assertEqual(resp_api.status_code, 200)

    def test_password_update_and_persistence(self):
        """Updating password should write to disk and require new password"""
        auth_headers = {"x-forwarded-for": "127.0.0.1"}

        # Update password
        resp_update = self.client.post(
            "/api/auth/update-password",
            json={"old_password": "987654321", "new_password": "MySuperSecret999!"},
            headers=auth_headers
        )
        self.assertEqual(resp_update.status_code, 200)
        self.assertTrue(resp_update.json().get("success"))

        # Verify persistent disk storage
        with open(self.config_path, "r", encoding="utf-8") as f:
            disk_cfg = json.load(f)
        self.assertEqual(disk_cfg["web"]["password"], "MySuperSecret999!")

        # New unauthenticated client trying old password should fail
        new_client = TestClient(self.app, base_url="http://testserver")
        new_ip_headers = {"x-forwarded-for": "198.51.100.77"}
        resp_old = new_client.post("/api/login", json={"password": "987654321"}, headers=new_ip_headers)
        self.assertEqual(resp_old.status_code, 401)

        # Trying new password should succeed
        resp_new = new_client.post("/api/login", json={"password": "MySuperSecret999!"}, headers=new_ip_headers)
        self.assertEqual(resp_new.status_code, 200)

    def test_logout_and_revoke_ip(self):
        """Logging out should revoke remembered IP"""
        headers = {"x-forwarded-for": "198.51.100.88"}
        # Login first
        self.client.post("/api/login", json={"password": "987654321", "remember_ip": True}, headers=headers)

        # Logout
        resp_logout = self.client.post("/api/logout", headers=headers)
        self.assertEqual(resp_logout.status_code, 200)

        # Verify IP is removed from config on disk
        with open(self.config_path, "r", encoding="utf-8") as f:
            disk_cfg = json.load(f)
        self.assertNotIn("198.51.100.88", disk_cfg["web"]["authenticated_ips"])

        # Create fresh client for that IP without cookies -> should be blocked
        clean_client = TestClient(self.app, base_url="http://testserver")
        resp = clean_client.get("/api/status", headers=headers)
        self.assertEqual(resp.status_code, 401)

    def test_localhost_logout_requires_login(self):
        """When 127.0.0.1 logs out, it should be required to re-authenticate"""
        local_headers = {"x-forwarded-for": "127.0.0.1"}

        # 1. Logout
        resp_logout = self.client.post("/api/logout", headers=local_headers)
        self.assertEqual(resp_logout.status_code, 200)

        # 2. Accessing /login should not redirect to /, but render login page
        clean_client = TestClient(self.app, base_url="http://testserver")
        resp_login = clean_client.get("/login", headers=local_headers, follow_redirects=False)
        self.assertEqual(resp_login.status_code, 200)
        self.assertIn("127.0.0.1", resp_login.text)

        # 3. Accessing / should redirect to /login
        resp_root = clean_client.get("/", headers=local_headers, follow_redirects=False)
        self.assertEqual(resp_root.status_code, 303)
        self.assertEqual(resp_root.headers.get("location"), "/login")

if __name__ == "__main__":
    unittest.main()

