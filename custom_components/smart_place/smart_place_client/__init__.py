"""Standalone Smart Place WebSocket client library.

Has no homeassistant dependency; safe to import from a notebook or CLI.
The HA integration in custom_components/smart_place/ is a thin wrapper.
"""

from .client import (
    CapturedFrame,
    DisconnectCallback,
    ExponentialBackoff,
    FrameHandler,
    ReauthCallback,
    SmartPlaceClient,
    install_token_redaction_filter,
)
from .commands import KNOWN_COMMANDS, CommandDefinition, Commands
from .messages import KNOWN_MESSAGES, parse_frame
from .protocol import (
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
    SmartPlaceOfflineError,
    Temperature,
    UnknownFrame,
    encode_frame,
    parse_chart_references,
    repair_mojibake,
)
from .state import ChartReading, SmartPlaceState, chart_target_status

__all__ = [
    "KNOWN_COMMANDS",
    "KNOWN_MESSAGES",
    "CapturedFrame",
    "ChartReading",
    "CommandDefinition",
    "Commands",
    "DisconnectCallback",
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
    "SmartPlaceOfflineError",
    "SmartPlaceState",
    "Temperature",
    "UnknownFrame",
    "chart_target_status",
    "encode_frame",
    "install_token_redaction_filter",
    "parse_chart_references",
    "parse_frame",
    "repair_mojibake",
]
