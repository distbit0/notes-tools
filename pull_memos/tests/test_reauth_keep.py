from pathlib import Path

from reauth_keep import upsert_env_var


def test_upsert_env_var_replaces_existing_export(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('export username="alice"\nexport masterKey="old-token"\n')

    upsert_env_var(env_file, "masterKey", "new-token")

    assert env_file.read_text() == 'export username="alice"\nexport masterKey="new-token"\n'


def test_upsert_env_var_appends_if_missing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('export username="alice"\n')

    upsert_env_var(env_file, "masterKey", "new-token")

    assert env_file.read_text() == 'export username="alice"\nexport masterKey="new-token"\n'
