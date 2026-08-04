"""Shared helpers for the Freeze Guard test suite.

The entry points are scripts, not an installed package: run.py and
run-monitor.py are loaded by path, exactly the way run.py loads the monitor at
runtime. That keeps the tests honest - they exercise the shipped files, not a
parallel copy.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI_PATH = os.path.join(ROOT, "run.py")
MONITOR_PATH = os.path.join(ROOT, "run-monitor.py")
CLI_PATH = os.path.join(ROOT, "freeze-guard")
DAEMON_PATH = os.path.join(ROOT, "fg-daemon.sh")
INSTALL_PATH = os.path.join(ROOT, "install.sh")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load(name, path):
    sys.dont_write_bytecode = True
    loader = importlib.machinery.SourceFileLoader(name, path)
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


_cache = {}


def load_monitor():
    """Imports run-monitor.py. No display needed."""
    if "monitor" not in _cache:
        _cache["monitor"] = _load("fg_monitor_under_test", MONITOR_PATH)
    return _cache["monitor"]


def load_gui():
    """Imports run.py. Requires PyGObject; raises otherwise."""
    if "gui" not in _cache:
        _cache["gui"] = _load("fg_gui_under_test", GUI_PATH)
    return _cache["gui"]


# --- environment probes -----------------------------------------------------
def has_psi():
    return os.path.exists("/proc/pressure/memory")


def has_procfs():
    return os.path.isdir("/proc")


def has_gtk():
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401
        return True
    except Exception:
        return False


def has_display():
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def gui_testable():
    """GUI tests need the bindings, a display and the monitor next to run.py."""
    return has_gtk() and has_display() and has_psi()


def pump(iterations=200):
    """Drains the GTK event queue so layout and drawing actually happen."""
    from gi.repository import Gtk

    count = 0
    while Gtk.events_pending() and count < iterations:
        Gtk.main_iteration_do(False)
        count += 1
    return count


# --- data builders ----------------------------------------------------------
CSV_HEADER = ("t,level,mem_avail_pct,swap_used_pct,mem_some10,mem_full10,mem_full60,"
              "io_full10,swapin_kbs,swapout_kbs,majflt_s,cpu_us,cpu_sy,cpu_wa,cpu_si,"
              "load1,kmsg_total,top")


def csv_row(ts, level="OK", avail=40.0, full10=0.0, swapout=0,
            top="MEM code:1000M | CPU code:5%"):
    """One recorder row in the exact column order run-monitor.py writes."""
    return (f"{ts:.0f},{level},{avail:.1f},10.0,0.0,{full10:.1f},0.0,0.0,0,"
            f"{swapout:.0f},0,5,1,0,0,1.00,0,\"{top}\"")


def write_history(path, *, samples=40, interval=5, gap_after=None, gap_seconds=600,
                  alarm_from=None, end=None):
    """Writes a synthetic recording.

    Args:
        path: Target file.
        samples: How many rows before/after the gap in total.
        interval: Seconds between rows.
        gap_after: Insert an outage after this row index (None = no outage).
        gap_seconds: Length of that outage.
        alarm_from: Rows from this index carry a level above OK.
        end: Timestamp of the last row (default: now).

    Returns:
        tuple: (first_ts, last_ts, gap_start, gap_end) - gap values None if absent.
    """
    end = end if end is not None else time.time()
    total_span = samples * interval + (gap_seconds if gap_after is not None else 0)
    ts = end - total_span
    lines, gap = [CSV_HEADER], (None, None)
    first = ts
    for i in range(samples):
        alarming = alarm_from is not None and i >= alarm_from
        lines.append(csv_row(
            ts,
            level="KRITISCH" if alarming else "OK",
            avail=7.5 if alarming else 40.0,
            full10=55.0 if alarming else 0.0,
            swapout=38000 if alarming else 0,
        ))
        if gap_after is not None and i == gap_after:
            gap = (ts, ts + gap_seconds)
            ts += gap_seconds
        ts += interval
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return first, ts - interval, gap[0], gap[1]


def real_sample(monitor=None, want_procs=True):
    """A genuine sample from the shipped Sampler.

    Building fixtures from the real thing is deliberate: a hand-written dict
    would drift from the sampler's keys and the drift would go unnoticed.
    """
    monitor = monitor or load_monitor()
    sampler = monitor.Sampler()
    sampler.sample(want_procs=want_procs)
    time.sleep(0.05)
    return sampler.sample(want_procs=want_procs)


def scenario_sample(monitor, level, base=None):
    """Turns a real sample into a named scenario.

    Args:
        monitor: The monitor module.
        level: 'green', 'yellow', 'orange' or 'red'.
        base: Sample to start from (default: a fresh real one).

    Returns:
        dict: Sample whose alert level is the requested one.
    """
    s = dict(base or real_sample(monitor))
    presets = {
        "green": dict(mem_avail_pct=55.0, mem_some10=0.0, mem_full10=0.0,
                      mem_full60=0.0, swapout_kbs=0.0, majflt_s=0.0, kmsg_new=0,
                      runaways=[]),
        "yellow": dict(mem_avail_pct=20.0, mem_some10=0.0, mem_full10=0.0,
                       mem_full60=0.0, swapout_kbs=0.0, majflt_s=0.0, kmsg_new=0,
                       runaways=[]),
        "orange": dict(mem_avail_pct=20.0, mem_some10=0.0, mem_full10=8.0,
                       mem_full60=0.0, swapout_kbs=0.0, majflt_s=0.0, kmsg_new=0,
                       runaways=[]),
        "red": dict(mem_avail_pct=9.0, mem_some10=40.0, mem_full10=45.0,
                    mem_full60=35.0, swapout_kbs=40000.0, majflt_s=900.0,
                    kmsg_new=0, runaways=[]),
    }
    s.update(presets[level])
    s["level"], s["reason"], s["reason_i18n"] = monitor.alert_level(s)
    return s


# --- stubs for the dashboard -----------------------------------------------
class StubKmsg:
    """Stands in for KmsgWatcher: no /dev/kmsg, no counting."""

    def __init__(self, fd=1):
        self.fd = fd
        self.count = 0

    def poll(self):
        return 0


class StubSampler:
    def __init__(self):
        self.kmsg = StubKmsg()

    def sample(self, want_procs=True):
        raise AssertionError("the stub poller never samples on its own")


class StubPoller:
    """Replaces the sampling thread: the test decides when a sample arrives."""

    def __init__(self, on_sample, interval=5.0):
        self.on_sample = on_sample
        self.interval = interval
        self.paused = False
        self.started = False
        self.stopped = False
        self.sampler = StubSampler()

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def set_paused(self, paused):
        self.paused = paused

    def feed(self, sample):
        """Delivers a sample the way GLib.idle_add would."""
        self.on_sample(sample)


class StubTray:
    """Replaces the real status icon: no panel icon, no D-Bus name to fight over."""

    def __init__(self, on_show=None, on_console=None, on_settings=None, on_quit=None):
        self.on_show = on_show
        self.on_console = on_console
        self.on_settings = on_settings
        self.on_quit = on_quit
        self.available = True
        self.updates = []
        self.rebuilds = 0

    def update(self, level_text, status, label):
        self.updates.append((level_text, status, label))

    def rebuild(self):
        self.rebuilds += 1


def build_window(gui, tmp_config_dir, *, page="live", tray=True, settings_overrides=None):
    """Builds a Window with stubbed poller and tray, ready to be fed samples.

    Args:
        gui: The loaded run.py module.
        tmp_config_dir: Directory that stands in for ~/.config/freeze-guard.
        page: Page to start on.
        tray: Whether the stub tray reports itself as available.
        settings_overrides: Values applied on top of the defaults.

    Returns:
        tuple: (window, poller_stub, tray_stub)
    """
    gui.CONFIG_DIR = tmp_config_dir
    gui.CONFIG_FILE = os.path.join(tmp_config_dir, "settings.json")
    gui.AUTOSTART_FILE = os.path.join(tmp_config_dir, "autostart.desktop")

    settings = gui.Settings()
    settings.data.update(settings_overrides or {})

    real_poller, real_tray = gui.Poller, gui.Tray
    created = {}

    def poller_factory(on_sample, interval=5.0):
        created["poller"] = StubPoller(on_sample, interval)
        return created["poller"]

    def tray_factory(*args, **kwargs):
        stub = StubTray(*args, **kwargs)
        stub.available = tray
        created["tray"] = stub
        return stub

    gui.Poller, gui.Tray = poller_factory, tray_factory
    try:
        window = gui.Window(settings, page=page)
    finally:
        gui.Poller, gui.Tray = real_poller, real_tray
    return window, created["poller"], created["tray"]


# --- subprocess helpers -----------------------------------------------------
def run_cli(*args, timeout=60, env=None):
    """Runs the freeze-guard CLI and returns the CompletedProcess."""
    return subprocess.run([CLI_PATH, *args], capture_output=True, text=True,
                          timeout=timeout, env=env)


def run_monitor_cli(*args, timeout=60):
    """Runs run-monitor.py and returns the CompletedProcess."""
    return subprocess.run([sys.executable, MONITOR_PATH, *args],
                          capture_output=True, text=True, timeout=timeout)
