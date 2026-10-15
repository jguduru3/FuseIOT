"""User accounts, roles, tokens, login throttling and the Authenticator."""
import time
from types import SimpleNamespace

import pytest

import fuseiot.auth as auth_module
from fuseiot.auth import (
    Authenticator,
    LoginThrottle,
    Principal,
    UserStore,
    hash_password,
    role_allows,
    verify_password,
)
from fuseiot.exceptions import AuthenticationError, AuthorizationError, ConfigurationError

PW = "correct horse"


@pytest.fixture
def store(tmp_path, fast_scrypt):
    s = UserStore(str(tmp_path / "sub" / "users.db"))
    yield s
    s.close()


@pytest.fixture
def populated(store):
    store.add_user("admin", PW, "admin")
    store.add_user("op", PW, "operator")
    store.add_user("viewer", PW, "viewer")
    return store


class TestPasswords:
    def test_hash_and_verify(self):
        encoded = hash_password(PW)
        assert encoded.startswith("scrypt$16384$8$1$") and PW not in encoded
        assert verify_password(PW, encoded) and not verify_password("wrong", encoded)

    def test_salted(self):
        assert hash_password(PW) != hash_password(PW)

    @pytest.mark.parametrize("encoded", ["", "plain", "bcrypt$1$2$3$aa$bb", "scrypt$x$8$1$aa$bb",
                                         "scrypt$16384$8$1$zz$bb"])
    def test_malformed_hashes_never_verify(self, encoded):
        assert verify_password(PW, encoded) is False

    def test_roles(self):
        assert role_allows("admin", "viewer") and role_allows("operator", "operator")
        assert not role_allows("viewer", "operator")
        p = Principal("u", "operator", "api")
        assert p.can("viewer") and not p.can("admin")
        assert p.to_dict() == {"username": "u", "role": "operator", "token_kind": "api"}


class TestUsers:
    def test_add_list_creates_parent_dir(self, store, tmp_path):
        assert not store.has_users()
        store.add_user("alice@home", PW, "admin")
        store.add_user("bob", PW)
        assert (tmp_path / "sub" / "users.db").exists()
        users = store.list_users()
        assert [(u["username"], u["role"], u["disabled"]) for u in users] == [
            ("alice@home", "admin", False), ("bob", "viewer", False)]
        assert store.has_users()

    @pytest.mark.parametrize("username", ["", "with space", "x" * 65, "semi;colon", "slash/"])
    def test_invalid_usernames(self, store, username):
        with pytest.raises(ConfigurationError, match="Username may contain"):
            store.add_user(username, PW)

    def test_valid_username_characters(self, store):
        store.add_user("a.b_c-d@e", PW)
        store.add_user("x" * 64, PW)

    def test_validation(self, store):
        with pytest.raises(ConfigurationError, match="at least 8"):
            store.add_user("u", "short")
        with pytest.raises(ConfigurationError, match="Role must be"):
            store.add_user("u", PW, "root")
        store.add_user("u", PW)
        with pytest.raises(ConfigurationError, match="already exists"):
            store.add_user("u", PW)

    def test_authenticate(self, populated):
        p = populated.authenticate("op", PW)
        assert (p.username, p.role, p.token_kind) == ("op", "operator", "session")
        with pytest.raises(AuthenticationError):
            populated.authenticate("op", "wrong password")
        with pytest.raises(AuthenticationError):
            populated.authenticate("ghost", PW)

    def test_set_role(self, populated):
        populated.set_role("viewer", "operator")
        assert {u["username"]: u["role"] for u in populated.list_users()}["viewer"] == "operator"
        with pytest.raises(ConfigurationError, match="Role must be"):
            populated.set_role("viewer", "god")
        with pytest.raises(ConfigurationError, match="No such user"):
            populated.set_role("ghost", "viewer")

    def test_last_admin_protections(self, populated):
        with pytest.raises(ConfigurationError, match="demote the last admin"):
            populated.set_role("admin", "viewer")
        with pytest.raises(ConfigurationError, match="disable the last admin"):
            populated.set_disabled("admin", True)
        with pytest.raises(ConfigurationError, match="remove the last admin"):
            populated.remove_user("admin")
        populated.set_role("admin", "admin")  # no-op keeps admin

    def test_second_admin_allows_changes(self, populated):
        populated.set_role("op", "admin")
        populated.set_role("admin", "viewer")
        with pytest.raises(ConfigurationError, match="last admin"):
            populated.remove_user("op")

    def test_disabled_admin_does_not_count(self, populated):
        populated.add_user("admin2", PW, "admin")
        populated.set_disabled("admin2", True)
        with pytest.raises(ConfigurationError, match="last admin"):
            populated.remove_user("admin")

    def test_set_password_revokes_sessions_not_api_tokens(self, populated):
        session, _ = populated.issue_token("op")
        api, _ = populated.issue_token("op", name="script", kind="api")
        populated.set_password("op", "new password!")
        assert populated.resolve_token(session) is None
        assert populated.resolve_token(api).username == "op"
        with pytest.raises(AuthenticationError):
            populated.authenticate("op", PW)
        assert populated.authenticate("op", "new password!").username == "op"
        with pytest.raises(ConfigurationError, match="at least 8"):
            populated.set_password("op", "x")
        with pytest.raises(ConfigurationError, match="No such user"):
            populated.set_password("ghost", PW)

    def test_disable_revokes_all_tokens_and_blocks_login(self, populated):
        session, _ = populated.issue_token("op")
        api, _ = populated.issue_token("op", kind="api")
        populated.set_disabled("op", True)
        assert populated.resolve_token(session) is None and populated.resolve_token(api) is None
        assert populated.list_users()[1]["disabled"] is True
        with pytest.raises(AuthenticationError):
            populated.authenticate("op", PW)
        populated.set_disabled("op", False)
        assert populated.authenticate("op", PW).username == "op"
        assert populated.resolve_token(api) is None  # revoked tokens stay revoked

    def test_remove_user_cascades_tokens(self, populated):
        api, _ = populated.issue_token("viewer", kind="api")
        populated.remove_user("viewer")
        assert "viewer" not in [u["username"] for u in populated.list_users()]
        assert populated.resolve_token(api) is None
        with pytest.raises(ConfigurationError, match="No such user"):
            populated.remove_user("viewer")

    def test_persists_across_instances(self, tmp_path):
        path = str(tmp_path / "u.db")
        s1 = UserStore(path)
        s1.add_user("admin", PW, "admin")
        token, _ = s1.issue_token("admin", kind="api")
        s1.close()
        s2 = UserStore(path)
        try:
            assert s2.resolve_token(token).username == "admin"
            assert s2.authenticate("admin", PW).role == "admin"
        finally:
            s2.close()

    def test_in_memory_store(self):
        s = UserStore(":memory:")
        try:
            s.add_user("m", PW)
            assert s.has_users()
        finally:
            s.close()


