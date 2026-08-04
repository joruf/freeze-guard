#!/usr/bin/env python3
"""Platform capability matrix for Freeze Guard.

What actually works depends on the machine, not on the wish list: PSI needs a
kernel >= 4.20 with CONFIG_PSI, the units need systemd, the tray needs a panel
that speaks one of three protocols, and killing someone else's process needs
polkit. This module states those dependencies once, in code, so that

  * `freeze-guard doctor` can report the running system honestly, and
  * tests can assert the documented matrix without switching OS.

Nothing here probes destructively; every check is a read or a `which`.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from enum import Enum


class SupportLevel(Enum):
    """How well a feature works in a given context."""

    FULL = "full"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class FeatureCapability:
    """One feature's verdict in one context."""

    name: str
    level: SupportLevel
    detail: str


@dataclass(frozen=True)
class PlatformContext:
    """A machine profile - either probed or hand-written for the matrix."""

    name: str
    os_family: str = "linux"          # linux | windows | darwin
    has_procfs: bool = True
    has_psi: bool = True             # /proc/pressure/memory
    has_systemd: bool = True
    cgroup_version: int = 2
    session: str = "x11"             # x11 | wayland | none
    kmsg_readable: bool = True       # /dev/kmsg
    has_zram: bool = True            # zram module or generator present
    has_earlyoom: bool = True
    has_gtk: bool = True
    tray_backend: str = "xapp"       # xapp | appindicator | statusicon | none
    has_pkexec: bool = True
    notes: tuple = field(default_factory=tuple)


# --- the documented profiles ------------------------------------------------
LINUX_FULL = PlatformContext(
    name="linux_full",
    notes=("Mint/Cinnamon, systemd, cgroup v2, X11, XApp tray - the reference",),
)
LINUX_WAYLAND = PlatformContext(
    name="linux_wayland",
    session="wayland",
    tray_backend="appindicator",
    notes=("GNOME/KDE on Wayland: the dashboard is Wayland-agnostic, it only "
           "reads /proc and draws",),
)
LINUX_NO_PSI = PlatformContext(
    name="linux_no_psi",
    has_psi=False,
    notes=("kernel < 4.20 or CONFIG_PSI=n: the entire early-warning idea is gone",),
)
LINUX_NO_SYSTEMD = PlatformContext(
    name="linux_no_systemd",
    has_systemd=False,
    has_earlyoom=False,
    tray_backend="statusicon",
    notes=("runit/openrc: the scripts still run, the units and the switches do not",),
)
LINUX_CGROUP_V1 = PlatformContext(
    name="linux_cgroup_v1",
    cgroup_version=1,
    notes=("service state is read from the cgroup v2 layout, so it degrades",),
)
LINUX_CONTAINER = PlatformContext(
    name="linux_container",
    has_systemd=False,
    kmsg_readable=False,
    has_zram=False,
    has_earlyoom=False,
    session="none",
    has_gtk=False,
    tray_backend="none",
    has_pkexec=False,
    notes=("docker/CI: fine for the recorder and the tests, nothing to protect",),
)
LINUX_HEADLESS = PlatformContext(
    name="linux_headless",
    session="none",
    has_gtk=False,
    tray_backend="none",
    notes=("server or ssh: console tools only",),
)
WINDOWS = PlatformContext(
    name="windows",
    os_family="windows",
    has_procfs=False,
    has_psi=False,
    has_systemd=False,
    kmsg_readable=False,
    has_zram=False,
    has_earlyoom=False,
    tray_backend="none",
    has_pkexec=False,
    notes=("no procfs, no PSI, no cgroups - out of scope by construction",),
)
DARWIN = PlatformContext(
    name="darwin",
    os_family="darwin",
    has_procfs=False,
    has_psi=False,
    has_systemd=False,
    kmsg_readable=False,
    has_zram=False,
    has_earlyoom=False,
    tray_backend="none",
    has_pkexec=False,
    notes=("macOS manages memory pressure itself and exposes none of this",),
)

