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
