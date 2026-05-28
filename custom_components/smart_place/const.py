"""Constants for the Smart Place integration."""

from typing import Final

DOMAIN: Final = "smart_place"

CONF_TOKEN: Final = "token"

CONFIG_FLOW_TIMEOUT: Final = 60.0

# How long after bootstrap to wait before forwarding entity platforms.
# Bootstrap (InfoboardWidgets) marks the WS as ready, but the broadcast
# pushes (TEMPIST<N>, PACKETBOX<N>, REGEN, ...) and the chart-stand
# replies arrive over the following ~1-3 seconds. Holding setup briefly
# lets the platforms enumerate everything in one pass.
SETUP_OBSERVATION_WINDOW: Final = 5.0

# Refresh interval for consumption charts (electricity, water). The
# server doesn't push chart values, so we re-issue
# ``GiveMeChartStandsManuell<id>`` every interval.
CHART_POLL_INTERVAL: Final = 60.0
