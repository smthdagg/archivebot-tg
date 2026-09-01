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


def test_bytes_properties():
    s = Settings()
    assert s.soft_limit_bytes == 800 * 1024 * 1024
    assert s.hard_limit_bytes == 1024 * 1024 * 1024
    assert s.cleanup_target_bytes == 200 * 1024 * 1024