KNOWN_PROFILES = {
    p.name: p for p in (
        LINUX_FULL, LINUX_WAYLAND, LINUX_NO_PSI, LINUX_NO_SYSTEMD,
        LINUX_CGROUP_V1, LINUX_CONTAINER, LINUX_HEADLESS, WINDOWS, DARWIN,
    )
}

# Features in the order a user meets them.
FEATURES = (
    "monitor", "recorder", "guard_kill", "zram", "earlyoom", "kernel_stalls",
    "gui", "tray", "auto_raise", "process_kill", "service_switch",
)

CORE_FEATURES = ("monitor", "recorder", "guard_kill")


def evaluate_capabilities(ctx: PlatformContext) -> dict:
    """Verdict per feature for one context.

    Args:
        ctx: The machine profile to judge.

    Returns:
        dict: Feature name -> FeatureCapability.
    """
    out = {}

    def add(name, level, detail):
        out[name] = FeatureCapability(name=name, level=level, detail=detail)

    FULL, PART, NONE = SupportLevel.FULL, SupportLevel.PARTIAL, SupportLevel.UNSUPPORTED

    if not ctx.has_procfs:
        for feature in FEATURES:
            add(feature, NONE, "requires /proc (Linux)")
        return out

    # The numbers themselves
    if ctx.has_psi:
        add("monitor", FULL, "PSI, /proc/meminfo, /proc/vmstat")
        add("recorder", FULL, "CSV every 5 s, fsync above OK")
        add("guard_kill", FULL, "kills on PSI full60 >= 20 % and < 15 % available")
    else:
        add("monitor", NONE, "/proc/pressure/memory missing - kernel < 4.20 or CONFIG_PSI=n")
        add("recorder", NONE, "same PSI requirement as the dashboard")
        add("guard_kill", NONE, "the kill condition is defined on PSI")

    # Layer 0/1 come from packages
    add("zram", FULL if ctx.has_zram else NONE,
        "systemd-zram-generator" if ctx.has_zram else "zram generator/module missing")
    add("earlyoom", FULL if ctx.has_earlyoom else NONE,
        "earlyoom present" if ctx.has_earlyoom else "package earlyoom not installed")

    add("kernel_stalls", FULL if ctx.kmsg_readable else PART,
        "/dev/kmsg readable" if ctx.kmsg_readable
        else "/dev/kmsg unreadable - the stall counter stays 0")

    # The window
    if ctx.has_gtk and ctx.session in ("x11", "wayland"):
        add("gui", FULL, f"GTK3 on {ctx.session}")
    elif not ctx.has_gtk:
        add("gui", NONE, "python3-gi / GTK3 missing - console tools only")
    else:
        add("gui", NONE, "no display session - console tools only")

    tray_detail = {
        "xapp": ("XApp: icon plus text label", FULL),
        "appindicator": ("AppIndicator: icon, label may be ignored", FULL),
        "statusicon": ("Gtk.StatusIcon fallback, deprecated", PART),
        "none": ("no tray: the window always starts visible", NONE),
    }[ctx.tray_backend]
    if out["gui"].level is NONE:
        add("tray", NONE, "no GUI, so no tray")
        add("auto_raise", NONE, "no GUI to raise")
    else:
        add("tray", tray_detail[1], tray_detail[0])
        add("auto_raise", FULL if ctx.tray_backend != "none" else PART,
            "raises itself once per incident" if ctx.tray_backend != "none"
            else "window is visible anyway")

    add("process_kill", FULL if ctx.has_pkexec else PART,
        "own processes directly, others via pkexec" if ctx.has_pkexec
        else "own processes only - pkexec missing")

    if not ctx.has_systemd:
        add("service_switch", NONE, "no systemd - nothing to enable or disable")
    elif not ctx.has_pkexec:
        add("service_switch", PART, "needs a terminal: sudo systemctl enable --now …")
    elif ctx.cgroup_version < 2:
        add("service_switch", PART,
            "switching works, the state display reads the cgroup v2 layout")
    else:
        add("service_switch", FULL, "systemctl enable/disable --now via pkexec")

    return out


