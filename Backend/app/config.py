# app/config.py
"""
Central application configuration.

Values are sourced from environment variables (.env, loaded once at startup in
main.py). Keep all tunable plant/deployment constants here so they can be
changed per-plant without touching business logic.
"""

import os

# ── Plant installed capacity ─────────────────────────────────────────────────
# Nameplate capacity in MW, used for every Performance Ratio calculation.
# Configurable via the INSTALLED_CAPACITY_MW environment variable (or .env) so
# it can be changed per plant WITHOUT editing code.
#
# NOTE: For PR, this should be the capacity the energy is referenced against
# (typically DC nameplate, kWp). See PR validation notes in the dashboard.
INSTALLED_CAPACITY_MW_DEFAULT = 130.0


def get_installed_capacity_mw() -> float:
    """Installed capacity in MW from INSTALLED_CAPACITY_MW (falls back to default)."""
    try:
        val = float(os.getenv("INSTALLED_CAPACITY_MW", ""))
        return val if val > 0 else INSTALLED_CAPACITY_MW_DEFAULT
    except (TypeError, ValueError):
        return INSTALLED_CAPACITY_MW_DEFAULT


def get_installed_capacity_kw() -> float:
    """Installed capacity in kW (derived from the MW configuration)."""
    return get_installed_capacity_mw() * 1000.0
