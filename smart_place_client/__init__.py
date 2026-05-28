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
from smart_place_client.messages import KNOWN_MESSAGES, parse_frame
from smart_place_client.protocol import (
    OPEN_FRONT_DOOR,
    OPEN_GROUND_FLOOR_ENTRANCE,
    GlobalConfig,
    GoToLinkSSL,
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
    encode_chart_stands_request,
    encode_frame,
    encode_global_config_request,
    encode_status_inhalt_liste_request,
    encode_status_liste_request,
)

__all__ = [
    "KNOWN_MESSAGES",
    "OPEN_FRONT_DOOR",
    "OPEN_GROUND_FLOOR_ENTRANCE",
    "CapturedFrame",
    "ExponentialBackoff",
    "FrameHandler",
    "GlobalConfig",
    "GoToLinkSSL",
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
    "encode_chart_stands_request",
    "encode_frame",
    "encode_global_config_request",
    "encode_status_inhalt_liste_request",
    "encode_status_liste_request",
    "install_token_redaction_filter",
    "parse_frame",
]
