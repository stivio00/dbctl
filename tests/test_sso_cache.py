"""Tests for the AWS SSO token-cache key resolution used by the SSM tunnel.

The AWS CLI names SSO token-cache files `sha1(<key>).json`, where `<key>`
is the `sso_session` name for session-based profiles or the profile's own
`sso_start_url` for legacy inline-SSO profiles — never the profile name
itself, and never SHA-256. These tests pin down that resolution against
real `~/.aws/config` shapes (via `monkeypatch` + `tmp_path`, no real AWS
CLI or network calls).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from dbctl.tunnels.ssm import _sso_cache_key, _sso_cache_path, _sso_token_is_valid


@pytest.fixture()
def aws_config(tmp_path, monkeypatch):
    config_file = tmp_path / "config"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))

    def write(text: str) -> None:
        config_file.write_text(text)

    return write


# --------------------------------------------------------------------------- #
# _sso_cache_key
# --------------------------------------------------------------------------- #
def test_cache_key_uses_sso_session_name_not_start_url(aws_config):
    aws_config(
        """
[profile Developer-111122223333]
sso_session = acme-dev-workload-session
sso_account_id = 111122223333
sso_role_name = Developer
region = eu-central-1

[sso-session acme-dev-workload-session]
sso_start_url = https://identitycenter.amazonaws.com/ssoins-exampleexample1
sso_region = eu-central-1
sso_registration_scopes = sso:account:access
"""
    )
    assert _sso_cache_key("Developer-111122223333") == "acme-dev-workload-session"


def test_cache_key_falls_back_to_start_url_for_legacy_inline_profile(aws_config):
    aws_config(
        """
[profile legacy-sso]
sso_start_url = https://example.awsapps.com/start
sso_region = eu-central-1
sso_account_id = 123456789012
sso_role_name = Admin
region = eu-central-1
"""
    )
    assert _sso_cache_key("legacy-sso") == "https://example.awsapps.com/start"


def test_cache_key_none_for_unknown_profile(aws_config):
    aws_config("[profile other]\nregion = eu-central-1\n")
    assert _sso_cache_key("does-not-exist") is None


def test_cache_key_none_for_non_sso_profile(aws_config):
    aws_config("[profile access-key-profile]\nregion = eu-central-1\n")
    assert _sso_cache_key("access-key-profile") is None


def test_cache_key_resolves_default_profile(aws_config):
    aws_config(
        """
[default]
sso_session = default-session
region = eu-central-1

[sso-session default-session]
sso_start_url = https://example.awsapps.com/start
sso_region = eu-central-1
"""
    )
    assert _sso_cache_key("default") == "default-session"


def test_cache_key_none_when_config_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "does-not-exist"))
    assert _sso_cache_key("anything") is None


# --------------------------------------------------------------------------- #
# _sso_cache_path
# --------------------------------------------------------------------------- #
def test_cache_path_hashes_session_name_with_sha1_not_sha256(aws_config):
    aws_config(
        """
[profile Developer-111122223333]
sso_session = acme-dev-workload-session

[sso-session acme-dev-workload-session]
sso_start_url = https://identitycenter.amazonaws.com/ssoins-exampleexample1
sso_region = eu-central-1
"""
    )
    path = _sso_cache_path("Developer-111122223333")
    expected_hash = hashlib.sha1(b"acme-dev-workload-session").hexdigest()
    assert path is not None
    assert path.name == f"{expected_hash}.json"
    # Regression guard: must NOT be the old (wrong) sha256-of-profile-name scheme.
    wrong_hash = hashlib.sha256(b"Developer-111122223333").hexdigest()
    assert path.name != f"{wrong_hash}.json"


def test_cache_path_none_for_non_sso_profile(aws_config):
    aws_config("[profile plain]\nregion = eu-central-1\n")
    assert _sso_cache_path("plain") is None


# --------------------------------------------------------------------------- #
# _sso_token_is_valid
# --------------------------------------------------------------------------- #
def test_token_valid_when_cache_file_matches_and_unexpired(tmp_path, monkeypatch, aws_config):
    aws_config(
        """
[profile my-profile]
sso_session = my-session

[sso-session my-session]
sso_start_url = https://example.awsapps.com/start
sso_region = eu-central-1
"""
    )
    monkeypatch.setattr("dbctl.tunnels.ssm.Path.home", staticmethod(lambda: tmp_path))
    cache_dir = tmp_path / ".aws" / "sso" / "cache"
    cache_dir.mkdir(parents=True)
    key_hash = hashlib.sha1(b"my-session").hexdigest()
    (cache_dir / f"{key_hash}.json").write_text(
        json.dumps({"accessToken": "tok", "expiresAt": "2099-01-01T00:00:00Z"})
    )
    assert _sso_token_is_valid("my-profile") is True


def test_token_invalid_when_expired(tmp_path, monkeypatch, aws_config):
    aws_config(
        """
[profile my-profile]
sso_session = my-session

[sso-session my-session]
sso_start_url = https://example.awsapps.com/start
sso_region = eu-central-1
"""
    )
    monkeypatch.setattr("dbctl.tunnels.ssm.Path.home", staticmethod(lambda: tmp_path))
    cache_dir = tmp_path / ".aws" / "sso" / "cache"
    cache_dir.mkdir(parents=True)
    key_hash = hashlib.sha1(b"my-session").hexdigest()
    (cache_dir / f"{key_hash}.json").write_text(
        json.dumps({"accessToken": "tok", "expiresAt": "2000-01-01T00:00:00Z"})
    )
    assert _sso_token_is_valid("my-profile") is False


def test_token_invalid_when_no_cache_file_exists(tmp_path, monkeypatch, aws_config):
    aws_config(
        """
[profile my-profile]
sso_session = my-session

[sso-session my-session]
sso_start_url = https://example.awsapps.com/start
sso_region = eu-central-1
"""
    )
    monkeypatch.setattr("dbctl.tunnels.ssm.Path.home", staticmethod(lambda: tmp_path))
    assert _sso_token_is_valid("my-profile") is False


def test_token_invalid_for_unresolvable_profile(aws_config, tmp_path, monkeypatch):
    aws_config("[profile plain]\nregion = eu-central-1\n")
    monkeypatch.setattr("dbctl.tunnels.ssm.Path.home", staticmethod(lambda: tmp_path))
    assert _sso_token_is_valid("plain") is False
