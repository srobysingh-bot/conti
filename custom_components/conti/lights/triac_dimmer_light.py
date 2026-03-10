"""Triac dimmer light (power + brightness, DP 20-range).

Triac / leading-edge dimmers typically use DPs in the 20–29 range
(e.g. DP 20 = power, DP 22 = brightness).  They behave identically to
a standard dimmer for HA purposes but may apply brightness changes more
slowly, so the stale-protect window is slightly longer.
"""

from __future__ import annotations

from .base_light import STALE_PROTECT_SECONDS  # noqa: F401 (re-export)
from .dimmer_light import ContiDimmerLight

# Triac dimmers are electrically slower to settle; a wider
# stale-protect window prevents bounce-back after slider adjustments.
_TRIAC_STALE_PROTECT_SECONDS: float = 8.0


class ContiTriacDimmerLight(ContiDimmerLight):
    """Triac / leading-edge dimmer (DP 20-range)."""

    def _is_stale(self, dp_id: str, incoming: object) -> bool:
        """Use a wider stale-protect window for triac devices."""
        import time

        if (
            dp_id in self._last_sent_dps
            and (time.monotonic() - self._last_sent_ts)
            < _TRIAC_STALE_PROTECT_SECONDS
            and incoming != self._last_sent_dps[dp_id]
        ):
            return True
        return False
