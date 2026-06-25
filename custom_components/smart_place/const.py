"""Constants for the Smart Place integration."""

from typing import Final

DOMAIN: Final = "smart_place"

CONF_TOKEN: Final = "token"

CONFIG_FLOW_TIMEOUT: Final = 60.0

# How long after bootstrap to wait before forwarding entity platforms.
# ``wait_for_bootstrap`` returns on ``MainMenuFinished`` (every
# ``ChartDefinition`` / ``ClimateConfig`` / ``LightConfig`` etc. has
# arrived by then), but the post-``SocketConnected:1`` broadcast burst
# (PACKETBOX<N>, REGEN, HAGEL, TEMPOUT, TEMPIST<N>, FEUCHTEIST<N>,
# SPRECHEN<N>, INFOBOARD<N>INHALT, ...) takes another ~300 ms to land.
# Two seconds gives a generous ~6× headroom on a slow link while still
# being much faster than the previous five-second wait.
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
