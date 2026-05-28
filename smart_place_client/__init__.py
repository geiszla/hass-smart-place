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
from smart_place_client.commands import KNOWN_COMMANDS, CommandDefinition, Commands
from smart_place_client.messages import KNOWN_MESSAGES, parse_frame
from smart_place_client.protocol import (
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
    encode_frame,
    parse_chart_references,
)
from smart_place_client.state import ChartReading, SmartPlaceState

__all__ = [
    "KNOWN_COMMANDS",
    "KNOWN_MESSAGES",
    "CapturedFrame",
    "ChartReading",
    "CommandDefinition",
    "Commands",
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
    "SmartPlaceState",
    "Temperature",
    "UnknownFrame",
    "encode_frame",
    "install_token_redaction_filter",
    "parse_chart_references",
    "parse_frame",
]
