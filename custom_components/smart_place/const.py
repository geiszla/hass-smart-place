"""Constants for the Smart Place integration."""

from typing import Final

DOMAIN: Final = "smart_place"

CONF_TOKEN: Final = "token"

CONFIG_FLOW_TIMEOUT: Final = 60.0

# How long after the first main-menu reply to wait before entities are
# enumerated (at setup, or later when the first bootstrap is slow). Every
# ``ChartDefinition`` / ``ClimateConfig`` / ``LightConfig`` etc. has
# arrived with ``MainMenuFinished``, but the post-``SocketConnected:1``
# broadcast burst (PACKETBOX<N>, REGEN, HAGEL, TEMPOUT, TEMPIST<N>,
# FEUCHTEIST<N>, SPRECHEN<N>, INFOBOARD<N>INHALT, ...) takes another
# ~300 ms to land. Two seconds gives a generous ~6× headroom on a slow
# link while still being much faster than the previous five-second wait.
SETUP_OBSERVATION_WINDOW: Final = 2.0

# Refresh interval for consumption charts (electricity, water). The
# server doesn't push chart values, so we re-issue
# ``GiveMeChartStandsManuell<id>`` every interval.
CHART_POLL_INTERVAL: Final = 60.0

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

# Give up on the first bootstrap after this long and set the platforms up
# anyway; entities for whatever arrives later are added when it does.
SETUP_BOOTSTRAP_TIMEOUT: Final = 10.0
