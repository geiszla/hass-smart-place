"""Constants for the Smart Place integration."""

from typing import Final

DOMAIN: Final = "smart_place"

CONF_TOKEN: Final = "token"

CONFIG_FLOW_TIMEOUT: Final = 60.0

# How long after the first value burst began to wait before entities are
# enumerated (at setup, or later when the first bootstrap is slow). The
# server sends every value (``leuchte<N>``, ``JALICO<N>``, ``TEMPSOLL<N>``,
# ..., ``WINDALARM<N>``, ``SPRECHEN<N>``, ``CALLINFO<N>``, ``PERSINFO``,
# ``SZENEN<N>``, ...) in one burst of about 100 frames within a few
# milliseconds, without a marker at its end. Two seconds is generous
# headroom on a slow link.
SETUP_OBSERVATION_WINDOW: Final = 2.0

# How long after a main-menu reply to wait for that value burst before the
# entities are enumerated without it. The burst follows the reply 0.1 to
# 4.8 s later (four captures, 2026-11-09/10).
VALUE_BURST_TIMEOUT: Final = 10.0

# Refresh interval for consumption charts (electricity, water). The
# server doesn't push chart values, so we re-issue
# ``GiveMeChartStandsManuell<id>`` every interval. Not shorter: the server
# needs about 3.5 s to answer one round and holds back every command sent
# meanwhile (a light then reacts that much later), and its chart values
# change only every 15 minutes.
CHART_POLL_INTERVAL: Final = 300.0

# Diagnostic frame capture. When set to a path, the live client tees
# EVERY frame (both directions, token-redacted) to this ndjson file —
# the same mechanism the CLI exposes as ``--capture``. ``None`` disables
# it (the normal state). Flip to a path to record real-world frames on
# the deployed box (e.g. a doorbell press) for offline analysis, then set
# back to ``None`` and redeploy. Keep it off in production: the write is
# synchronous (blocking I/O in the event loop on every frame, ~2-3x/s)
# and appends forever, so the file grows unbounded if left on.
CAPTURE_PATH: Final[str | None] = None

# Developer dry run. When ``True`` the client does not transmit any command
# that changes something in the building (lights, blinds, heating, scenes,
# doors): it logs ``dry run: would send <name> (<wire>)`` at WARNING instead.
# Reads (bootstrap, heartbeat, chart polls) are unaffected. The Connection
# binary sensor carries the attribute ``dry_run: true`` while it is on, so
# tooling can check it before calling a service. Must be ``False`` in a
# release.
DRY_RUN: Final = False

# Config-entry flag: the one-time area assignment of the heating-zone
# devices has run (see ``__init__.py``).
CONF_ZONE_AREAS_SET: Final = "zone_areas_set"

# Config-entry option: vendor light ids of the sockets no command may be
# sent for (see ``entity.py``). Read on every command, so a change takes
# effect without a reload.
CONF_PROTECTED_SOCKETS: Final = "protected_sockets"

# Config-entry options of "Lower after weather alarm" (``switch.py``): the
# vendor ids of the blinds whose switch is on, and the wait between the end
# of an alarm and the down command in whole minutes (0 if not set).
CONF_LOWER_AFTER_ALARM: Final = "lower_after_alarm"
CONF_LOWER_DELAY_MINUTES: Final = "lower_delay_minutes"

# Give up on the first bootstrap after this long and set the platforms up
# anyway; entities for whatever arrives later are added when it does.
SETUP_BOOTSTRAP_TIMEOUT: Final = 10.0
