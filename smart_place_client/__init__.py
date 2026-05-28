"""Standalone Smart Place WebSocket client library.

Has no homeassistant dependency; safe to import from a notebook or CLI.
The HA integration in custom_components/smart_place/ is a thin wrapper.
"""

from smart_place_client.client import (
    CapturedFrame,
    ExponentialBackoff,
    FrameHandler,
    ReauthCallback,
    SmartPlaceClient,
    install_token_redaction_filter,
)
from smart_place_client.protocol import (
    KNOWN_MESSAGES,
    GlobalConfig,
    GoToLinkOldSystem,
    GoToLinkSSL,
    HostNotOnline,
    MessageDefinition,
    NamedFields,
    NamedValue,
    ProtocolError,
    ServerFrame,
    SessionPhase,
    SessionState,
    SmartPlaceAuthError,
    Temperature,
    UnknownFrame,
    encode_frame,
    encode_global_config_request,
    encode_status_liste_request,
    parse_frame,
)

__all__ = [
    "KNOWN_MESSAGES",
    "CapturedFrame",
    "ExponentialBackoff",
    "FrameHandler",
    "GlobalConfig",
    "GoToLinkOldSystem",
    "GoToLinkSSL",
    "HostNotOnline",
    "MessageDefinition",
    "NamedFields",
    "NamedValue",
    "ProtocolError",
    "ReauthCallback",
    "ServerFrame",
    "SessionPhase",
    "SessionState",
    "SmartPlaceAuthError",
    "SmartPlaceClient",
    "Temperature",
    "UnknownFrame",
    "encode_frame",
    "encode_global_config_request",
    "encode_status_liste_request",
    "install_token_redaction_filter",
    "parse_frame",
]
