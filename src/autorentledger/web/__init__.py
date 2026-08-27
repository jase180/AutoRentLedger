"""Local read-only web adapter."""

from autorentledger.web.app import create_app
from autorentledger.web.auth import (
    AUTH_CONFIGURATION_ERROR,
    PASSWORD_HASH_ENV,
    SECRET_KEY_ENV,
    WebAuthConfig,
    WebAuthConfigurationError,
    load_web_auth_config,
)

__all__ = [
    "AUTH_CONFIGURATION_ERROR",
    "PASSWORD_HASH_ENV",
    "SECRET_KEY_ENV",
    "WebAuthConfig",
    "WebAuthConfigurationError",
    "create_app",
    "load_web_auth_config",
]
