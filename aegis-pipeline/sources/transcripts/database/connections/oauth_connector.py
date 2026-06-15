"""OAuth 2.0 client credentials flow with lazy token refresh."""

import logging
import time
from threading import Lock

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

REFRESH_BUFFER_SECONDS = 300
REQUEST_TIMEOUT = 30


def _should_retry_with_body_credentials(response) -> bool:
    """Return whether a 400 token response supports credential fallback."""
    if response.status_code != 400:
        return False
    try:
        error_data = response.json()
    except ValueError:
        return False

    error_value = str(error_data.get("error", "")).lower()
    description = str(error_data.get("error_description", "")).lower()
    return (
        error_value in {"invalid_client", "unauthorized_client"}
        or "basic" in description
        or "client credential" in description
        or "client_secret" in description
        or "client_id" in description
    )


class OAuthClient:
    """Manages OAuth token lifecycle with on-demand refresh.

    Fetches tokens via client credentials grant. Caches the
    token and refreshes automatically when it nears expiry.

    config must include token_endpoint, client_id, and client_secret. Calling
    get_token may perform a live HTTP request to the configured token endpoint.
    """

    def __init__(self, config: dict, verify_ssl: bool = True):
        """Store OAuth settings without fetching a token."""
        self.token_endpoint = config["token_endpoint"]
        self.client_id = config["client_id"]
        self.client_secret = config["client_secret"]
        self.grant_type = config.get("grant_type", "client_credentials")
        self.scope = config.get("scope", "")
        self.verify_ssl = verify_ssl
        self._access_token = ""
        self._expires_at = 0.0
        self._refresh_lock = Lock()

    def get_token(self) -> str:
        """Get a valid access token, refreshing if needed.

        Returns the cached bearer token when it is still valid. Otherwise this
        method performs a live token request and raises requests exceptions or
        response parsing errors from the underlying fetch.
        """
        if self.is_expired():
            with self._refresh_lock:
                if self.is_expired():
                    self._fetch_token()
        return self._access_token

    def is_expired(self) -> bool:
        """Return True when the token is missing or within the refresh buffer."""
        if not self._access_token:
            return True
        remaining = self._expires_at - time.time()
        return remaining <= REFRESH_BUFFER_SECONDS

    def _fetch_token(self) -> None:
        """Fetch and cache a new token via client credentials grant."""
        logger.info(
            "Fetching OAuth token from %s",
            self.token_endpoint,
        )

        data = {"grant_type": self.grant_type}
        if self.scope:
            data["scope"] = self.scope

        auth = HTTPBasicAuth(self.client_id, self.client_secret)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        response = requests.post(
            self.token_endpoint,
            data=data,
            auth=auth,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            verify=self.verify_ssl,
        )

        if _should_retry_with_body_credentials(response):
            logger.info("Basic auth failed, retrying with body credentials")
            data["client_id"] = self.client_id
            data["client_secret"] = self.client_secret
            response = requests.post(
                self.token_endpoint,
                data=data,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                verify=self.verify_ssl,
            )

        response.raise_for_status()
        token_data = response.json()

        self._access_token = token_data["access_token"]
        expires_in = token_data.get("expires_in", 3600)
        self._expires_at = time.time() + expires_in

        logger.info(
            "OAuth token obtained (expires in %ds)",
            expires_in,
        )
