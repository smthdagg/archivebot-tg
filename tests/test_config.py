"""配置加载测试。"""

from app.config import Settings


def test_defaults():
    s = Settings()
    assert s.storage_soft_limit_mb == 800
    assert s.storage_hard_limit_mb == 1024
    assert s.storage_cleanup_target_mb == 200
    assert s.max_user_concurrency == 2
    assert s.max_global_concurrency == 4


def test_env_override(monkeypatch):
    monkeypatch.setenv("STORAGE_SOFT_LIMIT_MB", "500")
    monkeypatch.setenv("MAX_USER_CONCURRENCY", "5")
    s = Settings()
    assert s.storage_soft_limit_mb == 500
    assert s.max_user_concurrency == 5


def test_invalid_language_rejected():
    import pytest

    with pytest.raises(ValueError):
        Settings(default_language="fr-FR")


def test_web_admin_ratelimit_defaults():
    s = Settings()
    assert s.web_admin_rate_limit == 100
    assert s.web_admin_rate_window_seconds == 60
    assert s.web_admin_login_max_failures == 5
    assert s.web_admin_login_window_seconds == 900
    assert s.web_admin_login_lockout_seconds == 900


def test_web_admin_ratelimit_env_override(monkeypatch):
    monkeypatch.setenv("WEB_ADMIN_RATE_LIMIT", "50")
    monkeypatch.setenv("WEB_ADMIN_LOGIN_MAX_FAILURES", "3")
    s = Settings()
    assert s.web_admin_rate_limit == 50
    assert s.web_admin_login_max_failures == 3


def test_bytes_properties():
    s = Settings()
    assert s.soft_limit_bytes == 800 * 1024 * 1024
    assert s.hard_limit_bytes == 1024 * 1024 * 1024
    assert s.cleanup_target_bytes == 200 * 1024 * 1024
