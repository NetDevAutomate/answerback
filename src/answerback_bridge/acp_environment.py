"""Shared network settings for the ACP session children (Kiro's and provider plugins')."""

# Keep these exact names aligned with T-73's process.NETWORK_ENV_KEYS.
# Consolidate the constants when both lanes are merged.
NETWORK_ENV_KEYS: frozenset[str] = frozenset(
    {
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
    }
)
