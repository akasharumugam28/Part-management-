import re

import pytest

from conftest import create_part, db_rows, login, make_config
from app import create_app

PROTECTED_GETS = [
    "/",
    "/parts/new",
    "/parts/1/edit",
    "/parts/1",
    "/uploads/whatever.png",
]
PROTECTED_POSTS = [
    "/parts/create",
    "/parts/1/update",
    "/parts/1/delete",
]


def test_seed_creates_hashed_admin(app):
    rows = db_rows(app, "SELECT * FROM admin")
    assert len(rows) == 1
    admin = rows[0]
    assert admin["username"] == "admin"
    assert admin["must_change_password"] == 0
    assert admin["password_hash"] != "admin@123"
    assert "admin@123" not in admin["password_hash"]
    assert admin["password_hash"].startswith(("scrypt:", "pbkdf2:"))


def test_seed_refuses_duplicate_and_reset_works(app):
    runner = app.test_cli_runner()
    again = runner.invoke(args=["create-admin"])
    assert again.exit_code != 0
    assert "already exists" in again.output
    reset = runner.invoke(args=["create-admin", "--reset"])
    assert reset.exit_code == 0
    assert len(db_rows(app, "SELECT * FROM admin")) == 1


def test_login_page_and_no_signup(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert b"Log in" in r.data
    for path in ("/register", "/signup"):
        assert client.get(path).status_code == 404


def test_invalid_login(client):
    r = login(client, password="wrong")
    assert r.status_code == 401
    assert b"Invalid username or password." in r.data
    r = login(client, username="nobody", password="admin@123")
    assert r.status_code == 401
    assert b"Invalid username or password." in r.data
    r = client.post("/login", data={"username": "", "password": ""})
    assert r.status_code == 400
    assert b"Please fill in all required fields." in r.data


def test_valid_login_goes_straight_to_dashboard(client):
    r = login(client)
    assert r.status_code == 302
    assert r.location == "/"
    for path in ("/", "/parts/new"):
        assert client.get(path).status_code == 200


def test_login_success_message(client):
    r = login(client)
    page = client.get(r.location).data
    assert b"Login successful." in page


def test_logout_requires_post_and_clears_session(auth_client):
    assert auth_client.get("/logout").status_code == 405
    r = auth_client.post("/logout")
    assert r.status_code == 302 and "/login" in r.location
    r = auth_client.get("/")
    assert r.status_code == 302 and "/login" in r.location


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_protected_get_routes_redirect_to_login(client, path):
    r = client.get(path)
    assert r.status_code == 302
    assert "/login" in r.location


@pytest.mark.parametrize("path", PROTECTED_POSTS)
def test_protected_post_routes_reject_anonymous(app, client, path):
    r = client.post(path, data={"part_name": "X", "part_number": "X", "line_storage": "X", "ols_storage": "X"})
    assert r.status_code == 302 and "/login" in r.location
    assert db_rows(app, "SELECT COUNT(*) FROM parts")[0][0] == 0


def test_open_redirect_is_blocked(client):
    client.post("/logout")
    r = client.post("/login?next=https://evil.example/", data={"username": "admin", "password": "admin@123"})
    assert r.location == "/"
    client.post("/logout")
    r = client.post("/login?next=/parts/new", data={"username": "admin", "password": "admin@123"})
    assert r.location == "/parts/new"


def test_session_cookie_flags(tmp_path):
    app = create_app(make_config(tmp_path, SESSION_COOKIE_SECURE=True))
    app.test_cli_runner().invoke(args=["create-admin"])
    client = app.test_client()
    r = login(client)
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=Lax" in cookie


def test_login_rate_limiting(tmp_path):
    app = create_app(make_config(tmp_path, RATELIMIT_ENABLED=True, LOGIN_RATE_LIMIT="3 per minute"))
    app.test_cli_runner().invoke(args=["create-admin"])
    client = app.test_client()
    statuses = [login(client, password="bad").status_code for _ in range(4)]
    assert statuses == [401, 401, 401, 429]
    r = login(client)  # even correct credentials are blocked while limited
    assert r.status_code == 429
    assert b"Too many login attempts" in r.data
    assert client.get("/login").status_code == 200  # GET is not limited


class TestCsrf:
    @pytest.fixture
    def csrf_client(self, tmp_path):
        app = create_app(make_config(tmp_path, WTF_CSRF_ENABLED=True))
        app.test_cli_runner().invoke(args=["create-admin"])
        return app, app.test_client()

    @staticmethod
    def token(client, path):
        html = client.get(path).get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def test_login_without_token_rejected(self, csrf_client):
        _, client = csrf_client
        r = login(client)
        assert r.status_code == 400
        assert b"session expired" in r.data

    def test_full_flow_with_tokens(self, csrf_client):
        app, client = csrf_client
        tok = self.token(client, "/login")
        r = client.post("/login", data={"username": "admin", "password": "admin@123", "csrf_token": tok})
        assert r.status_code == 302

        # Write without token -> rejected, nothing saved
        r = create_part(client)
        assert r.status_code == 400
        assert b"session expired" in r.data
        assert db_rows(app, "SELECT COUNT(*) FROM parts")[0][0] == 0

        # Same request with the token -> created
        tok = self.token(client, "/parts/new")
        r = create_part(client, csrf_token=tok)
        assert r.status_code == 303
        assert db_rows(app, "SELECT COUNT(*) FROM parts")[0][0] == 1

        # Delete and logout also need the token
        assert client.post("/parts/1/delete").status_code == 400
        assert client.post("/logout").status_code == 400
        assert client.get("/").status_code == 200  # still logged in