class TestTokens:
    def test_session_token_expiry(self, populated):
        token, expires = populated.issue_token("op")
        assert token.startswith("fio_") and len(token) > 40
        assert expires == pytest.approx(time.time() + populated.session_ttl, abs=5)
        p = populated.resolve_token(token)
        assert (p.username, p.role, p.token_kind) == ("op", "operator", "session")

    def test_api_token_never_expires_by_default(self, populated):
        token, expires = populated.issue_token("op", kind="api")
        assert expires is None and populated.resolve_token(token).token_kind == "api"

    def test_api_token_with_ttl(self, populated):
        _, expires = populated.issue_token("op", kind="api", ttl=60)
        assert expires == pytest.approx(time.time() + 60, abs=5)

    def test_expired_token_rejected(self, populated):
        token, _ = populated.issue_token("op", ttl=0.01)
        time.sleep(0.05)
        assert populated.resolve_token(token) is None

    def test_role_change_applies_to_existing_tokens(self, populated):
        token, _ = populated.issue_token("viewer", kind="api")
        populated.set_role("viewer", "operator")
        assert populated.resolve_token(token).role == "operator"

    def test_invalid_tokens(self, populated):
        assert populated.resolve_token("not-a-fio-token") is None
        assert populated.resolve_token("fio_doesnotexist") is None
        with pytest.raises(ValueError):
            populated.issue_token("op", kind="service")
        with pytest.raises(ConfigurationError, match="No such user"):
            populated.issue_token("ghost")

    def test_revoke_token(self, populated):
        token, _ = populated.issue_token("op")
        assert populated.revoke_token(token) is True
        assert populated.revoke_token(token) is False
        assert populated.resolve_token(token) is None

    def test_list_and_revoke_by_id(self, populated):
        populated.issue_token("op")  # sessions are not listed
        t1, _ = populated.issue_token("op", name="phone", kind="api")
        t2, _ = populated.issue_token("op", name="x" * 100, kind="api")
        listed = populated.list_tokens("op")
        assert [t["name"] for t in listed] == ["phone", "x" * 64]
        assert all(len(t["id"]) == 12 and t["kind"] == "api" for t in listed)
        assert populated.list_tokens("admin") == []
        # Another user cannot revoke op's token by id.
        assert populated.revoke_token_id("admin", listed[0]["id"]) is False
        assert populated.revoke_token_id("op", listed[0]["id"][:7]) is False  # too short
        assert populated.revoke_token_id("op", listed[0]["id"]) is True
        assert populated.resolve_token(t1) is None and populated.resolve_token(t2) is not None

    def test_last_used_recorded(self, populated):
        token, _ = populated.issue_token("op", kind="api")
        assert populated.list_tokens("op")[0]["last_used"] is None
        populated.resolve_token(token)
        assert populated.list_tokens("op")[0]["last_used"] is not None

    def test_wildcard_id_revokes_nothing(self, populated):
        populated.issue_token("op", name="a", kind="api")
        populated.issue_token("op", name="b", kind="api")
        assert populated.revoke_token_id("op", "%%%%%%%%") is False
        assert len(populated.list_tokens("op")) == 2


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(auth_module, "time", SimpleNamespace(monotonic=c.monotonic, time=time.time))
    return c


