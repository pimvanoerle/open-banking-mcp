import pytest

from open_banking_mcp.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        client_id="sandbox-test",
        client_secret="secret-abc",
        redirect_uri="http://localhost:18080/callback",
        env="sandbox",
        scopes="info accounts balance offline_access",
        providers="uk-cs-mock",
        token_file=tmp_path / "tokens.json",
        use_keyring=False,
        cache_file=tmp_path / "cache.db",
        max_age_hours=25.0,
        history_days=365,
        psu_ip=None,
    )
