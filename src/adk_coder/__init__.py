"""Terminal coding assistant."""

import os
import sys

__version__ = "2.1.0"

# Ensure Windows cmd.exe / PowerShell consoles never crash with cp1252 UnicodeEncodeError
# when rendering Rich UI banners, spinners, or Markdown output.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

# Ensure macOS python.org builds (which omit system OpenSSL CA bundles) verify HTTPS cleanly
# across aiohttp, httpx, litellm, openai, google-genai, and urllib.
if not os.environ.get("SSL_CERT_FILE"):
    try:
        import certifi

        _ca_bundle = certifi.where()
        os.environ.setdefault("SSL_CERT_FILE", _ca_bundle)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca_bundle)
    except ImportError:
        pass
