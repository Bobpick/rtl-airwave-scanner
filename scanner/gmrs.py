"""Complete US GMRS / FRS channel tables (MHz → Hz)."""

from __future__ import annotations

from scanner.config import KnownChannel

# Official US GMRS/FRS center frequencies (Hz)
# Channels 1–7: GMRS/FRS 462 MHz interstitial
# 8–14: FRS only 467 MHz interstitial  
# 15–22: GMRS repeater/simplex 462 MHz
# Plus repeater inputs 467.550–467.725 (GMRS)

GMRS_FRS_CHANNELS: list[tuple[str, float, str]] = [
    # ch, freq MHz, notes
    ("GMRS/FRS 1", 462.5625, "462.5625"),
    ("GMRS/FRS 2", 462.5875, "462.5875"),
    ("GMRS/FRS 3", 462.6125, "462.6125"),
    ("GMRS/FRS 4", 462.6375, "462.6375"),
    ("GMRS/FRS 5", 462.6625, "462.6625"),
    ("GMRS/FRS 6", 462.6875, "462.6875"),
    ("GMRS/FRS 7", 462.7125, "462.7125"),
    ("FRS 8", 467.5625, "467.5625 FRS"),
    ("FRS 9", 467.5875, "467.5875 FRS"),
    ("FRS 10", 467.6125, "467.6125 FRS"),
    ("FRS 11", 467.6375, "467.6375 FRS"),
    ("FRS 12", 467.6625, "467.6625 FRS"),
    ("FRS 13", 467.6875, "467.6875 FRS"),
    ("FRS 14", 467.7125, "467.7125 FRS"),
    ("GMRS 15", 462.5500, "462.550 simplex/repeater out"),
    ("GMRS 16", 462.5750, "462.575"),
    ("GMRS 17", 462.6000, "462.600"),
    ("GMRS 18", 462.6250, "462.625"),
    ("GMRS 19", 462.6500, "462.650"),
    ("GMRS 20", 462.6750, "462.675"),
    ("GMRS 21", 462.7000, "462.700"),
    ("GMRS 22", 462.7250, "462.725"),
    # Repeater inputs (GMRS 15–22 inputs, +5 MHz)
    ("GMRS 15 in", 467.5500, "467.550 repeater input"),
    ("GMRS 16 in", 467.5750, "467.575 repeater input"),
    ("GMRS 17 in", 467.6000, "467.600 repeater input"),
    ("GMRS 18 in", 467.6250, "467.625 repeater input"),
    ("GMRS 19 in", 467.6500, "467.650 repeater input"),
    ("GMRS 20 in", 467.6750, "467.675 repeater input"),
    ("GMRS 21 in", 467.7000, "467.700 repeater input"),
    ("GMRS 22 in", 467.7250, "467.725 repeater input"),
]


def gmrs_known_channels(match_hz: float = 6000.0) -> list[KnownChannel]:
    return [
        KnownChannel(
            name=name,
            frequency_hz=mhz * 1e6,
            match_hz=match_hz,
            kind="voice",
            notes=notes,
        )
        for name, mhz, notes in GMRS_FRS_CHANNELS
    ]
