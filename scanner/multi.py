"""Multi-dongle band partition and device assignment (#5)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Sequence

from scanner.config import Band
from scanner.sdr import DetectedDevice, list_rtlsdr_devices

log = logging.getLogger(__name__)

# Suggested split when two radios have no explicit groups
DEFAULT_PRIMARY_GROUPS = (
    "ham_2m",
    "ham_70cm",
    "gmrs",
    "murs",
    "marine",
    "ham_1p25m",
)
DEFAULT_SECONDARY_GROUPS = (
    "atc",
    "ham_10m",
    "ham_6m",
    "ham_33cm",
    "ham_23cm",
    "other",
)


@dataclass
class RadioConfig:
    """One physical RTL-SDR in the multi-dongle plan."""

    label: str = "R0"
    serial: str | None = None
    device_index: int | None = None
    gain: float | None = None  # None → use global device.gain
    ppm_error: int | None = None  # None → use global device.ppm_error
    # Band groups this radio hops. None = catch-all for unassigned groups.
    groups: list[str] | None = None


def parse_radios(device: dict, default_gain: float, default_ppm: int) -> list[RadioConfig]:
    """
    Build radio list from config device section.

    Legacy single-dongle::
        device:
          serial: null
          gain: 40.2

    Multi-dongle::
        device:
          gain: 40.2
          radios:
            - label: voice
              serial: null          # auto-pick free dongle
              groups: [ham_2m, ham_70cm, gmrs, murs, marine]
            - label: atc
              serial: "00000002"    # set after rtl_eeprom / --list-devices
              groups: [atc, ham_10m, ham_6m]
    """
    raw_list = device.get("radios")
    if not raw_list:
        return [
            RadioConfig(
                label="R0",
                serial=device.get("serial"),
                device_index=device.get("device_index"),
                gain=float(device.get("gain", default_gain)),
                ppm_error=int(device.get("ppm_error", default_ppm)),
                groups=None,
            )
        ]

    radios: list[RadioConfig] = []
    for i, item in enumerate(raw_list):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or f"R{i}")
        groups = item.get("groups")
        if groups is not None:
            groups = [str(g).lower() for g in groups]
        g = item.get("gain")
        p = item.get("ppm_error")
        radios.append(
            RadioConfig(
                label=label,
                serial=item.get("serial"),
                device_index=item.get("device_index"),
                gain=float(g) if g is not None else None,
                ppm_error=int(p) if p is not None else None,
                groups=groups,
            )
        )
    if not radios:
        return parse_radios({**device, "radios": None}, default_gain, default_ppm)
    return radios


def apply_default_group_split(radios: list[RadioConfig]) -> list[RadioConfig]:
    """If 2+ radios all have groups=None, apply a sensible VHF/UHF+ATC split."""
    if len(radios) < 2:
        return radios
    if any(r.groups is not None for r in radios):
        return radios
    out: list[RadioConfig] = []
    for i, r in enumerate(radios):
        if i == 0:
            out.append(replace(r, groups=list(DEFAULT_PRIMARY_GROUPS)))
        elif i == 1:
            out.append(replace(r, groups=list(DEFAULT_SECONDARY_GROUPS)))
        else:
            # Extra radios: round-robin leftover / share secondary
            out.append(replace(r, groups=list(DEFAULT_SECONDARY_GROUPS)))
    log.info(
        "Multi-dongle: auto group split → %s",
        ", ".join(f"{r.label}:{r.groups}" for r in out),
    )
    return out


def resolve_devices(radios: list[RadioConfig]) -> list[RadioConfig]:
    """
    Bind radios without serial/index to free USB devices.
    Raises if not enough dongles are present.
    """
    try:
        present = list_rtlsdr_devices()
    except SystemExit:
        raise
    except Exception as exc:
        log.warning("Could not enumerate RTL-SDRs: %s", exc)
        present = []

    if present:
        log.info(
            "RTL-SDR devices found (%d): %s",
            len(present),
            "; ".join(d.display() for d in present),
        )
    else:
        log.warning("No RTL-SDR devices enumerated (will try open anyway)")

    claimed_serials = {
        str(r.serial).strip()
        for r in radios
        if r.serial is not None and str(r.serial).strip() != ""
    }
    claimed_idx = {int(r.device_index) for r in radios if r.device_index is not None}

    free: list[DetectedDevice] = []
    for d in present:
        if d.serial and d.serial in claimed_serials:
            continue
        if d.index in claimed_idx:
            continue
        free.append(d)

    free_i = 0
    resolved: list[RadioConfig] = []
    for r in radios:
        if r.serial is not None and str(r.serial).strip() != "":
            resolved.append(r)
            continue
        if r.device_index is not None:
            resolved.append(r)
            continue
        if free_i < len(free):
            d = free[free_i]
            free_i += 1
            resolved.append(
                replace(
                    r,
                    serial=d.serial or None,
                    device_index=d.index if not d.serial else r.device_index,
                )
            )
            log.info("[%s] auto-assigned %s", r.label, d.display())
        elif len(radios) == 1:
            # Single radio, no enumeration — open default device
            resolved.append(r)
        else:
            raise RuntimeError(
                f"Not enough RTL-SDR dongles for radio '{r.label}' "
                f"(need {len(radios)}, found {len(present)}). "
                "Plug in another stick or remove a radios: entry. "
                "List devices: python -m scanner --list-devices"
            )
    return resolved


def partition_bands(
    bands: Sequence[Band],
    radios: Sequence[RadioConfig],
) -> dict[str, list[Band]]:
    """Assign each band window to exactly one radio by group."""
    assignment: dict[str, list[Band]] = {r.label: [] for r in radios}
    claimed: set[str] = set()

    for r in radios:
        if not r.groups:
            continue
        gset = {g.lower() for g in r.groups}
        for b in bands:
            if b.name in claimed:
                continue
            if b.group.lower() in gset:
                assignment[r.label].append(b)
                claimed.add(b.name)

    unassigned = [b for b in bands if b.name not in claimed]
    catch = [r for r in radios if r.groups is None]
    if not catch:
        catch = list(radios)
    for i, b in enumerate(unassigned):
        r = catch[i % len(catch)]
        assignment[r.label].append(b)
        claimed.add(b.name)

    for r in radios:
        n = len(assignment[r.label])
        groups = sorted({b.group for b in assignment[r.label]})
        log.info(
            "[%s] plan: %d windows · groups %s",
            r.label,
            n,
            ",".join(groups) if groups else "(none)",
        )
    return assignment