# --- probing the running machine -------------------------------------------
def current_os_family() -> str:
    """Returns 'linux', 'windows', 'darwin' or 'unknown'."""
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "unknown"


def _gtk_available() -> bool:
    try:
        import gi  # noqa: PLC0415 - probing on purpose

        gi.require_version("Gtk", "3.0")
        return True
    except Exception:
        return False


def _tray_backend() -> str:
    try:
        import gi  # noqa: PLC0415
    except Exception:
        return "none"
    for module, version, name in (("XApp", "1.0", "xapp"),
                                  ("AyatanaAppIndicator3", "0.1", "appindicator"),
                                  ("AppIndicator3", "0.1", "appindicator")):
        try:
            gi.require_version(module, version)
            return name
        except Exception:
            continue
    return "statusicon" if _gtk_available() else "none"


def _zram_available() -> bool:
    if os.path.exists("/sys/class/zram-control") or os.path.exists("/dev/zram0"):
        return True
    return any(os.path.exists(p) for p in (
        "/usr/lib/systemd/system/systemd-zram-setup@.service",
        "/lib/systemd/system/systemd-zram-setup@.service",
        "/usr/lib/systemd/system-generators/zram-generator"))


def current_context() -> PlatformContext:
    """Probes the running machine. Read-only."""
    family = current_os_family()
    session = ("wayland" if os.environ.get("WAYLAND_DISPLAY")
               else "x11" if os.environ.get("DISPLAY") else "none")
    return PlatformContext(
        name="current",
        os_family=family,
        has_procfs=os.path.isdir("/proc"),
        has_psi=os.path.exists("/proc/pressure/memory"),
        has_systemd=os.path.isdir("/run/systemd/system"),
        cgroup_version=2 if os.path.exists("/sys/fs/cgroup/cgroup.controllers") else 1,
        session=session,
        kmsg_readable=os.access("/dev/kmsg", os.R_OK),
        has_zram=_zram_available(),
        has_earlyoom=bool(shutil.which("earlyoom")),
        has_gtk=_gtk_available(),
        tray_backend=_tray_backend(),
        has_pkexec=bool(shutil.which("pkexec")),
    )


MARK = {SupportLevel.FULL: "+", SupportLevel.PARTIAL: "~", SupportLevel.UNSUPPORTED: "-"}


def report(ctx: PlatformContext | None = None) -> str:
    """Human-readable capability report for one context."""
    ctx = ctx or current_context()
    caps = evaluate_capabilities(ctx)
    lines = [
        f"Freeze Guard - platform report ({ctx.name})",
        f"  os={ctx.os_family} session={ctx.session} systemd={ctx.has_systemd} "
        f"cgroup=v{ctx.cgroup_version} psi={ctx.has_psi} kmsg={ctx.kmsg_readable} "
        f"tray={ctx.tray_backend} pkexec={ctx.has_pkexec}",
        "",
    ]
    width = max(len(f) for f in FEATURES)
    for feature in FEATURES:
        cap = caps[feature]
        lines.append(f"  [{MARK[cap.level]}] {feature:<{width}}  {cap.detail}")
    missing = [f for f in CORE_FEATURES if caps[f].level is SupportLevel.UNSUPPORTED]
    lines.append("")
    lines.append("  core features unavailable: " + ", ".join(missing) if missing
                 else "  all core features available")
    for note in ctx.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def main() -> int:
    """Prints the report for this machine. Exit 1 if a core feature is missing."""
    ctx = current_context()
    print(report(ctx))
    caps = evaluate_capabilities(ctx)
    return 1 if any(caps[f].level is SupportLevel.UNSUPPORTED
                    for f in CORE_FEATURES) else 0


if __name__ == "__main__":
    sys.exit(main())
