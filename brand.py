# brand.py — Central branding & panel-path configuration.
#
# Every user-visible brand string (panel name, footer, support/channel links,
# config remark prefix, subscription base path) is resolved from here instead of
# being hard-coded across main.py / pages.py / static HTML. Values can be
# overridden at runtime from panel settings (SETTINGS["brand"]) or from the
# environment, so the panel can be re-branded without touching code.
import os


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or "").strip() or default


def normalize_panel_path(raw: str) -> str:
    """Return a safe absolute panel path like ``/panel`` (no trailing slash)."""
    p = (raw or "").strip()
    if not p.startswith("/"):
        p = "/" + p
    p = "/" + "/".join(seg for seg in p.split("/") if seg)
    # "/" would collide with the root route; fall back to the default path.
    return p if p not in ("", "/") else "/panel"


# Default brand. Overridable via env. Runtime overrides live in SETTINGS["brand"].
BRAND = {
    "panel_name": _env("PANEL_BRAND", "PANAHANNET"),
    "panel_name_fa": _env("PANEL_BRAND_FA", "پنل پناهاننت"),
    "footer_text": _env("PANEL_FOOTER", "Powered by PANAHANNET"),
    "name_prefix": _env("NAME_PREFIX", "PANAHANNET"),
    "support_url": _env("SUPPORT_URL", "https://t.me/PenhanNetvpnbot"),
    "channel_url": _env("CHANNEL_URL", "https://t.me/PenhanNetvpnbot"),
    "contact_url": _env("CONTACT_URL", "https://t.me/PenhanNetvpnbot"),
    "bot_username": _env("BOT_USERNAME", "@PenhanNetvpnbot"),
    "bot_url": _env("BOT_URL", "https://t.me/PenhanNetvpnbot"),
    "bot_token": _env("BOT_TOKEN", ""),
    "admin_ids": _env("ADMIN_IDS", ""),
    "logo": _env("PANEL_LOGO", "/static/panahannet-logo.svg"),
    "panel_path": normalize_panel_path(_env("PANEL_PATH", "/panel")),
    "sub_prefix": "/sub",
    "credit": _env("PANEL_CREDIT", "PANAHANNET Panel"),
}

# The panel (admin) route. Resolved once at import; changing it from settings
# takes effect after the next restart (documented in the settings UI).
PANEL_PATH = BRAND["panel_path"]

# Keys that may be edited from the panel settings UI.
EDITABLE_BRAND_KEYS = (
    "panel_name",
    "panel_name_fa",
    "footer_text",
    "name_prefix",
    "support_url",
    "channel_url",
    "contact_url",
    "bot_username",
    "bot_url",
)