class TestLoginThrottle:
    def test_blocks_after_max_failures(self, clock):
        t = LoginThrottle(max_failures=3, window=60)
        for _ in range(2):
            t.failed("1.2.3.4")
        assert t.blocked_for("1.2.3.4") == 0
        t.failed("1.2.3.4")
        assert t.blocked_for("1.2.3.4") == pytest.approx(60)
        assert t.blocked_for("5.6.7.8") == 0  # per client
        clock.now += 30
        assert t.blocked_for("1.2.3.4") == pytest.approx(30)
        clock.now += 31
        assert t.blocked_for("1.2.3.4") == 0

    def test_success_resets(self, clock):
        t = LoginThrottle(max_failures=2, window=60)
        t.failed("c")
        t.succeeded("c")
        t.failed("c")
        assert t.blocked_for("c") == 0

    def test_old_failures_expire(self, clock):
        t = LoginThrottle(max_failures=2, window=10)
        t.failed("c")
        clock.now += 11
        t.failed("c")
        assert t.blocked_for("c") == 0


class TestAuthenticator:
    def test_disabled_without_users_or_token(self, store):
        assert not Authenticator().enabled
        a = Authenticator(users=store)
        assert not a.enabled
        p = a.resolve(None)
        assert (p.username, p.role, p.token_kind) == ("anonymous", "admin", "anonymous")

    def test_enabled_by_service_token(self):
        a = Authenticator(service_token="svc")
        assert a.enabled
        assert a.resolve("svc").to_dict() == {"username": "service", "role": "admin", "token_kind": "service"}
        for bad in (None, "", "svc2", "SVC"):
            with pytest.raises(AuthenticationError):
                a.resolve(bad)

    def test_enabled_by_users(self, populated):
        a = Authenticator(users=populated)
        assert a.enabled
        token, _ = populated.issue_token("viewer", kind="api")
        assert a.resolve(token).role == "viewer"
        with pytest.raises(AuthenticationError):
            a.resolve("fio_bogus")

    def test_service_token_and_users_together(self, populated):
        a = Authenticator(users=populated, service_token="svc")
        token, _ = populated.issue_token("op")
        assert a.resolve("svc").token_kind == "service" and a.resolve(token).username == "op"

    def test_login(self, populated):
        a = Authenticator(users=populated)
        token, expires, principal = a.login("admin", PW, "10.0.0.1")
        assert principal.role == "admin" and expires is not None
        assert a.resolve(token).token_kind == "session"

    def test_login_without_users(self):
        with pytest.raises(ConfigurationError, match="users_db"):
            Authenticator(service_token="x").login("a", "b", "c")

    def test_login_throttling(self, populated, clock):
        a = Authenticator(users=populated)
        for _ in range(5):
            with pytest.raises(AuthenticationError):
                a.login("admin", "bad password", "10.0.0.9")
        with pytest.raises(AuthorizationError, match="Too many failed logins") as info:
            a.login("admin", PW, "10.0.0.9")  # blocked even with the right password
        assert info.value.details["retry_after"] == 301
        assert a.login("admin", PW, "10.0.0.10")[2].username == "admin"  # other client fine
        clock.now += 301
        assert a.login("admin", PW, "10.0.0.9")[2].username == "admin"
