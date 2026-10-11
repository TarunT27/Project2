"""No-login local mode: automatic local admin, loopback-only host check, refusal on non-loopback binds."""

import isolation  # noqa: F401  (must run before anything reads Claude credentials)

import os
import shutil
import tempfile
import unittest

TMP = tempfile.mkdtemp(prefix="dra-nologin-")
SAVED = {}
KEYS = ("DRA_DATA_DIR", "DRA_MODELS_DIR", "DRA_NO_LOGIN", "HOST")


def _set(**values):
    from app.config import get_config

    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_config.cache_clear()


def setUpModule():
    SAVED.update({k: os.environ.get(k) for k in KEYS})
    _set(DRA_DATA_DIR=TMP, DRA_MODELS_DIR=os.path.join(TMP, "models"), DRA_NO_LOGIN="1", HOST="127.0.0.1")


def tearDownModule():
    _set(**SAVED)
    import app.auth
    app.auth._local_user_id = None
    shutil.rmtree(TMP, ignore_errors=True)


class NoLoginTests(unittest.TestCase):
    def client(self, host="127.0.0.1:8000"):
        from fastapi.testclient import TestClient
        from app.main import app

        return TestClient(app, base_url=f"http://{host}")

    def test_local_admin_without_sign_in(self):
        with self.client() as c:
            status = c.get("/api/auth/status").json()
            self.assertFalse(status["setup_required"])
            self.assertTrue(status["config"]["no_login"])
            self.assertEqual(status["user"]["role"], "admin")
            inc = c.post("/api/incidents", json={"name": "Local incident"})
            self.assertEqual(inc.status_code, 200, inc.text)
            self.assertEqual(c.get("/api/admin/audit").status_code, 200)
            # The built-in account can never be used with a password.
            r = c.post("/api/auth/login", json={"username": "local", "password": "anything-at-all"})
            self.assertEqual(r.status_code, 401)

    def test_requests_to_other_hosts_are_refused(self):
        with self.client("evil.example:8000") as c:
            self.assertEqual(c.get("/api/incidents").status_code, 403)
        with self.client("localhost:8000") as c:
            self.assertEqual(c.get("/api/incidents").status_code, 200)

    def test_refuses_to_start_on_non_loopback_bind(self):
        from app.config import get_config
        from app.main import check_startup

        _set(HOST="0.0.0.0")
        try:
            with self.assertRaises(SystemExit):
                check_startup(get_config())
        finally:
            _set(HOST="127.0.0.1")
        check_startup(get_config())  # loopback is fine


if __name__ == "__main__":
    unittest.main()
