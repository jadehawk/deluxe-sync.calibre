"""Persistent local settings for Deluxe Sync."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from calibre.utils.config import JSONConfig

from calibre_plugins.deluxe_sync.api import DEFAULT_SERVER_URL


DEFAULT_PROFILE_ID = "primary"

_PREFS = JSONConfig("plugins/deluxe_sync")
_PREFS.defaults["server_url"] = DEFAULT_SERVER_URL
_PREFS.defaults["client_uuid"] = ""
_PREFS.defaults["registrations"] = {}
_PREFS.defaults["server_profiles"] = {}
_PREFS.defaults["active_server_profile_id"] = DEFAULT_PROFILE_ID
_PREFS.defaults["server_profiles_migrated"] = False
_PREFS.defaults["column_mappings"] = {}
_PREFS.defaults["metadata_policies"] = {}


def get_server_url() -> str:
    profile = get_active_server_profile()
    if profile:
        value = profile.get("server_url")
        if isinstance(value, str) and value.strip():
            return value.strip()

    value = _PREFS.get("server_url", DEFAULT_SERVER_URL)
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_SERVER_URL
    return value.strip()


def set_server_url(server_url: str) -> None:
    _PREFS["server_url"] = server_url


def get_column_mappings() -> dict[str, str]:
    value = _PREFS.get("column_mappings", {})
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(mapped or "")
        for key, mapped in value.items()
        if isinstance(key, str)
    }


def set_column_mappings(mappings: dict[str, str]) -> None:
    _PREFS["column_mappings"] = {
        str(key): str(mapped or "")
        for key, mapped in mappings.items()
        if isinstance(key, str)
    }


def get_metadata_policies() -> dict[str, str]:
    value = _PREFS.get("metadata_policies", {})
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(policy or "")
        for key, policy in value.items()
        if isinstance(key, str)
    }


def set_metadata_policies(policies: dict[str, str]) -> None:
    _PREFS["metadata_policies"] = {
        str(key): str(policy or "")
        for key, policy in policies.items()
        if isinstance(key, str)
    }


def get_or_create_client_uuid() -> str:
    value = _PREFS.get("client_uuid", "")
    if isinstance(value, str) and value.strip():
        return value.strip()

    value = str(uuid4())
    _PREFS["client_uuid"] = value
    return value


def _all_registrations() -> dict:
    value = _PREFS.get("registrations", {})
    if not isinstance(value, dict):
        return {}
    return deepcopy(value)


def _all_server_profiles() -> dict[str, dict]:
    value = _PREFS.get("server_profiles", {})
    if not isinstance(value, dict):
        return {}
    return {
        str(key): deepcopy(profile)
        for key, profile in value.items()
        if isinstance(key, str) and isinstance(profile, dict)
    }


def _ensure_profile_migration() -> None:
    if bool(_PREFS.get("server_profiles_migrated", False)):
        return

    profiles = _all_server_profiles()
    if not profiles:
        legacy = _all_registrations()
        if legacy:
            preferred_url = str(_PREFS.get("server_url", "") or "").strip().rstrip("/").lower()
            selected = None
            for registration in legacy.values():
                if not isinstance(registration, dict):
                    continue
                candidate_url = str(registration.get("server_url") or "").strip().rstrip("/").lower()
                if preferred_url and candidate_url == preferred_url:
                    selected = registration
                    break
                if selected is None:
                    selected = registration

            if isinstance(selected, dict):
                profile = deepcopy(selected)
                profile.pop("library_uuid", None)
                profile.setdefault("id", DEFAULT_PROFILE_ID)
                profile.setdefault("name", "Primary Server")
                profile.setdefault("auth_mode", "pairing")
                profiles[DEFAULT_PROFILE_ID] = profile
                _PREFS["server_profiles"] = profiles
                _PREFS["active_server_profile_id"] = DEFAULT_PROFILE_ID

    _PREFS["server_profiles_migrated"] = True


def get_server_profiles() -> dict[str, dict]:
    _ensure_profile_migration()
    return _all_server_profiles()


def get_active_server_profile_id() -> str:
    _ensure_profile_migration()
    value = _PREFS.get("active_server_profile_id", DEFAULT_PROFILE_ID)
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_PROFILE_ID
    return value.strip()


def set_active_server_profile_id(profile_id: str) -> None:
    clean = str(profile_id or "").strip() or DEFAULT_PROFILE_ID
    _PREFS["active_server_profile_id"] = clean


def get_active_server_profile() -> dict | None:
    profiles = get_server_profiles()
    value = profiles.get(get_active_server_profile_id())
    return deepcopy(value) if isinstance(value, dict) else None


def save_server_profile(profile: dict, profile_id: str = DEFAULT_PROFILE_ID) -> None:
    clean_id = str(profile_id or "").strip() or DEFAULT_PROFILE_ID
    profiles = get_server_profiles()
    saved = deepcopy(profile)
    saved["id"] = clean_id
    saved.setdefault("name", "Primary Server")
    profiles[clean_id] = saved
    _PREFS["server_profiles"] = profiles
    _PREFS["active_server_profile_id"] = clean_id
    _PREFS["server_profiles_migrated"] = True
    server_url = saved.get("server_url")
    if isinstance(server_url, str) and server_url.strip():
        _PREFS["server_url"] = server_url.strip()


def forget_server_profile(profile_id: str | None = None) -> None:
    clean_id = str(profile_id or get_active_server_profile_id()).strip() or DEFAULT_PROFILE_ID
    profiles = get_server_profiles()
    profiles.pop(clean_id, None)
    _PREFS["server_profiles"] = profiles
    _PREFS["server_profiles_migrated"] = True
    if get_active_server_profile_id() == clean_id:
        remaining = next(iter(profiles), DEFAULT_PROFILE_ID)
        _PREFS["active_server_profile_id"] = remaining


# Compatibility wrappers for Stage 1/2 callers and previously installed settings.
# Registration is now installation-level, so library_uuid is intentionally ignored.
def get_registration(library_uuid: str = "") -> dict | None:
    return get_active_server_profile()


def save_registration(library_uuid: str, registration: dict) -> None:
    save_server_profile(registration)


def forget_registration(library_uuid: str = "") -> None:
    forget_server_profile()
