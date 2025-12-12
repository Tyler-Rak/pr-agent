"""Tests for Bitbucket Server multi-server credential support"""
import pytest
from unittest.mock import patch, MagicMock

from pr_agent.git_providers.utils import get_bitbucket_server_credentials


class TestBitbucketServerCredentials:
    """Tests for get_bitbucket_server_credentials helper function"""

    @patch('pr_agent.git_providers.utils.get_settings')
    def test_multi_server_credentials_with_bearer_token(self, mock_settings):
        """Test getting credentials for multi-server with bearer token"""
        mock_settings.return_value.get.side_effect = lambda key, default=None: {
            "BITBUCKET_SERVER_INSTANCES": {
                "git.rakuten-it.com": {
                    "bearer_token": "token-rakuten-123"
                }
            }
        }.get(key, default)

        pr_url = "https://git.rakuten-it.com/projects/TEST/repos/repo/pull-requests/1"
        bearer_token, username, password = get_bitbucket_server_credentials(pr_url)

        assert bearer_token == "token-rakuten-123"
        assert username is None
        assert password is None

    @patch('pr_agent.git_providers.utils.get_settings')
    def test_multi_server_credentials_with_username_password(self, mock_settings):
        """Test getting credentials for multi-server with username/password"""
        mock_settings.return_value.get.side_effect = lambda key, default=None: {
            "BITBUCKET_SERVER_INSTANCES": {
                "gitpub.example.com": {
                    "username": "admin",
                    "password": "secret123"
                }
            }
        }.get(key, default)

        pr_url = "https://gitpub.example.com/projects/PROJ/repos/repo/pull-requests/5"
        bearer_token, username, password = get_bitbucket_server_credentials(pr_url)

        assert bearer_token is None
        assert username == "admin"
        assert password == "secret123"

    @patch('pr_agent.git_providers.utils.get_settings')
    def test_multi_server_different_servers_different_tokens(self, mock_settings):
        """Test that different servers get different tokens"""
        mock_settings.return_value.get.side_effect = lambda key, default=None: {
            "BITBUCKET_SERVER_INSTANCES": {
                "git.rakuten-it.com": {
                    "bearer_token": "token-rakuten"
                },
                "gitpub.example.com": {
                    "bearer_token": "token-gitpub"
                }
            }
        }.get(key, default)

        # First server
        pr_url1 = "https://git.rakuten-it.com/projects/TEST/repos/repo/pull-requests/1"
        token1, _, _ = get_bitbucket_server_credentials(pr_url1)

        # Second server
        pr_url2 = "https://gitpub.example.com/projects/PROJ/repos/repo/pull-requests/2"
        token2, _, _ = get_bitbucket_server_credentials(pr_url2)

        assert token1 == "token-rakuten"
        assert token2 == "token-gitpub"
        assert token1 != token2

    @patch('pr_agent.git_providers.utils.get_settings')
    def test_legacy_fallback_when_hostname_not_in_instances(self, mock_settings):
        """Test fallback to legacy config when hostname not found in instances"""
        mock_settings.return_value.get.side_effect = lambda key, default=None: {
            "BITBUCKET_SERVER_INSTANCES": {
                "git.rakuten-it.com": {
                    "bearer_token": "token-rakuten"
                }
            },
            "BITBUCKET_SERVER.BEARER_TOKEN": "legacy-token",
            "BITBUCKET_SERVER.USERNAME": "legacy-user",
            "BITBUCKET_SERVER.PASSWORD": "legacy-pass"
        }.get(key, default)

        pr_url = "https://unknown-server.example.com/projects/TEST/repos/repo/pull-requests/1"
        bearer_token, username, password = get_bitbucket_server_credentials(pr_url)

        assert bearer_token == "legacy-token"
        assert username == "legacy-user"
        assert password == "legacy-pass"

    @patch('pr_agent.git_providers.utils.get_settings')
    def test_legacy_fallback_when_no_instances_config(self, mock_settings):
        """Test fallback to legacy config when BITBUCKET_SERVER_INSTANCES not configured"""
        mock_settings.return_value.get.side_effect = lambda key, default=None: {
            "BITBUCKET_SERVER_INSTANCES": {},
            "BITBUCKET_SERVER.BEARER_TOKEN": "legacy-token"
        }.get(key, default)

        pr_url = "https://git.rakuten-it.com/projects/TEST/repos/repo/pull-requests/1"
        bearer_token, username, password = get_bitbucket_server_credentials(pr_url)

        assert bearer_token == "legacy-token"

    @patch('pr_agent.git_providers.utils.get_settings')
    def test_none_when_no_pr_url(self, mock_settings):
        """Test that None is returned when pr_url is None"""
        mock_settings.return_value.get.side_effect = lambda key, default=None: {
            "BITBUCKET_SERVER.BEARER_TOKEN": "legacy-token",
            "BITBUCKET_SERVER.USERNAME": "user",
            "BITBUCKET_SERVER.PASSWORD": "pass"
        }.get(key, default)

        bearer_token, username, password = get_bitbucket_server_credentials(None)

        # Should fall back to legacy config
        assert bearer_token == "legacy-token"
        assert username == "user"
        assert password == "pass"

    @patch('pr_agent.git_providers.utils.get_settings')
    def test_port_in_hostname(self, mock_settings):
        """Test that port numbers in hostname are handled correctly"""
        mock_settings.return_value.get.side_effect = lambda key, default=None: {
            "BITBUCKET_SERVER_INSTANCES": {
                "git.example.com:7990": {
                    "bearer_token": "token-with-port"
                }
            }
        }.get(key, default)

        pr_url = "https://git.example.com:7990/projects/TEST/repos/repo/pull-requests/1"
        bearer_token, username, password = get_bitbucket_server_credentials(pr_url)

        assert bearer_token == "token-with-port"

    @patch('pr_agent.git_providers.utils.get_settings')
    def test_mixed_credentials_bearer_token_wins(self, mock_settings):
        """Test that when both bearer token and username/password are present, both are returned"""
        mock_settings.return_value.get.side_effect = lambda key, default=None: {
            "BITBUCKET_SERVER_INSTANCES": {
                "git.rakuten-it.com": {
                    "bearer_token": "token123",
                    "username": "user",
                    "password": "pass"
                }
            }
        }.get(key, default)

        pr_url = "https://git.rakuten-it.com/projects/TEST/repos/repo/pull-requests/1"
        bearer_token, username, password = get_bitbucket_server_credentials(pr_url)

        # All credentials should be returned
        assert bearer_token == "token123"
        assert username == "user"
        assert password == "pass"


