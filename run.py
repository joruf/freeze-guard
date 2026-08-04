#!/usr/bin/env python3
"""run.py - graphical dashboard for the freeze protection.

Same numbers as run-monitor.py, in a window. The measurement is not
reimplemented here: run-monitor.py is imported and its Sampler is used, so
console and window can never drift apart and claim different values during an
incident. Installed as the command `fg-gui`.

  freeze-guard ui        this window
  freeze-guard monitor   the same data in a terminal
  freeze-guard           asks which of the two

Sampling happens in a background thread (only /proc reads, no subprocess per
tick); the GTK main loop paints and nothing else.

Pages: Live, History (forensics over the recording), Log, Simulation, Settings.

The window can live in the system tray and pull itself to the front when memory
pressure rises - which is the whole point: an early warning is worthless if
nobody happens to be looking at the moment it appears.
"""

import argparse
import importlib.machinery
import importlib.util
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gio, GLib, Gtk, Pango
except (ImportError, ValueError) as exc:  # pragma: no cover - environment issue
    sys.exit(
        f"GTK3 is not available ({exc}).\n"
        "  install : sudo apt install python3-gi gir1.2-gtk-3.0\n"
        "  console : freeze-guard monitor"
    )

# The tray is optional and comes in three flavours. XApp first: on Cinnamon it
# is the native protocol and the only one of the three that actually shows the
# text label next to the icon (Ayatana's set_label is ignored there). Then
# AppIndicator for GNOME/KDE/Unity, then plain Gtk.StatusIcon. If none of them
# works, Tray.available stays False and the window never starts hidden.
try:
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp
except (ImportError, ValueError):
    XApp = None
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ImportError, ValueError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator
    except (ImportError, ValueError):
        AppIndicator = None


# ---------------------------------------------------------------------------
# shared measurement code
# ---------------------------------------------------------------------------
def set_process_name(name):
    """Make comm show the tool instead of "python3".

    The guard whitelists processes by /proc/PID/comm, and for a Python script
    that is the interpreter name - too broad to whitelist (it would protect
    every Python process) and useless when reading `ps`. prctl(PR_SET_NAME)
    fixes both. comm is capped at 15 bytes by the kernel.
    """
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        buf = ctypes.create_string_buffer(name.encode()[:15])
        libc.prctl(15, ctypes.byref(buf), 0, 0, 0)  # 15 = PR_SET_NAME
    except Exception:  # pragma: no cover - cosmetic only, never fatal
        pass


def load_monitor():
    """Import the monitor as a module.

    Two names, because the file is called run-monitor.py in the repository but is
    installed as /usr/local/bin/fg-monitor (no suffix, like every other command).
    Bytecode writing stays off so neither directory collects __pycache__.
    """
    here = os.path.dirname(os.path.realpath(__file__))
    for cand in (os.path.join(here, "run-monitor.py"),
                 os.path.join(here, "fg-monitor"),
                 "/usr/local/bin/fg-monitor"):
        if os.path.isfile(cand):
            sys.dont_write_bytecode = True
            loader = importlib.machinery.SourceFileLoader("fg_monitor", cand)
            spec = importlib.util.spec_from_loader(loader.name, loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            return mod
    sys.exit("run-monitor.py not found (expected next to run.py, or as\n"
             "/usr/local/bin/fg-monitor).")


M = load_monitor()

# The single-instance guard lives next to run.py; import it by path so the file
# works from the repository and from /usr/local/bin alike.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fg_instance  # noqa: E402  (needs the path above)

GREEN, YELLOW, ORANGE, RED = M.GREEN, M.YELLOW, M.ORANGE, M.RED
LEVEL_KEY = {GREEN: "level.ok", YELLOW: "level.raised",
             ORANGE: "level.warning", RED: "level.critical"}
LEVEL_ICON = {GREEN: "✓", YELLOW: "!", ORANGE: "▲", RED: "✕"}
LEVEL_STATUS = {GREEN: "good", YELLOW: "warning", ORANGE: "serious", RED: "critical"}
# The recorder writes these ASCII names into the CSV - they are data, not UI.
CSV_LEVEL_KEY = {"OK": "level.ok", "ERHOEHT": "level.raised",
                 "WARNUNG": "level.warning", "KRITISCH": "level.critical"}

HISTORY_FILE = M.HISTORY_FILE
APP_DIR = os.path.dirname(os.path.realpath(__file__))
INSTALL_SH = os.path.join(APP_DIR, "install.sh")
INSTALL_CMD = f"sudo {INSTALL_SH}"
CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
CONFIG_DIR = os.path.join(CONFIG_HOME, "freeze-guard")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")
AUTOSTART_FILE = os.path.join(CONFIG_HOME, "autostart", "freeze-guard.desktop")

UNITS = (
    ("earlyoom", "earlyoom"),
    ("freeze-guard", "svc.guard"),
    ("freeze-guard-recorder", "svc.recorder"),
)
UNIT_DIRS = ("/etc/systemd/system", "/run/systemd/system",
             "/usr/lib/systemd/system", "/lib/systemd/system")


def unit_installed(name):
    """systemd unit file present? Cheaper and more precise than `systemctl
    is-active`, which reports a missing unit as merely 'inactive'."""
    return any(os.path.exists(f"{d}/{name}.service") for d in UNIT_DIRS)


# ---------------------------------------------------------------------------
# language
# ---------------------------------------------------------------------------
STR = {
    "en": {
        "app.title": "Freeze Guard",
        "app.subtitle": "memory pressure and freeze early warning",
        "level.ok": "OK",
        "level.raised": "RAISED",
        "level.warning": "WARNING",
        "level.critical": "CRITICAL",
        "page.live": "Live",
        "page.history": "History",
        "page.log": "Log",
        "page.sim": "Simulation",
        "page.settings": "Settings",
        "hdr.pause": "Pause",
        "hdr.paused": "paused",
        "menu.console": "Open console dashboard in a terminal",
        "menu.settings": "Settings",
        "menu.tray": "Hide to system tray",
        "menu.language": "Language",
        "menu.copy_install": "Copy install command",
        "menu.quit": "Quit",
        "tray.hint_title": "Freeze Guard keeps watching",
        "tray.hint_body": ("The window is only hidden - the icon in the system tray "
                           "stays. Quit it through the tray menu, or switch off "
                           "\"Start in the system tray\" in the settings."),
        "menu.show": "Show window",
        "card.memory": "Memory",
        "card.state": "State",
        "card.psi": "Memory pressure — share of time tasks are blocked",
        "card.swapout": "Swapping out of RAM",
        "card.cpu": "CPU load — us apps · sy kernel · wa I/O wait · si softirq",
        "card.top_mem": "Largest memory users",
        "card.top_cpu": "Highest CPU load",
        "card.services": "Protection layers",
        "card.victims": "Victim selection — prediction only, nothing is killed here",
        "card.record": "Recording — red areas are outages",
        "card.outages": "Outages and alerts",
        "card.log": "Log",
        "card.no_record": "No recording",
        "mem.ram": "RAM  {used} of {total} GB in use   ·   {pct} % free",
        "mem.swap": "Swap  {used} of {total} GB in use   ·   {zram}",
        "mem.swap_none": "Swap  not active — no cushion before the OOM killer",
        "mem.zram": "zram {size} GB",
        "mem.no_zram": "without zram!",
        "mem.marker": "Marker: below 15 % free memory the guard steps in.",
        "mem.thresholds": ("The guard only kills once full60 ≥ 20 % AND less than 15 % "
                           "of memory is available — both for 60 s straight."),
        "tile.psi": "Pressure full60",
        "tile.psi_sub": "full10 {f10} %  ·  some10 {some10} %",
        "tile.psi_hint": "kill from 20 %",
        "tile.swapout": "Swapping out",
        "tile.swapout_sub": "in {rate}  ·  major faults {majflt}/s",
        "tile.swapout_hint": "out of RAM",
        "tile.stalls": "Kernel stalls",
        "tile.stalls_hint": "precursor of the freeze",
        "tile.stalls_new": "+{n} NEW!",
        "tile.stalls_denied": "no access to /dev/kmsg",
        "tile.load": "Load",
        "tile.load_sub": "io-full10 {io} %  ·  wa {wa} %",
        "tile.load_hint": "{cpus} CPUs",
        "col.process": "Process",
        "col.rss": "RSS",
        "col.cpu": "CPU",
        "col.pid": "PID",
        "col.points": "Points (MB)",
        "col.category": "Category",
        "col.kill": "End",
        "col.time": "Time",
        "col.level": "Level",
        "col.mem_free": "Memory free",
        "col.psi_full": "Pressure full10",
        "col.swapout": "Swap-out",
        "col.wa_sy": "wa/sy",
        "col.top": "Largest consumers",
        "kill.tip": "End this process: SIGTERM now, SIGKILL after 8 s",
        "kill.sent": "SIGTERM sent to PID {pid} ({comm})",
        "kill.killed": "PID {pid} ({comm}) ignored SIGTERM — sent SIGKILL",
        "kill.gone": "PID {pid} is already gone",
        "kill.denied": "PID {pid} belongs to another user — asking for the password",
        "kill.failed": "Could not end PID {pid}: {err}",
        "kill.runaway": "← sustained load",
        "svc.guard": "Guard",
        "svc.recorder": "Recording",
        "svc.active": "active",
        "svc.stopped": "stopped",
        "svc.missing": "not installed",
        "svc.working": "switching …",
        "svc.tip": ("On: systemctl enable --now — starts the service and enables it at "
                    "boot. Off: disable --now. Runs through pkexec, so the password is "
                    "asked by the desktop, never by this program."),
        "svc.missing_tip": "Unit file is missing — install first: {cmd}",
        "svc.enable_all": "Enable all",
        "svc.install": "Install protection …",
        "svc.installing": "installing — the desktop asks for the password, then it "
                          "takes a moment",
        "svc.install_done": "Protection installed and started",
        "svc.install_failed": "Installation failed: {err}",
        "svc.install_missing": "Installer not found: {path}",
        "svc.hint_none": ("Nothing is installed yet — that is why the switches are "
                          "off. The installer sets up zram, earlyoom, the guard and "
                          "the recording."),
        "svc.hint_some": "not installed: {units}",
        "svc.hint_missing": "Protection is not installed: {cmd}",
        "svc.failed": "{unit}: switching failed ({err})",
        "sim.auto": "Keep refreshing",
        "sim.target": "Target: PID {pid} · {comm} · {mb} MB",
        "sim.no_target": "No target above 200 MB — the guard would do nothing.",
        "sim.hint": ("Points = RSS × weight, highest wins; nothing under 200 MB is "
                     "killed because that would only postpone the stall. Preferred are "
                     "build and renderer processes that are safe to restart (×3), the "
                     "IDE counts only ×0.15, system services are exempt. Anything "
                     "holding ≥ 90 % of a core for 5 minutes counts as the culprit and "
                     "doubles."),
        "sim.tier_protected": "protected",
        "sim.tier_prefer": "preferred ×3",
        "sim.tier_ide": "IDE ×0.15",
        "sim.tier_normal": "normal ×1",
        "sim.tier_suspect": " · sustained load ×2",
        "hist.range": "Period",
        "hist.reload": "Reload",
        "hist.hours6": "6 hours",
        "hist.hours24": "24 hours",
        "hist.hours48": "48 hours",
        "hist.days7": "7 days",
        "hist.days30": "30 days",
        "hist.summary": "{rows} samples · {span} · {outages} · {alarms} samples above OK",
        "hist.outage_one": "1 outage",
        "hist.outage_many": "{n} outages",
        "hist.outage": "Outage {start}",
        "hist.outage_dead": "{min} min dead",
        "hist.outage_until": "until {end}",
        "hist.alarms": "Alerts above level OK",
        "hist.alarms_count": "{n} samples",
        "hist.alarms_last": "the last 20",
        "hist.calm": "No outage, no sample above OK",
        "hist.calm_sub": "memory pressure stayed harmless in the selected period",
        "hist.missing_title": "{path} does not exist.",
        "hist.missing_body": ("The black-box recording is not running. Without it the "
                              "next freeze leaves no trace — after a hard reset there is "
                              "no way to tell what the machine was doing in its last "
                              "minutes.\nSet up:  {cmd}\nCheck:  systemctl status "
                              "freeze-guard-recorder"),
        "hist.denied_title": "{path} is not readable.",
        "hist.denied_body": ("The file belongs to root. Either grant read access "
                             "(sudo chmod a+r {path}) or run the evaluation in a "
                             "terminal: sudo freeze-guard history"),
        "hist.empty_title": "No usable data in {path}.",
        "hist.empty_body": ("Reason: {err}. The recorder writes every 5 s — with a short "
                            "runtime this is normal."),
        "hist.copy": "Copy install command",
        "hist.legend_free": "Memory free",
        "hist.legend_psi": "Pressure full10",
        "log.refresh": "Refresh",
        "log.reading": "reading journal …",
        "log.state": "as of {time}",
        "log.none": "(no entries)",
        "log.unavailable": "(not available: {err})",
        "log.sec_guard": "freeze-guard",
        "log.sec_earlyoom": "earlyoom — interventions only",
        "log.sec_oom": "kernel OOM kills",
        "log.sec_stalls": "kernel block stalls — freeze precursors",
        "set.language": "Language",
        "set.interval": "Sample interval",
        "set.interval_hint": "Every tick reads all of /proc — 5 s is easy on the machine.",
        "set.tray": "Start in the system tray",
        "set.tray_hint": ("The window stays closed; the tray icon carries the level. "
                          "Closing the window also only hides it."),
        "set.tray_missing": "No tray support found — the window always starts visible.",
        "set.autoopen": "Bring the window to the front automatically",
        "set.autoopen_level": "From level",
        "set.autoopen_hint": ("Once per incident: the window only reappears after the "
                              "level has dropped below the threshold again."),
        "set.notify": "Desktop notifications",
        "set.notify_level": "Notify from level",
        "set.notify_level_hint": ("Quiet by default: only the level at which the "
                                  "guard is about to kill. The tray icon carries "
                                  "every level anyway."),
        "set.notify_cooldown": "At most one notification every",
        "set.notify_cooldown_hint": ("Only on escalation, never with urgency "
                                     "'critical' - those never expire - and always "
                                     "replacing the previous bubble instead of "
                                     "stacking. Each one closes itself after 3 "
                                     "seconds, because this desktop ignores the "
                                     "expire hint."),
        "set.minutes": "{n} min",
        "set.panel_label": "Show free memory in the panel",
        "set.autostart": "Start on login",
        "set.autostart_hint": "Writes {path}",
        "set.record_file": "Recording",
        "set.seconds": "{n} s",
        "set.saved": "saved to {path}",
        "chart.some10": "some10",
        "chart.full10": "full10",
        "chart.full60": "full60",
        "chart.threshold": "kill 20 %",
        "chart.swapout": "swap-out",
        "chart.now": "now",
        "chart.minutes_ago": "-{n} min",
        "chart.seconds_ago": "-{n} s",
        "tray.level": "Level: {level}",
        "err.no_terminal": "no terminal found",
        "instance.title": "Freeze Guard is already running",
        "instance.focused": ("The existing window has been brought to the front. "
                             "Only one instance may run: two of them would sample "
                             "/proc twice, fight over the same tray icon and report "
                             "the same incident twice."),
        "instance.blocked": ("Only one instance may run at a time. The running one "
                             "(pid {pid}) did not answer - quit it through its tray "
                             "icon, or end it with: kill {pid}"),
        "instance.blocked_nopid": ("Only one instance may run at a time, and the "
                                   "running one did not answer. Quit it through its "
                                   "tray icon."),
        "reason.stall": "stall condition met — the guard is about to step in",
        "reason.all_blocked": "all tasks are blocked on memory",
        "reason.kernel_stalls": "kernel reports block-layer stalls (freeze precursor)",
        "reason.full_started": "full blockages on memory have begun",
        "reason.below12": "less than 12 % of memory available",
        "reason.thrashing": "heavy swapping — thrashing risk",
        "reason.waiting": "tasks are noticeably waiting for memory",
        "reason.tight": "memory is getting tight",
        "reason.swapping": "swapping has started",
        "reason.runaway": "sustained load: {comm} at {pct} % CPU",
        "reason.calm": "nothing unusual",
    },
    "de": {
        "app.title": "Freeze Guard",
        "app.subtitle": "Speicherdruck und Freeze-Frühwarnung",
        "level.ok": "OK",
        "level.raised": "ERHÖHT",
        "level.warning": "WARNUNG",
        "level.critical": "KRITISCH",
        "page.live": "Live",
        "page.history": "Verlauf",
        "page.log": "Protokoll",
        "page.sim": "Simulation",
        "page.settings": "Einstellungen",
        "hdr.pause": "Pause",
        "hdr.paused": "angehalten",
        "menu.console": "Konsolen-Dashboard im Terminal öffnen",
        "menu.settings": "Einstellungen",
        "menu.tray": "In die Systemleiste minimieren",
        "menu.language": "Sprache",
        "menu.copy_install": "Installationsbefehl kopieren",
        "menu.quit": "Beenden",
        "tray.hint_title": "Freeze Guard überwacht weiter",
        "tray.hint_body": ("Das Fenster ist nur versteckt - das Symbol in der "
                           "Systemleiste bleibt. Beenden über das Menü des Symbols, "
                           "oder \"In der Systemleiste starten\" in den "
                           "Einstellungen abschalten."),
        "menu.show": "Fenster zeigen",
        "card.memory": "Speicher",
        "card.state": "Zustand",
        "card.psi": "Speicherdruck — Anteil der Zeit, in der Tasks blockieren",
        "card.swapout": "Auslagern aus dem RAM",
        "card.cpu": ("CPU-Auslastung — us Anwendungen · sy Kernel · wa I/O-Warten · "
                     "si Softirq"),
        "card.top_mem": "Größter Speicherverbrauch",
        "card.top_cpu": "Höchste CPU-Last",
        "card.services": "Schutzschichten",
        "card.victims": "Opferauswahl — reine Simulation, hier wird nichts beendet",
        "card.record": "Aufzeichnung — rote Flächen sind Ausfälle",
        "card.outages": "Ausfälle und Alarme",
        "card.log": "Protokoll",
        "card.no_record": "Keine Aufzeichnung",
        "mem.ram": "RAM  {used} von {total} GB belegt   ·   {pct} % frei",
        "mem.swap": "Swap  {used} von {total} GB belegt   ·   {zram}",
        "mem.swap_none": "Swap  nicht aktiv — kein Puffer vor dem OOM-Killer",
        "mem.zram": "zram {size} GB",
        "mem.no_zram": "ohne zram!",
        "mem.marker": "Markierung: unter 15 % freiem Speicher greift der Guard ein.",
        "mem.thresholds": ("Der Guard beendet einen Prozess erst, wenn full60 ≥ 20 % UND "
                           "weniger als 15 % Speicher verfügbar sind — beides 60 s "
                           "durchgehend."),
        "tile.psi": "Druck full60",
        "tile.psi_sub": "full10 {f10} %  ·  some10 {some10} %",
        "tile.psi_hint": "Kill ab 20 %",
        "tile.swapout": "Auslagern",
        "tile.swapout_sub": "herein {rate}  ·  MajFaults {majflt}/s",
        "tile.swapout_hint": "aus dem RAM",
        "tile.stalls": "Kernel-Stalls",
        "tile.stalls_hint": "Vorbote des Freeze",
        "tile.stalls_new": "+{n} NEU!",
        "tile.stalls_denied": "kein Zugriff auf /dev/kmsg",
        "tile.load": "Load",
        "tile.load_sub": "io-full10 {io} %  ·  wa {wa} %",
        "tile.load_hint": "{cpus} CPUs",
        "col.process": "Prozess",
        "col.rss": "RSS",
        "col.cpu": "CPU",
        "col.pid": "PID",
        "col.points": "Punkte (MB)",
        "col.category": "Kategorie",
        "col.kill": "Beenden",
        "col.time": "Zeitpunkt",
        "col.level": "Stufe",
        "col.mem_free": "Speicher frei",
        "col.psi_full": "Druck full10",
        "col.swapout": "Auslagern",
        "col.wa_sy": "wa/sy",
        "col.top": "Größte Verbraucher",
        "kill.tip": "Diesen Prozess beenden: sofort SIGTERM, nach 8 s SIGKILL",
        "kill.sent": "SIGTERM an PID {pid} ({comm}) gesendet",
        "kill.killed": "PID {pid} ({comm}) ignorierte SIGTERM — SIGKILL gesendet",
        "kill.gone": "PID {pid} existiert nicht mehr",
        "kill.denied": "PID {pid} gehört einem anderen Benutzer — frage nach dem Passwort",
        "kill.failed": "PID {pid} ließ sich nicht beenden: {err}",
        "kill.runaway": "← Dauerlast",
        "svc.guard": "Guard",
        "svc.recorder": "Aufzeichnung",
        "svc.active": "aktiv",
        "svc.stopped": "gestoppt",
        "svc.missing": "nicht installiert",
        "svc.working": "schalte …",
        "svc.tip": ("Ein: systemctl enable --now — startet den Dienst und aktiviert ihn "
                    "beim Systemstart. Aus: disable --now. Läuft über pkexec, das "
                    "Passwort fragt der Desktop ab, nie dieses Programm."),
        "svc.missing_tip": "Unit-Datei fehlt — erst installieren: {cmd}",
        "svc.enable_all": "Alle aktivieren",
        "svc.install": "Schutz installieren …",
        "svc.installing": "installiere — der Desktop fragt das Passwort, danach "
                          "dauert es einen Moment",
        "svc.install_done": "Schutz installiert und gestartet",
        "svc.install_failed": "Installation fehlgeschlagen: {err}",
        "svc.install_missing": "Installationsskript nicht gefunden: {path}",
        "svc.hint_none": ("Es ist noch nichts installiert — deshalb sind die Schalter "
                          "aus. Das Installationsskript richtet zram, earlyoom, den "
                          "Guard und die Aufzeichnung ein."),
        "svc.hint_some": "nicht installiert: {units}",
        "svc.hint_missing": "Der Schutz ist nicht installiert: {cmd}",
        "svc.failed": "{unit}: Schalten fehlgeschlagen ({err})",
        "sim.auto": "Laufend aktualisieren",
        "sim.target": "Ziel: PID {pid} · {comm} · {mb} MB",
        "sim.no_target": "Kein Ziel über 200 MB — der Guard würde nichts tun.",
        "sim.hint": ("Punkte = RSS × Gewicht, die höchste gewinnt; unter 200 MB wird "
                     "nichts beendet, weil das den Stall nur vertagen würde. Bevorzugt "
                     "werden gefahrlos neustartbare Build- und Renderer-Prozesse (×3), "
                     "die IDE zählt nur ×0,15, Systemdienste sind ausgenommen. Wer "
                     "5 Minuten ≥ 90 % einer CPU hält, gilt als Verursacher und zählt "
                     "doppelt."),
        "sim.tier_protected": "geschützt",
        "sim.tier_prefer": "bevorzugt ×3",
        "sim.tier_ide": "IDE ×0,15",
        "sim.tier_normal": "normal ×1",
        "sim.tier_suspect": " · Dauerlast ×2",
        "hist.range": "Zeitraum",
        "hist.reload": "Neu laden",
        "hist.hours6": "6 Stunden",
        "hist.hours24": "24 Stunden",
        "hist.hours48": "48 Stunden",
        "hist.days7": "7 Tage",
        "hist.days30": "30 Tage",
        "hist.summary": ("{rows} Messungen · {span} · {outages} · {alarms} Messungen "
                         "über OK"),
        "hist.outage_one": "1 Ausfall",
        "hist.outage_many": "{n} Ausfälle",
        "hist.outage": "Ausfall {start}",
        "hist.outage_dead": "{min} min tot",
        "hist.outage_until": "bis {end}",
        "hist.alarms": "Alarme über Stufe OK",
        "hist.alarms_count": "{n} Messungen",
        "hist.alarms_last": "die letzten 20",
        "hist.calm": "Kein Ausfall, keine Messung über OK",
        "hist.calm_sub": "der Speicherdruck blieb im gewählten Zeitraum harmlos",
        "hist.missing_title": "{path} existiert nicht.",
        "hist.missing_body": ("Die Blackbox-Aufzeichnung läuft nicht. Ohne sie "
                              "hinterlässt der nächste Freeze keine Spuren — nach einem "
                              "harten Reset ist nicht mehr feststellbar, was die "
                              "Maschine in ihren letzten Minuten getan hat.\n"
                              "Einrichten:  {cmd}\nPrüfen:  systemctl status "
                              "freeze-guard-recorder"),
        "hist.denied_title": "{path} ist nicht lesbar.",
        "hist.denied_body": ("Die Datei gehört root. Entweder Leserechte setzen "
                             "(sudo chmod a+r {path}) oder die Auswertung im Terminal "
                             "aufrufen: sudo freeze-guard history"),
        "hist.empty_title": "Keine auswertbaren Daten in {path}.",
        "hist.empty_body": ("Grund: {err}. Der Recorder schreibt alle 5 s — bei kurzer "
                            "Laufzeit ist das normal."),
        "hist.copy": "Installationsbefehl kopieren",
        "hist.legend_free": "Speicher frei",
        "hist.legend_psi": "Druck full10",
        "log.refresh": "Aktualisieren",
        "log.reading": "lese Journal …",
        "log.state": "Stand {time}",
        "log.none": "(keine Einträge)",
        "log.unavailable": "(nicht abfragbar: {err})",
        "log.sec_guard": "freeze-guard",
        "log.sec_earlyoom": "earlyoom — nur Eingriffe",
        "log.sec_oom": "Kernel-OOM-Kills",
        "log.sec_stalls": "Kernel-Block-Stalls — Freeze-Vorboten",
        "set.language": "Sprache",
        "set.interval": "Messintervall",
        "set.interval_hint": ("Jeder Takt liest ganz /proc — 5 s sind schonend für die "
                              "Maschine."),
        "set.tray": "In der Systemleiste starten",
        "set.tray_hint": ("Das Fenster bleibt zu; das Symbol trägt die Stufe. Auch "
                          "Schließen versteckt das Fenster nur."),
        "set.tray_missing": ("Keine Systemleisten-Unterstützung gefunden — das Fenster "
                             "startet immer sichtbar."),
        "set.autoopen": "Fenster automatisch nach vorn holen",
        "set.autoopen_level": "Ab Stufe",
        "set.autoopen_hint": ("Einmal pro Vorfall: das Fenster kommt erst wieder, wenn "
                              "die Stufe zwischendurch unter die Schwelle fiel."),
        "set.notify": "Desktop-Benachrichtigungen",
        "set.notify_level": "Melden ab Stufe",
        "set.notify_level_hint": ("Standard ist leise: nur die Stufe, auf der der "
                                  "Guard gleich eingreift. Das Symbol in der "
                                  "Systemleiste trägt ohnehin jede Stufe."),
        "set.notify_cooldown": "Höchstens eine Meldung alle",
        "set.notify_cooldown_hint": ("Nur bei Verschlechterung, nie mit Dringlichkeit "
                                     "'critical' - die laufen nie ab - und immer die "
                                     "vorherige Meldung ersetzend statt stapelnd. "
                                     "Jede schließt sich nach 3 Sekunden selbst, "
                                     "weil dieser Desktop die Ablaufzeit ignoriert."),
        "set.minutes": "{n} min",
        "set.panel_label": "Freien Speicher in der Systemleiste anzeigen",
        "set.autostart": "Beim Anmelden starten",
        "set.autostart_hint": "Schreibt {path}",
        "set.record_file": "Aufzeichnung",
        "set.seconds": "{n} s",
        "set.saved": "gespeichert in {path}",
        "chart.some10": "some10",
        "chart.full10": "full10",
        "chart.full60": "full60",
        "chart.threshold": "Kill 20 %",
        "chart.swapout": "Auslagern aus",
        "chart.now": "jetzt",
        "chart.minutes_ago": "-{n} min",
        "chart.seconds_ago": "-{n} s",
        "tray.level": "Stufe: {level}",
        "err.no_terminal": "kein Terminal gefunden",
        "instance.title": "Freeze Guard läuft bereits",
        "instance.focused": ("Das vorhandene Fenster wurde nach vorn geholt. Es darf "
                             "nur eine Instanz laufen: zwei würden /proc doppelt "
                             "abfragen, sich um dasselbe Symbol in der Systemleiste "
                             "streiten und denselben Vorfall zweimal melden."),
        "instance.blocked": ("Es darf nur eine Instanz laufen. Die laufende "
                             "(PID {pid}) antwortet nicht - über ihr Symbol in der "
                             "Systemleiste beenden, oder mit: kill {pid}"),
        "instance.blocked_nopid": ("Es darf nur eine Instanz laufen, und die laufende "
                                   "antwortet nicht. Über ihr Symbol in der "
                                   "Systemleiste beenden."),
        "reason.stall": "Stall-Bedingung erfüllt — der Guard greift gleich ein",
        "reason.all_blocked": "Alle Tasks blockieren auf Speicher",
        "reason.kernel_stalls": "Kernel meldet Block-Layer-Stalls (Vorbote des Freeze)",
        "reason.full_started": "Totalblockaden auf Speicher haben begonnen",
        "reason.below12": "Weniger als 12 % Speicher verfügbar",
        "reason.thrashing": "Starkes Auslagern — Thrashing-Gefahr",
        "reason.waiting": "Tasks warten spürbar auf Speicher",
        "reason.tight": "Speicher wird knapp",
        "reason.swapping": "Auslagerung hat eingesetzt",
        "reason.runaway": "Dauerlast: {comm} bei {pct} % CPU",
        "reason.calm": "unauffällig",
    },
}

LANG = "en"


def reason_text(sample):
    """The monitor ships a German sentence plus an i18n key. Prefer the key; fall
    back to its text if an older monitor is installed alongside."""
    key, fmt = sample.get("reason_i18n", (None, {}))
    if key and key in STR["en"]:
        return t(key, **fmt)
    return sample.get("reason", "")


def t(key, **fmt):
    text = STR.get(LANG, STR["en"]).get(key) or STR["en"].get(key, key)
    return text.format(**fmt) if fmt else text


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
class Settings:
    """Plain JSON in ~/.config/freeze-guard/settings.json - small enough that a
    schema would cost more than it buys, and editable by hand."""

    DEFAULTS = {
        "lang": "en",
        "interval": 5.0,
        "start_in_tray": True,
        "auto_open": True,
        "auto_open_level": ORANGE,
        "notify": True,
        "notify_cooldown": 900.0,   # seconds between two notifications
        "notify_level": RED,        # quiet by default: only the kill condition
        "tray_hint_shown": False,   # "closing only hides me" - shown once
        "panel_label": True,
        "history_file": HISTORY_FILE,
    }

    def __init__(self):
        self.data = dict(self.DEFAULTS)
        try:
            with open(CONFIG_FILE) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update({k: v for k, v in loaded.items() if k in self.DEFAULTS})
        except (OSError, ValueError):
            pass
        if self.data["lang"] not in STR:
            self.data["lang"] = "en"
        try:
            self.data["interval"] = min(60.0, max(1.0, float(self.data["interval"])))
            self.data["auto_open_level"] = int(self.data["auto_open_level"])
            self.data["notify_cooldown"] = min(
                3600.0, max(60.0, float(self.data["notify_cooldown"])))
            self.data["notify_level"] = int(self.data["notify_level"])
            if self.data["notify_level"] not in (YELLOW, ORANGE, RED):
                self.data["notify_level"] = RED
        except (TypeError, ValueError):
            self.data["interval"] = self.DEFAULTS["interval"]
            self.data["auto_open_level"] = self.DEFAULTS["auto_open_level"]
            self.data["notify_cooldown"] = self.DEFAULTS["notify_cooldown"]
            self.data["notify_level"] = self.DEFAULTS["notify_level"]

    def __getitem__(self, key):
        return self.data[key]

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def save(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
            os.replace(tmp, CONFIG_FILE)
        except OSError:
            pass  # a read-only home must not take the dashboard down

    # -- autostart is a file, not a value: keep it derived ------------------
    @staticmethod
    def autostart_enabled():
        return os.path.exists(AUTOSTART_FILE)

    @staticmethod
    def set_autostart(enabled):
        try:
            if not enabled:
                if os.path.exists(AUTOSTART_FILE):
                    os.remove(AUTOSTART_FILE)
                return True
            os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
            exe = "/usr/local/bin/fg-gui"
            if not os.path.exists(exe):
                exe = os.path.join(APP_DIR, "run.py")
            with open(AUTOSTART_FILE, "w") as f:
                f.write("[Desktop Entry]\n"
                        "Type=Application\n"
                        "Name=Freeze Guard\n"
                        "Comment=Memory pressure and freeze early warning\n"
                        f"Exec={exe} --quiet\n"
                        "Icon=utilities-system-monitor\n"
                        "Terminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n")
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# palette - the validated data-viz palette, one selected step set per mode
# ---------------------------------------------------------------------------
LIGHT = {
    "surface": "#fcfcfb",
    "plane": "#f9f9f7",
    "ink": "#0b0b0b",
    "ink2": "#52514e",
    "muted": "#898781",
    "grid": "#e1e0d9",
    "axis": "#c3c2b7",
    "border": "rgba(11,11,11,0.10)",
    "series": ("#2a78d6", "#eb6834", "#1baf7a", "#eda100"),
}
DARK = {
    "surface": "#1a1a19",
    "plane": "#0d0d0d",
    "ink": "#ffffff",
    "ink2": "#c3c2b7",
    "muted": "#898781",
    "grid": "#2c2c2a",
    "axis": "#383835",
    "border": "rgba(255,255,255,0.10)",
    "series": ("#3987e5", "#d95926", "#199e70", "#c98500"),
}
STATUS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}

PAL = dict(LIGHT)  # replaced at startup and whenever the GTK theme changes


def rgba(hexcolor, alpha=1.0):
    h = hexcolor.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255, alpha)


def detect_dark(widget):
    settings = Gtk.Settings.get_default()
    if settings is not None:
        try:
            if settings.get_property("gtk-application-prefer-dark-theme"):
                return True
            if "dark" in (settings.get_property("gtk-theme-name") or "").lower():
                return True
        except TypeError:
            pass
    ok, col = widget.get_style_context().lookup_color("theme_bg_color")
    if ok and col.alpha > 0.1:
        return 0.2126 * col.red + 0.7152 * col.green + 0.0722 * col.blue < 0.5
    return False


CSS_TEMPLATE = """
.fg-plane        { background-color: %(plane)s; }
.fg-card         { background-color: %(surface)s;
                   border: 1px solid %(border)s; border-radius: 8px; }
.fg-card-title   { color: %(muted)s; font-size: 90%%; font-weight: 700; }
.fg-ink          { color: %(ink)s; }
.fg-sub          { color: %(ink2)s; font-size: 90%%; }
.fg-muted        { color: %(muted)s; font-size: 90%%; }
.fg-value        { color: %(ink)s; font-size: 170%%; font-weight: 700; }
.fg-banner       { border-radius: 8px; padding: 8px 12px; }
.fg-banner-good     { background-color: alpha(%(good)s, 0.14);
                      border-left: 4px solid %(good)s; }
.fg-banner-warning  { background-color: alpha(%(warning)s, 0.18);
                      border-left: 4px solid %(warning)s; }
.fg-banner-serious  { background-color: alpha(%(serious)s, 0.20);
                      border-left: 4px solid %(serious)s; }
.fg-banner-critical { background-color: alpha(%(critical)s, 0.24);
                      border-left: 4px solid %(critical)s; }
.fg-badge        { font-weight: 800; font-size: 115%%; color: %(ink)s; }
.fg-icon-good     { color: %(good)s; font-weight: 800; }
.fg-icon-warning  { color: %(warning)s; font-weight: 800; }
.fg-icon-serious  { color: %(serious)s; font-weight: 800; }
.fg-icon-critical { color: %(critical)s; font-weight: 800; }
.fg-hint         { color: %(ink2)s; font-size: 90%%; }
"""

_css_provider = None


def apply_theme(widget):
    """Pick the light or dark step set and (re)load the CSS."""
    global PAL, _css_provider
    PAL = dict(DARK if detect_dark(widget) else LIGHT)
    data = CSS_TEMPLATE % dict(PAL, **STATUS)
    if _css_provider is None:
        _css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), _css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_provider.load_from_data(data.encode())


# ---------------------------------------------------------------------------
# formatting helpers - decimal comma in German, decimal point in English
# ---------------------------------------------------------------------------
def num(v, digits=1):
    s = f"{v:.{digits}f}"
    return s.replace(".", ",") if LANG == "de" else s


def gb(kb):
    return num(kb / 1048576.0, 1)


def rate_text(kbs):
    if kbs >= 10240:
        return f"{num(kbs / 1024.0, 1)} MB/s"
    return f"{num(kbs, 0)} kB/s"


def nice_ceil(v):
    if v <= 0:
        return 1.0
    exp = math.floor(math.log10(v))
    base = 10.0**exp
    for m in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if v <= m * base:
            return m * base
    return 10 * base


def rounded_rect(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def card(title, child, expand=True):
    """A titled surface. The charts validate their colours against this
    background, so every plot lives inside one of these."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.get_style_context().add_class("fg-card")
    box.set_border_width(10)
    if title:
        lbl = Gtk.Label(label=title.upper(), xalign=0.0)
        lbl.get_style_context().add_class("fg-card-title")
        lbl.set_ellipsize(Pango.EllipsizeMode.END)  # never widen the window
        box.pack_start(lbl, False, False, 0)
    box.pack_start(child, expand, expand, 0)
    return box


def small(text, cls="fg-sub", xalign=0.0, ellipsize=False, wrap=0):
    lbl = Gtk.Label(label=text, xalign=xalign)
    lbl.get_style_context().add_class(cls)
    if ellipsize:
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        lbl.set_max_width_chars(12)
    if wrap:
        lbl.set_line_wrap(True)
        lbl.set_max_width_chars(wrap)
    return lbl


# ---------------------------------------------------------------------------
# chart widgets
# ---------------------------------------------------------------------------
class TimeChart(Gtk.DrawingArea):
    """Multi-series line chart on a single y axis.

    One axis, always: measures with different units get their own chart rather
    than a second scale, because two scales in one frame invite exactly the
    misreading this tool must not produce.

    Two x modes: 'index' for the live view (evenly spaced samples, the line
    grows in from the left and then scrolls) and 'time' for the recording, where
    the distance between samples carries meaning - a gap IS the freeze.
    """

    PAD_L, PAD_R, PAD_T, PAD_B = 44, 10, 20, 16

    def __init__(self, series, unit="%", min_ymax=10.0, threshold=None,
                 threshold_label="", capacity=180, height=96, x_mode="index",
                 digits=1):
        super().__init__()
        self.series = series  # [(label, colour slot)]
        self.unit = unit
        self.min_ymax = min_ymax
        self.threshold = threshold
        self.threshold_label = threshold_label
        self.capacity = capacity
        self.x_mode = x_mode
        self.digits = digits
        self.times = deque(maxlen=capacity)
        self.data = [deque(maxlen=capacity) for _ in series]
        self.bands = []  # [(t0, t1)] outages, time mode only
        self.hover = None
        self.set_size_request(-1, height)
        self.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("leave-notify-event", self._on_leave)

    # -- data ---------------------------------------------------------------
    def push(self, ts, values):
        self.times.append(ts)
        for dq, v in zip(self.data, values):
            dq.append(float(v))
        self.queue_draw()

    def set_data(self, times, columns, bands=()):
        self.times = deque(times)
        self.data = [deque(c) for c in columns]
        self.bands = list(bands)
        self.queue_draw()

    # -- hover --------------------------------------------------------------
    def _on_motion(self, _w, event):
        i = self._index_at(event.x)
        if i != self.hover:
            self.hover = i
            self.queue_draw()
        return False

    def _on_leave(self, *_a):
        if self.hover is not None:
            self.hover = None
            self.queue_draw()
        return False

    def _index_at(self, px):
        n = len(self.times)
        if n == 0:
            return None
        x0, _y0, pw, _ph = self._plot()
        if pw <= 0:
            return None
        best, bestd = None, 1e9
        for i in range(n):
            d = abs(self._x(i, x0, pw) - px)
            if d < bestd:
                best, bestd = i, d
        return best if bestd <= 28 else None

    # -- geometry -----------------------------------------------------------
    def _plot(self):
        a = self.get_allocation()
        return (self.PAD_L, self.PAD_T,
                max(1, a.width - self.PAD_L - self.PAD_R),
                max(1, a.height - self.PAD_T - self.PAD_B))

    def _x(self, i, x0, pw):
        n = len(self.times)
        if self.x_mode == "time" and n > 1:
            t0, t1 = self.times[0], self.times[-1]
            return x0 + (self.times[i] - t0) / max(1e-6, t1 - t0) * pw
        return x0 + i * (pw / max(1, self.capacity - 1))

    def _ymax(self):
        vals = [v for dq in self.data for v in dq]
        top = max(vals) if vals else 0.0
        if self.threshold:
            top = max(top, self.threshold * 1.15)
        return nice_ceil(max(top, self.min_ymax))

    # -- painting -----------------------------------------------------------
    def do_draw(self, cr):
        cr.select_font_face("Sans")
        x0, y0, pw, ph = self._plot()
        ymax = self._ymax()
        cr.set_line_width(1)

        def ypos(v):
            return y0 + ph - min(1.0, max(0.0, v / ymax)) * ph

        # outage bands first - everything else is read on top of them
        if self.bands and len(self.times) > 1:
            t0, t1 = self.times[0], self.times[-1]
            span = max(1e-6, t1 - t0)
            for b0, b1 in self.bands:
                bx0 = x0 + (max(b0, t0) - t0) / span * pw
                bx1 = max(bx0 + 2, x0 + (min(b1, t1) - t0) / span * pw)
                cr.set_source_rgba(*rgba(STATUS["critical"], 0.16))
                cr.rectangle(bx0, y0, bx1 - bx0, ph)
                cr.fill()

        # grid + y labels
        cr.set_font_size(9)
        for frac in (0.0, 0.5, 1.0):
            y = y0 + ph - frac * ph
            cr.set_source_rgba(*rgba(PAL["grid"] if frac else PAL["axis"]))
            cr.move_to(x0, round(y) + 0.5)
            cr.line_to(x0 + pw, round(y) + 0.5)
            cr.stroke()
            tick = ymax * frac
            label = num(tick, 0 if abs(tick - round(tick)) < 0.05 else 1)
            cr.set_source_rgba(*rgba(PAL["muted"]))
            cr.move_to(x0 - 6 - cr.text_extents(label).width, y + 3)
            cr.show_text(label)
        cr.set_source_rgba(*rgba(PAL["muted"]))
        cr.move_to(4, y0 - 7)
        cr.show_text(self.unit)

        # the guard's own kill threshold, so the numbers have a reference
        if self.threshold and self.threshold <= ymax:
            cr.save()
            cr.set_dash((3, 3))
            cr.set_source_rgba(*rgba(PAL["muted"], 0.9))
            y = round(ypos(self.threshold)) + 0.5
            cr.move_to(x0, y)
            cr.line_to(x0 + pw, y)
            cr.stroke()
            cr.restore()
            if self.threshold_label:
                cr.set_source_rgba(*rgba(PAL["muted"]))
                ext = cr.text_extents(self.threshold_label)
                cr.move_to(x0 + pw - ext.width, ypos(self.threshold) - 4)
                cr.show_text(self.threshold_label)

        n = len(self.times)
        colors = [PAL["series"][slot] for _lbl, slot in self.series]

        # a single series gets an area fill; several would only turn to mud
        if n > 1 and len(self.series) == 1:
            cr.set_source_rgba(*rgba(colors[0], 0.16))
            cr.move_to(self._x(0, x0, pw), y0 + ph)
            for i in range(n):
                cr.line_to(self._x(i, x0, pw), ypos(self.data[0][i]))
            cr.line_to(self._x(n - 1, x0, pw), y0 + ph)
            cr.close_path()
            cr.fill()

        cr.set_line_width(2)
        cr.set_line_join(1)  # round
        for si, color in enumerate(colors):
            if n < 2:
                break
            cr.set_source_rgba(*rgba(color))
            cr.move_to(self._x(0, x0, pw), ypos(self.data[si][0]))
            for i in range(1, n):
                cr.line_to(self._x(i, x0, pw), ypos(self.data[si][i]))
            cr.stroke()

        cr.set_font_size(10)
        if len(self.series) == 1:
            # one series needs no legend - the card title names it. The current
            # value rides along at the end of the line instead.
            if n and self.x_mode != "time":
                cur = self.data[0][-1]
                text = f"{num(cur, self.digits)} {self.unit}"
                ext = cr.text_extents(text)
                cr.set_source_rgba(*rgba(PAL["ink2"]))
                cr.move_to(min(self._x(n - 1, x0, pw) + 6, x0 + pw - ext.width),
                           max(y0 + 9, min(ypos(cur) - 6, y0 + ph - 2)))
                cr.show_text(text)
        else:
            # legend with the current value - the value doubles as the direct
            # label; three of the light steps sit below 3:1 on this surface, so
            # the reading must never depend on the swatch alone
            lx = x0
            for si, (label, _slot) in enumerate(self.series):
                cur = self.data[si][-1] if self.data[si] else 0.0
                text = (label if self.x_mode == "time"
                        else f"{label} {num(cur, self.digits)}")
                cr.set_source_rgba(*rgba(colors[si]))
                rounded_rect(cr, lx, self.PAD_T - 13, 7, 7, 2)
                cr.fill()
                cr.set_source_rgba(*rgba(PAL["ink2"]))
                cr.move_to(lx + 11, self.PAD_T - 6)
                cr.show_text(text)
                lx += 11 + cr.text_extents(text).width + 14

        # x axis hints
        cr.set_font_size(9)
        cr.set_source_rgba(*rgba(PAL["muted"]))
        if n > 1:
            if self.x_mode == "time":
                left = time.strftime("%d.%m. %H:%M", time.localtime(self.times[0]))
                right = time.strftime("%d.%m. %H:%M", time.localtime(self.times[-1]))
            else:
                # window width in wall-clock terms, from the measured sample
                # spacing - rounded, because a jittery "-365 s" reads like a
                # measurement when it is only scheduling noise
                dt = (self.times[-1] - self.times[0]) / max(1, n - 1)
                span = self.capacity * max(0.5, dt)
                left = (t("chart.minutes_ago", n=round(span / 60)) if span >= 120
                        else t("chart.seconds_ago", n=int(round(span / 5) * 5)))
                right = t("chart.now")
            cr.move_to(x0, y0 + ph + 12)
            cr.show_text(left)
            cr.move_to(x0 + pw - cr.text_extents(right).width, y0 + ph + 12)
            cr.show_text(right)

        if self.hover is not None and self.hover < n:
            self._draw_hover(cr, x0, y0, pw, ph, ypos)

    def _draw_hover(self, cr, x0, y0, pw, ph, ypos):
        i = self.hover
        hx = self._x(i, x0, pw)
        cr.set_line_width(1)
        cr.set_source_rgba(*rgba(PAL["axis"]))
        cr.move_to(round(hx) + 0.5, y0)
        cr.line_to(round(hx) + 0.5, y0 + ph)
        cr.stroke()

        lines = [time.strftime("%H:%M:%S", time.localtime(self.times[i]))]
        for si, (label, slot) in enumerate(self.series):
            v = self.data[si][i]
            lines.append(f"{label}  {num(v, self.digits)} {self.unit}")
            cr.set_source_rgba(*rgba(PAL["surface"]))  # 2px surface ring
            cr.arc(hx, ypos(v), 4.5, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(*rgba(PAL["series"][slot]))
            cr.arc(hx, ypos(v), 3.5, 0, 2 * math.pi)
            cr.fill()

        cr.set_font_size(10)
        w = max(cr.text_extents(line).width for line in lines) + 14
        h = 6 + 13 * len(lines)
        bx = min(hx + 10, x0 + pw - w)
        by = max(y0 + 2, min(y0 + 4, y0 + ph - h))
        cr.set_source_rgba(*rgba(PAL["surface"], 0.97))
        rounded_rect(cr, bx, by, w, h, 5)
        cr.fill_preserve()
        cr.set_source_rgba(*rgba(PAL["axis"]))
        cr.stroke()
        for k, line in enumerate(lines):
            cr.set_source_rgba(*rgba(PAL["ink"] if k == 0 else PAL["ink2"]))
            cr.move_to(bx + 7, by + 15 + 13 * k)
            cr.show_text(line)


class Meter(Gtk.DrawingArea):
    """Horizontal usage bar. Colour carries the status, but the caption next to
    it always spells the number out - the fill is never the only signal."""

    def __init__(self, height=14, marker=None, marker_tip=None):
        super().__init__()
        self.frac = 0.0
        self.status = "good"
        self.marker = marker  # fraction of the track, e.g. the kill threshold
        self.set_size_request(-1, height)
        if marker_tip:
            self.set_tooltip_text(marker_tip)

    def set(self, frac, status):
        self.frac = max(0.0, min(1.0, frac))
        self.status = status
        self.queue_draw()

    def do_draw(self, cr):
        a = self.get_allocation()
        w, h = a.width, a.height
        cr.set_source_rgba(*rgba(PAL["grid"]))
        rounded_rect(cr, 0, 0, w, h, 4)
        cr.fill()
        if self.frac > 0:
            cr.set_source_rgba(*rgba(STATUS[self.status]))
            rounded_rect(cr, 0, 0, max(6, self.frac * w), h, 4)
            cr.fill()
        if self.marker is not None:
            cr.set_source_rgba(*rgba(PAL["ink"], 0.45))
            cr.set_line_width(1)
            cr.move_to(round(self.marker * w) + 0.5, 0)
            cr.line_to(round(self.marker * w) + 0.5, h)
            cr.stroke()


# ---------------------------------------------------------------------------
# process list with a per-row stop button
# ---------------------------------------------------------------------------
# Notifications are never sent with urgency "critical": per the desktop
# notification spec a critical notification does not expire, so a flapping alert
# level builds a wall of bubbles that cannot be dismissed.
#
# The expire-timeout below is only a hint, and Cinnamon ignores it - measured:
# a bubble sent with 3000 ms produced no NotificationClosed signal at all. So the
# lifetime is enforced from here, by closing the bubble ourselves.
NOTIFY_VISIBLE_SECONDS = 3
NOTIFY_TIMEOUT_MS = NOTIFY_VISIBLE_SECONDS * 1000
NOTIFY_ICON = "utilities-system-monitor"

KILL_ICON = "process-stop-symbolic"
# Seconds between SIGTERM and SIGKILL. A constant, not a literal, so tests can
# exercise the escalation without sitting through it.
KILL_ESCALATE_SECONDS = 8


class ProcTable(Gtk.Box):
    """Process list with a stop button in the last column.

    The button acts immediately - SIGTERM now, SIGKILL after 8 s. That is
    deliberate: during a stall, a confirmation dialog is one more thing that has
    to be scheduled, drawn and read while the machine is already unresponsive.
    """

    def __init__(self, columns, rows_visible, on_kill):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_kill = on_kill
        self.ncols = len(columns)
        # store layout: <text columns…> | icon | weight | pid | comm
        self.store = Gtk.ListStore(*([str] * self.ncols + [str, int, int, str]))
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_enable_search(False)
        self.view.get_style_context().add_class("fg-ink")
        for i, (title, xalign, expand) in enumerate(columns):
            renderer = Gtk.CellRendererText(xalign=xalign)
            col = Gtk.TreeViewColumn(title, renderer, text=i, weight=self.ncols + 1)
            col.set_alignment(xalign)
            col.set_expand(expand)
            self.view.append_column(col)
        icon = Gtk.CellRendererPixbuf(xalign=0.5,
                                      stock_size=int(Gtk.IconSize.MENU))
        self.kill_col = Gtk.TreeViewColumn(t("col.kill"), icon, icon_name=self.ncols)
        self.kill_col.set_alignment(0.5)
        self.kill_col.set_min_width(62)
        self.view.append_column(self.kill_col)
        self.view.connect("button-press-event", self._on_click)
        self.view.set_has_tooltip(True)
        self.view.connect("query-tooltip", self._on_tooltip)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(26 + rows_visible * 24)
        scroller.add(self.view)
        self.pack_start(scroller, True, True, 0)

    def fill(self, rows):
        """rows: [(text…, weight, pid, comm)] - the icon column is filled in
        here, so callers never have to know about it."""
        prepared = [tuple(r[:self.ncols]) + (KILL_ICON,) + tuple(r[self.ncols:])
                    for r in rows]
        if len(self.store) == len(prepared):
            for i, row in enumerate(prepared):
                it = self.store.get_iter(Gtk.TreePath(i))
                for c, val in enumerate(row):
                    if self.store.get_value(it, c) != val:
                        self.store.set_value(it, c, val)
            return
        self.store.clear()
        for row in prepared:
            self.store.append(list(row))

    def _on_click(self, view, event):
        if event.button != 1:
            return False
        hit = view.get_path_at_pos(int(event.x), int(event.y))
        if not hit or hit[1] is not self.kill_col:
            return False
        it = self.store.get_iter(hit[0])
        self.on_kill(self.store.get_value(it, self.ncols + 2),
                     self.store.get_value(it, self.ncols + 3))
        return True

    def _on_tooltip(self, view, x, y, keyboard, tooltip):
        if keyboard:
            return False
        bx, by = view.convert_widget_to_bin_window_coords(x, y)
        hit = view.get_path_at_pos(bx, by)
        if not hit or hit[1] is not self.kill_col:
            return False
        tooltip.set_text(t("kill.tip"))
        return True


def kill_process(pid, comm, report):
    """SIGTERM now, SIGKILL after KILL_ESCALATE_SECONDS if it is still there.

    Processes owned by another user need root, which is why pkexec appears here -
    the desktop asks for the password, this program never sees it.
    """

    def escalate():
        # Verify identity before escalating: in those seconds the pid could in
        # principle have been recycled, and SIGKILL to the wrong process is
        # unforgivable.
        try:
            with open(f"/proc/{pid}/comm") as f:
                still = f.read().strip()
        except OSError:
            return False
        if still != comm:
            return False
        try:
            os.kill(pid, signal.SIGKILL)
            report(t("kill.killed", pid=pid, comm=comm))
        except PermissionError:
            _pkexec_kill(pid, "-KILL", report)
        except OSError:
            pass
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        report(t("kill.gone", pid=pid))
        return
    except PermissionError:
        report(t("kill.denied", pid=pid))
        _pkexec_kill(pid, "-TERM", report)
        GLib.timeout_add_seconds(KILL_ESCALATE_SECONDS, escalate)
        return
    except OSError as exc:
        report(t("kill.failed", pid=pid, err=exc.strerror or str(exc)))
        return
    report(t("kill.sent", pid=pid, comm=comm))
    GLib.timeout_add_seconds(KILL_ESCALATE_SECONDS, escalate)


def _pkexec_kill(pid, sig, report):
    try:
        subprocess.Popen(["pkexec", "/usr/bin/kill", sig, str(pid)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        report(t("kill.failed", pid=pid, err=str(exc)))


# ---------------------------------------------------------------------------
# service control
# ---------------------------------------------------------------------------
def systemctl(action, units, on_done):
    """enable --now / disable --now through pkexec, off the main loop."""

    def work():
        cmd = (["pkexec", "/usr/bin/systemctl", action, "--now"]
               + [f"{u}.service" for u in units])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if res.returncode == 0:
                err = ""
            elif res.stderr.strip():
                err = res.stderr.strip().splitlines()[-1]
            else:
                err = f"exit {res.returncode}"
        except (OSError, subprocess.SubprocessError) as exc:
            err = str(exc)
        GLib.idle_add(on_done, err)

    threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------------------
# sampling thread
# ---------------------------------------------------------------------------
class Poller(threading.Thread):
    """Reads /proc off the main loop and hands finished samples to GTK.

    It also keeps the guard's own suspect bookkeeping: a process at >= 90 % of a
    core for 5 minutes counts double in the victim ranking, and the simulation
    page has to show what the daemon would decide - not something close to it.
    """

    def __init__(self, on_sample, interval=5.0):
        super().__init__(daemon=True)
        self.on_sample = on_sample
        self.interval = interval
        self.paused = False
        self.suspect_since = {}
        self.suspects = set()
        self._stop = threading.Event()
        self._discard_next = False
        self.sampler = M.Sampler()
        self.error = None

    def stop(self):
        self._stop.set()

    def set_paused(self, paused):
        if self.paused and not paused:
            self._discard_next = True  # the pause window would skew the rates
        self.paused = paused

    def run(self):
        try:
            self.sampler.sample(want_procs=True)  # prime the deltas
        except Exception as exc:  # pragma: no cover
            self.error = str(exc)
        while not self._stop.wait(self.interval):
            if self.paused:
                continue
            try:
                s = self.sampler.sample(want_procs=True)
            except Exception as exc:  # pragma: no cover
                self.error = str(exc)
                continue
            if self._discard_next:
                self._discard_next = False
                continue
            self._track_suspects(s)
            s["suspects"] = set(self.suspects)
            GLib.idle_add(self.on_sample, s)

    def _track_suspects(self, s):
        now = time.time()
        seen = set()
        for _rss, pct, _comm, pid in s.get("by_cpu_all", s["by_cpu"]):
            seen.add(pid)
            if pct >= 90:
                self.suspect_since.setdefault(pid, now)
            else:
                self.suspect_since.pop(pid, None)
        for pid in list(self.suspect_since):
            if pid not in seen and not os.path.isdir(f"/proc/{pid}"):
                self.suspect_since.pop(pid, None)
        self.suspects = {pid for pid, since in self.suspect_since.items()
                         if now - since >= 300}


# ---------------------------------------------------------------------------
# victim simulation - mirrors select_victim() in fg-daemon.sh
# ---------------------------------------------------------------------------
WHITELIST = re.compile(
    r"^(systemd|systemd-.*|init|Xorg|Xwayland|cinnamon|cinnamon-.*|nemo|muffin|sshd"
    r"|NetworkManager|wpa_supplicant|dbus-daemon|dbus-broker.*|pipewire|pipewire-.*"
    r"|wireplumber|pulseaudio|lightdm|gdm.*|rtkit-daemon|polkitd|udisksd|earlyoom"
    r"|freeze-guard|fg-daemon[.]sh|fg-monitor|fg-gui|logger|awk|sleep|kill)$"
)
PREFER = re.compile(
    r"^(node|esbuild|tsserver|webpack|rollup|vite|next-server|jest|eslint|tsc|php"
    r"|php8[.][0-9]|composer|Web Content|Isolated Web Co|chrome|chromium)$"
)
PROTECTED = re.compile(r"^(code|claude|codium)$")
MIN_RSS_KB = 204800


def rank_victims(by_rss, suspects):
    """Score = RSS x tier weight, biggest wins, nothing under 200 MB. Same rule
    as the daemon, so this page can be trusted as a prediction."""
    rows, target = [], None
    for rss_kb, pct, comm, pid in by_rss:
        if int(pid) <= 2 or rss_kb <= 0:
            continue
        if WHITELIST.match(comm):
            tier, weight = t("sim.tier_protected"), 0.0
        elif PREFER.match(comm):
            tier, weight = t("sim.tier_prefer"), 3.0
        elif PROTECTED.match(comm):
            tier, weight = t("sim.tier_ide"), 0.15
        else:
            tier, weight = t("sim.tier_normal"), 1.0
        if weight and pid in suspects:
            weight *= 2.0
            tier += t("sim.tier_suspect")
        score = rss_kb * weight
        rows.append((pid, comm, rss_kb, pct, weight, score, tier))
        if weight > 0 and rss_kb >= MIN_RSS_KB and (target is None or score > target[5]):
            target = rows[-1]
    rows.sort(key=lambda r: -r[5])
    return rows, target


# ---------------------------------------------------------------------------
# history file
# ---------------------------------------------------------------------------
class History:
    def __init__(self, path, hours):
        self.path = path
        self.hours = hours
        self.error = None
        self.rows = []
        self.idx = {}
        self.gaps = []
        self.alarms = []
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            self.error = "missing"
            return
        try:
            with open(self.path) as f:
                header = f.readline().rstrip("\n").split(",")
                if not header or header[0] != "t":
                    self.error = "format"
                    return
                self.idx = {name: i for i, name in enumerate(header)}
                cutoff = time.time() - self.hours * 3600
                for line in f:
                    parts = line.rstrip("\n").split(",", len(header) - 1)
                    if len(parts) != len(header):
                        continue
                    try:
                        parts[0] = float(parts[0])
                    except ValueError:
                        continue
                    if parts[0] >= cutoff:
                        self.rows.append(parts)
        except PermissionError:
            self.error = "permission"
            return
        except OSError as exc:
            self.error = str(exc)
            return
        # A gap in the timeline IS the freeze: the recorder writes every 5 s, so
        # more than a minute of silence means the machine was not running.
        for a, b in zip(self.rows, self.rows[1:]):
            if b[0] - a[0] > 60:
                self.gaps.append((a[0], b[0]))
        self.alarms = [r for r in self.rows if r[self.idx["level"]] != "OK"]

    def column(self, name):
        i = self.idx.get(name)
        if i is None:
            return []
        out = []
        for r in self.rows:
            try:
                out.append(float(r[i]))
            except ValueError:
                out.append(0.0)
        return out

    def times(self):
        return [r[0] for r in self.rows]

    def rows_before(self, ts, count=8, window=120):
        return [r for r in self.rows if ts - window <= r[0] <= ts][-count:]


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------
class LivePage(Gtk.Box):
    def __init__(self, on_kill, on_status, ncpu):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.on_status = on_status
        self._svc_hold = {}
        self._kmsg_ok = True

        # --- memory / swap -------------------------------------------------
        mem_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.ram_caption = small("RAM")
        self.ram_meter = Meter(marker=0.85, marker_tip=t("mem.marker"))
        self.swap_caption = small("Swap")
        self.swap_meter = Meter()
        mem_box.pack_start(self.ram_caption, False, False, 0)
        mem_box.pack_start(self.ram_meter, False, False, 0)
        mem_box.pack_start(small(""), False, False, 0)
        mem_box.pack_start(self.swap_caption, False, False, 0)
        mem_box.pack_start(self.swap_meter, False, False, 0)
        mem_box.pack_end(small(t("mem.thresholds"), "fg-muted", wrap=46), False, False, 0)

        # --- headline numbers ----------------------------------------------
        # 2x2 rather than 1x4: four tiles in a row would set a minimum width the
        # window then could not go under.
        tiles = Gtk.Grid(column_spacing=18, row_spacing=8, column_homogeneous=True)
        self.tiles = {}
        for i, (key, title, hint) in enumerate((
            ("psi", t("tile.psi"), t("tile.psi_hint")),
            ("swapout", t("tile.swapout"), t("tile.swapout_hint")),
            ("stalls", t("tile.stalls"), t("tile.stalls_hint")),
            ("load", t("tile.load"), t("tile.load_hint", cpus=ncpu)),
        )):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            value = Gtk.Label(label="–", xalign=0.0)
            value.get_style_context().add_class("fg-value")
            value.set_ellipsize(Pango.EllipsizeMode.END)
            sub = small(hint, "fg-muted", ellipsize=True)
            col.pack_start(small(title, "fg-card-title", ellipsize=True), False, False, 0)
            col.pack_start(value, False, False, 0)
            col.pack_start(sub, False, False, 0)
            tiles.attach(col, i % 2, i // 2, 1, 1)
            self.tiles[key] = (value, sub)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, homogeneous=True)
        top.pack_start(card(t("card.memory"), mem_box), True, True, 0)
        top.pack_start(card(t("card.state"), tiles), True, True, 0)
        self.pack_start(top, False, False, 0)

        # --- charts ---------------------------------------------------------
        self.psi_chart = TimeChart(
            [(t("chart.some10"), 0), (t("chart.full10"), 1), (t("chart.full60"), 2)],
            unit="%", min_ymax=10, threshold=20, threshold_label=t("chart.threshold"))
        self.swap_chart = TimeChart([(t("chart.swapout"), 0)], unit="kB/s",
                                    min_ymax=1000, digits=0)
        self.cpu_chart = TimeChart([("us", 0), ("sy", 1), ("wa", 2), ("si", 3)],
                                   unit="%", min_ymax=100, digits=0)
        charts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        charts.pack_start(card(t("card.psi"), self.psi_chart), True, True, 0)
        charts.pack_start(card(t("card.swapout"), self.swap_chart), True, True, 0)
        charts.pack_start(card(t("card.cpu"), self.cpu_chart), True, True, 0)

        # --- process lists with stop buttons --------------------------------
        self.mem_table = ProcTable(
            [(t("col.process"), 0.0, True), (t("col.rss"), 1.0, False),
             (t("col.cpu"), 1.0, False)], 5, on_kill)
        self.cpu_table = ProcTable(
            [(t("col.process"), 0.0, True), (t("col.cpu"), 1.0, False),
             (t("col.rss"), 1.0, False)], 3, on_kill)

        # --- protection switches --------------------------------------------
        self.services = {}
        svc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for unit, label_key in UNITS:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            icon = Gtk.Label(label="–")
            name = small(t(label_key) if label_key.startswith("svc.") else label_key,
                         "fg-ink")
            state = small("", "fg-sub")
            switch = Gtk.Switch(valign=Gtk.Align.CENTER)
            switch.set_tooltip_text(t("svc.tip"))
            handler = switch.connect("state-set", self._on_switch, unit)
            row.pack_start(icon, False, False, 0)
            row.pack_start(name, False, False, 0)
            row.pack_end(switch, False, False, 0)
            row.pack_end(state, False, False, 0)
            svc_box.pack_start(row, False, False, 0)
            self.services[unit] = (icon, state, switch, handler)
        # A card that only says "not installed" is a dead end, so the button
        # takes over the missing step instead of staying grey.
        self.svc_hint = small("", "fg-muted", wrap=44)
        svc_box.pack_start(self.svc_hint, False, False, 0)
        self.enable_all = Gtk.Button(label=t("svc.enable_all"))
        self.enable_all.connect("clicked", self._on_action)
        svc_box.pack_start(self.enable_all, False, False, 4)
        self._install_mode = False
        self._installing = False

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right.set_size_request(390, -1)
        right.pack_start(card(t("card.top_mem"), self.mem_table), True, True, 0)
        right.pack_start(card(t("card.top_cpu"), self.cpu_table), True, True, 0)
        right.pack_start(card(t("card.services"), svc_box, expand=False), False, False, 0)

        middle = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        middle.pack_start(charts, True, True, 0)
        middle.pack_start(right, False, False, 0)
        self.pack_start(middle, True, True, 0)

    # -- updates ------------------------------------------------------------
    def update(self, s):
        avail = s["mem_avail_pct"]
        used_kb = s["mem_total_kb"] - s["mem_avail_kb"]
        ram_status = ("good" if avail >= 25 else "warning" if avail >= 15
                      else "serious" if avail >= 10 else "critical")
        self.ram_caption.set_text(t("mem.ram", used=gb(used_kb),
                                    total=gb(s["mem_total_kb"]), pct=num(avail, 0)))
        self.ram_meter.set(1 - avail / 100.0, ram_status)

        zram = [x for x in M.read_swaps() if "zram" in x[0]]
        if s["swap_total_kb"]:
            used = s["swap_used_pct"]
            swap_status = ("good" if used < 40 else "warning" if used < 70
                           else "serious" if used < 90 else "critical")
            if not zram and swap_status == "good":
                swap_status = "serious"
            self.swap_caption.set_text(t(
                "mem.swap", used=gb(s["swap_used_kb"]), total=gb(s["swap_total_kb"]),
                zram=(t("mem.zram", size=gb(zram[0][2])) if zram else t("mem.no_zram"))))
            self.swap_meter.set(used / 100.0, swap_status)
        else:
            self.swap_caption.set_text(t("mem.swap_none"))
            self.swap_meter.set(0.0, "critical")

        self._tile("psi", num(s["mem_full60"], 1) + " %",
                   t("tile.psi_sub", f10=num(s["mem_full10"], 1),
                     some10=num(s["mem_some10"], 1)))
        self._tile("swapout", rate_text(s["swapout_kbs"]),
                   t("tile.swapout_sub", rate=rate_text(s["swapin_kbs"]),
                     majflt=num(s["majflt_s"], 0)))
        if self._kmsg_ok:
            self._tile("stalls", str(s["kmsg_total"]),
                       t("tile.stalls_new", n=s["kmsg_new"]) if s["kmsg_new"]
                       else t("tile.stalls_hint"))
        self._tile("load", num(s["load1"], 2),
                   t("tile.load_sub", io=num(s["io_full10"], 1), wa=num(s["cpu_wa"], 0)))

        ts = s["t"]
        self.psi_chart.push(ts, (s["mem_some10"], s["mem_full10"], s["mem_full60"]))
        self.swap_chart.push(ts, (s["swapout_kbs"],))
        self.cpu_chart.push(ts, (s["cpu_us"], s["cpu_sy"], s["cpu_wa"], s["cpu_si"]))

        self.mem_table.fill([
            (comm, f"{rss // 1024} MB", f"{num(pct, 0)} %",
             Pango.Weight.BOLD if pct >= 90 else Pango.Weight.NORMAL, int(pid), comm)
            for rss, pct, comm, pid in s["by_rss"][:5]
        ])
        self.cpu_table.fill([
            (comm + ("  " + t("kill.runaway") if pct >= 90 else ""),
             f"{num(pct, 0)} %", f"{rss // 1024} MB",
             Pango.Weight.BOLD if pct >= 90 else Pango.Weight.NORMAL, int(pid), comm)
            for rss, pct, comm, pid in s["by_cpu"][:3] if pct > 0
        ])

        self._update_services()

    def _tile(self, key, value, sub):
        value_lbl, sub_lbl = self.tiles[key]
        value_lbl.set_text(value)
        sub_lbl.set_text(sub)
        sub_lbl.set_tooltip_text(sub)  # the caption may be shortened

    def _update_services(self):
        now = time.time()
        missing = 0
        for unit, _label in UNITS:
            icon, state, switch, handler = self.services[unit]
            if now - self._svc_hold.get(unit, 0) < 15:
                continue  # a switch is being acted on - do not fight the user
            if not unit_installed(unit):
                missing += 1
                self._pill(icon, state, "warning", "–", t("svc.missing"))
                switch.set_sensitive(False)
                switch.set_tooltip_text(t("svc.missing_tip", cmd=INSTALL_CMD))
                self._set_switch(switch, handler, False)
            else:
                switch.set_sensitive(True)
                switch.set_tooltip_text(t("svc.tip"))
                active = bool(M.service_active(unit))
                self._pill(icon, state, "good" if active else "critical",
                           "✓" if active else "✕",
                           t("svc.active") if active else t("svc.stopped"))
                self._set_switch(switch, handler, active)
        self._refresh_action(missing)

    def _refresh_action(self, missing):
        """Nothing installed -> the button installs. Otherwise it enables what
        is there. Either way it stays usable."""
        if self._installing:
            return
        gone = [u for u, _l in UNITS if not unit_installed(u)]
        if len(gone) == len(UNITS) and os.path.exists(INSTALL_SH):
            self._install_mode = True
            self.enable_all.set_label(t("svc.install"))
            self.enable_all.set_sensitive(True)
            self.enable_all.set_tooltip_text(INSTALL_CMD)
            self.svc_hint.set_text(t("svc.hint_none"))
        else:
            self._install_mode = False
            self.enable_all.set_label(t("svc.enable_all"))
            self.enable_all.set_sensitive(len(gone) < len(UNITS))
            self.enable_all.set_tooltip_text(t("svc.tip"))
            self.svc_hint.set_text(t("svc.hint_some", units=", ".join(gone))
                                   if gone else "")
        self.svc_hint.set_visible(bool(self.svc_hint.get_text()))

    @staticmethod
    def _set_switch(switch, handler, active):
        """Reflect reality without triggering the handler - otherwise every
        sample would look like the user had just flipped the switch."""
        if switch.get_active() == active:
            return
        switch.handler_block(handler)
        switch.set_active(active)
        switch.handler_unblock(handler)

    @staticmethod
    def _pill(icon, state, status, glyph, text):
        icon.set_text(glyph)
        sc = icon.get_style_context()
        for cls in ("good", "warning", "serious", "critical"):
            sc.remove_class(f"fg-icon-{cls}")
        sc.add_class(f"fg-icon-{status}")
        state.set_text(text)

    def _on_switch(self, _switch, wanted, unit):
        if not unit_installed(unit):
            self.on_status(t("svc.hint_missing", cmd=INSTALL_CMD))
            return True
        self._svc_hold[unit] = time.time()
        self.services[unit][1].set_text(t("svc.working"))
        systemctl("enable" if wanted else "disable", [unit],
                  lambda err, u=unit: self._switch_done(u, err))
        return False

    def _on_action(self, _btn):
        if self._install_mode:
            self._install()
        else:
            self._on_enable_all(_btn)

    def _install(self):
        """Runs install.sh through pkexec. It installs packages and writes units,
        so it needs root - the desktop asks for the password, we only wait."""
        if not os.path.exists(INSTALL_SH):
            self.on_status(t("svc.install_missing", path=INSTALL_SH))
            return
        self._installing = True
        self.enable_all.set_sensitive(False)
        self.svc_hint.set_text(t("svc.installing"))
        self.svc_hint.set_visible(True)

        def work():
            try:
                res = subprocess.run(["pkexec", INSTALL_SH], capture_output=True,
                                     text=True, timeout=900)
                if res.returncode == 0:
                    err = ""
                elif res.stderr.strip():
                    err = res.stderr.strip().splitlines()[-1]
                else:
                    err = f"exit {res.returncode}"
            except (OSError, subprocess.SubprocessError) as exc:
                err = str(exc)
            GLib.idle_add(done, err)

        def done(err):
            self._installing = False
            self.on_status(t("svc.install_failed", err=err) if err
                           else t("svc.install_done"))
            self._update_services()
            return False

        threading.Thread(target=work, daemon=True).start()

    def _on_enable_all(self, _btn):
        units = [u for u, _l in UNITS if unit_installed(u)]
        if not units:
            self.on_status(t("svc.hint_missing", cmd=INSTALL_CMD))
            return
        for u in units:
            self._svc_hold[u] = time.time()
            self.services[u][1].set_text(t("svc.working"))
        systemctl("enable", units, lambda err: self._switch_done(", ".join(units), err))

    def _switch_done(self, unit, err):
        self._svc_hold.clear()
        if err:
            self.on_status(t("svc.failed", unit=unit, err=err))
        self._update_services()

    def set_kmsg_unavailable(self):
        self._kmsg_ok = False
        self._tile("stalls", "–", t("tile.stalls_denied"))


class HistoryPage(Gtk.Box):
    """Post-mortem. Without the recorder there is nothing to show, so this page
    says so plainly instead of drawing an empty chart."""

    RANGES = ((6, "hist.hours6"), (24, "hist.hours24"), (48, "hist.hours48"),
              (168, "hist.days7"), (720, "hist.days30"))

    def __init__(self, on_copy, path):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.on_copy = on_copy
        self.path = path

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.pack_start(small(t("hist.range"), "fg-sub"), False, False, 0)
        self.range_combo = Gtk.ComboBoxText()
        for _h, key in self.RANGES:
            self.range_combo.append_text(t(key))
        self.range_combo.set_active(2)
        self.range_combo.connect("changed", lambda *_a: self.reload())
        bar.pack_start(self.range_combo, False, False, 0)
        reload_btn = Gtk.Button(label=t("hist.reload"))
        reload_btn.connect("clicked", lambda *_a: self.reload())
        bar.pack_start(reload_btn, False, False, 0)
        self.summary = small("", "fg-sub")
        bar.pack_end(self.summary, False, False, 0)
        self.pack_start(bar, False, False, 0)

        self.pct_chart = TimeChart(
            [(t("hist.legend_free"), 0), (t("hist.legend_psi"), 1)], unit="%",
            min_ymax=100, x_mode="time", capacity=100000, height=120)
        self.swap_chart = TimeChart(
            [(t("chart.swapout"), 0)], unit="kB/s", min_ymax=1000, digits=0,
            x_mode="time", capacity=100000, height=100)
        self.charts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.charts.pack_start(card(t("card.record"), self.pct_chart), True, True, 0)
        self.charts.pack_start(card(t("card.swapout"), self.swap_chart), True, True, 0)

        self.store = Gtk.TreeStore(str, str, str, str, str, str, str, int)
        self.view = Gtk.TreeView(model=self.store)
        self.view.get_style_context().add_class("fg-ink")
        for i, (title, xalign, expand) in enumerate((
            (t("col.time"), 0.0, False), (t("col.level"), 0.0, False),
            (t("col.mem_free"), 1.0, False), (t("col.psi_full"), 1.0, False),
            (t("col.swapout"), 1.0, False), (t("col.wa_sy"), 1.0, False),
            (t("col.top"), 0.0, True),
        )):
            renderer = Gtk.CellRendererText(xalign=xalign)
            col = Gtk.TreeViewColumn(title, renderer, text=i, weight=7)
            col.set_alignment(xalign)
            col.set_expand(expand)
            self.view.append_column(col)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(150)
        scroller.add(self.view)
        self.table_card = card(t("card.outages"), scroller)
        self.missing = self._missing_box()

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.pack_start(self.body, True, True, 0)
        self.reload()

    def _missing_box(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.missing_title = Gtk.Label(xalign=0.0)
        self.missing_title.get_style_context().add_class("fg-ink")
        self.missing_text = Gtk.Label(xalign=0.0)
        self.missing_text.set_line_wrap(True)
        self.missing_text.get_style_context().add_class("fg-sub")
        btn = Gtk.Button(label=t("hist.copy"))
        btn.connect("clicked", lambda *_a: self.on_copy(INSTALL_CMD))
        btn.set_halign(Gtk.Align.START)
        box.pack_start(self.missing_title, False, False, 0)
        box.pack_start(self.missing_text, False, False, 0)
        box.pack_start(btn, False, False, 0)
        return card(t("card.no_record"), box, expand=False)

    def reload(self):
        hours = self.RANGES[max(0, self.range_combo.get_active())][0]
        h = History(self.path, hours)
        for child in self.body.get_children():
            self.body.remove(child)

        if h.error or not h.rows:
            if h.error == "missing":
                self.missing_title.set_text(t("hist.missing_title", path=self.path))
                self.missing_text.set_text(t("hist.missing_body", cmd=INSTALL_CMD))
            elif h.error == "permission":
                self.missing_title.set_text(t("hist.denied_title", path=self.path))
                self.missing_text.set_text(t("hist.denied_body", path=self.path))
            else:
                self.missing_title.set_text(t("hist.empty_title", path=self.path))
                self.missing_text.set_text(t("hist.empty_body", err=h.error or "—"))
            self.summary.set_text("")
            self.body.pack_start(self.missing, False, False, 0)
            self.body.show_all()
            return

        times = h.times()
        self.pct_chart.set_data(
            times, [h.column("mem_avail_pct"), h.column("mem_full10")], h.gaps)
        self.swap_chart.set_data(times, [h.column("swapout_kbs")], h.gaps)

        outages = (t("hist.outage_one") if len(h.gaps) == 1
                   else t("hist.outage_many", n=len(h.gaps)))
        self.summary.set_text(t(
            "hist.summary", rows=len(h.rows), alarms=len(h.alarms), outages=outages,
            span=f"{self._ts(times[0])} – {self._ts(times[-1])}"))

        self.store.clear()
        expand = []
        idx = h.idx
        for t0, t1 in h.gaps:
            parent = self.store.append(None, [
                t("hist.outage", start=self._ts(t0)),
                t("hist.outage_dead", min=num((t1 - t0) / 60, 0)),
                "", "", "", "", t("hist.outage_until", end=self._ts(t1)),
                Pango.Weight.BOLD])
            for r in h.rows_before(t0):
                self.store.append(parent, self._row(r, idx))
            expand.append(self.store.get_path(parent))
        if h.alarms:
            parent = self.store.append(None, [
                t("hist.alarms"), t("hist.alarms_count", n=len(h.alarms)),
                "", "", "", "", t("hist.alarms_last"), Pango.Weight.BOLD])
            for r in h.alarms[-20:]:
                self.store.append(parent, self._row(r, idx))
        if not h.gaps and not h.alarms:
            self.store.append(None, [t("hist.calm"), "", "", "", "", "",
                                     t("hist.calm_sub"), Pango.Weight.NORMAL])

        self.body.pack_start(self.charts, True, True, 0)
        self.body.pack_start(self.table_card, True, True, 0)
        self.body.show_all()
        for path in expand:
            self.view.expand_row(path, False)

    def _row(self, r, idx):
        dec = (lambda v: v.replace(".", ",")) if LANG == "de" else (lambda v: v)
        level = r[idx["level"]]
        return [
            self._ts(r[0], with_date=False),
            t(CSV_LEVEL_KEY[level]) if level in CSV_LEVEL_KEY else level,
            dec(r[idx["mem_avail_pct"]]) + " %",
            dec(r[idx["mem_full10"]]) + " %",
            r[idx["swapout_kbs"]] + " kB/s",
            f"{r[idx['cpu_wa']]}/{r[idx['cpu_sy']]}",
            r[idx["top"]].strip('"'),
            Pango.Weight.NORMAL,
        ]

    @staticmethod
    def _ts(ts, with_date=True):
        return time.strftime("%d.%m. %H:%M:%S" if with_date else "%H:%M:%S",
                             time.localtime(ts))


class LogPage(Gtk.Box):
    """journalctl output, fetched in a thread - the journal can take a moment
    and the main loop must keep painting the live numbers."""

    QUERIES = (
        ("log.sec_guard",
         ["journalctl", "-t", "freeze-guard", "--no-pager", "-n", "40"], None),
        ("log.sec_earlyoom",
         ["journalctl", "-u", "earlyoom", "--no-pager", "--since", "30 days ago"],
         ("sending sig", "killed", "victim")),
        ("log.sec_oom",
         ["journalctl", "-k", "--no-pager", "--since", "30 days ago"],
         ("out of memory: killed",)),
        ("log.sec_stalls",
         ["journalctl", "-k", "--no-pager", "--since", "7 days ago"],
         ("blk_mq_run_work_fn hogged", "hung_task", "soft lockup")),
    )

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn = Gtk.Button(label=t("log.refresh"))
        btn.connect("clicked", lambda *_a: self.refresh())
        bar.pack_start(btn, False, False, 0)
        self.status = small("", "fg-sub")
        bar.pack_start(self.status, False, False, 0)
        self.pack_start(bar, False, False, 0)

        self.view = Gtk.TextView(editable=False, cursor_visible=False)
        self.view.set_monospace(True)
        self.view.set_left_margin(8)
        self.view.set_right_margin(8)
        self.buffer = self.view.get_buffer()
        self.buffer.create_tag("head", weight=Pango.Weight.BOLD,
                               pixels_above_lines=10, pixels_below_lines=4)
        self.buffer.create_tag("dim", style=Pango.Style.ITALIC)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.view)
        self.pack_start(card(t("card.log"), scroller), True, True, 0)
        self.loaded = False
        self._busy = False

    def refresh(self):
        if self._busy:
            return
        self._busy = True
        self.loaded = True
        self.status.set_text(t("log.reading"))
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        sections = []
        for key, cmd, needles in self.QUERIES:
            try:
                out = subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=25).stdout
            except (OSError, subprocess.SubprocessError) as exc:
                sections.append((t(key), t("log.unavailable", err=exc)))
                continue
            if needles:
                lines = [ln for ln in out.splitlines()
                         if any(n in ln.lower() for n in needles)]
                out = "\n".join(lines[-25:])
            else:
                out = "\n".join(out.splitlines()[-40:])
            sections.append((t(key), out.strip() or t("log.none")))
        GLib.idle_add(self._render, sections)

    def _render(self, sections):
        self.buffer.set_text("")
        end = self.buffer.get_end_iter
        for title, body in sections:
            self.buffer.insert_with_tags_by_name(end(), f"── {title}\n", "head")
            if body.startswith("("):
                self.buffer.insert_with_tags_by_name(end(), body + "\n", "dim")
            else:
                self.buffer.insert(end(), body + "\n")
        self.status.set_text(t("log.state", time=time.strftime("%H:%M:%S")))
        self._busy = False
        return False


class SimPage(Gtk.Box):
    """What the guard would kill right now. Same scoring as the daemon, run on
    the live sample - a prediction. The stop button in the last column is not a
    prediction; it acts."""

    def __init__(self, on_kill):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.auto = Gtk.CheckButton(label=t("sim.auto"))
        self.auto.set_active(True)
        bar.pack_start(self.auto, False, False, 0)
        self.target = Gtk.Label(xalign=1.0, label="—")
        self.target.get_style_context().add_class("fg-ink")
        self.target.set_ellipsize(Pango.EllipsizeMode.END)
        bar.pack_end(self.target, False, False, 0)
        self.pack_start(bar, False, False, 0)

        self.table = ProcTable(
            [(t("col.process"), 0.0, True), (t("col.pid"), 1.0, False),
             (t("col.rss"), 1.0, False), (t("col.cpu"), 1.0, False),
             (t("col.points"), 1.0, False), (t("col.category"), 0.0, False)],
            13, on_kill)
        self.pack_start(card(t("card.victims"), self.table), True, True, 0)
        self.pack_start(small(t("sim.hint"), "fg-hint", wrap=90), False, False, 0)

    def update(self, s):
        if not self.auto.get_active():
            return
        rows, target = rank_victims(s.get("by_rss_all", s["by_rss"]),
                                    s.get("suspects", set()))
        self.table.fill([
            (comm, str(pid), f"{rss // 1024} MB", f"{num(pct, 0)} %",
             num(score / 1024, 0) if weight else "—", tier,
             Pango.Weight.BOLD if target and pid == target[0] else Pango.Weight.NORMAL,
             int(pid), comm)
            for pid, comm, rss, pct, weight, score, tier in rows[:15]
        ])
        if target:
            self.target.set_text(t("sim.target", pid=target[0], comm=target[1],
                                   mb=target[2] // 1024))
        else:
            self.target.set_text(t("sim.no_target"))


class SettingsPage(Gtk.Box):
    INTERVALS = (1.0, 2.0, 5.0, 10.0, 30.0)
    COOLDOWNS = (60.0, 300.0, 900.0, 3600.0)
    LANGS = (("en", "English"), ("de", "Deutsch"))
    LEVELS = ((YELLOW, "level.raised"), (ORANGE, "level.warning"),
              (RED, "level.critical"))

    def __init__(self, settings, on_change, tray_available):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.settings = settings
        self.on_change = on_change
        self._loading = True

        grid = Gtk.Grid(row_spacing=10, column_spacing=16)
        row = 0

        self.lang_combo = Gtk.ComboBoxText()
        for _code, name in self.LANGS:
            self.lang_combo.append_text(name)
        self.lang_combo.set_active([c for c, _n in self.LANGS].index(settings["lang"]))
        self.lang_combo.connect("changed", self._on_lang)
        row = self._add(grid, row, t("set.language"), self.lang_combo)

        self.interval_combo = Gtk.ComboBoxText()
        for sec in self.INTERVALS:
            self.interval_combo.append_text(t("set.seconds", n=num(sec, 0)))
        self.interval_combo.set_active(
            self.INTERVALS.index(settings["interval"])
            if settings["interval"] in self.INTERVALS else 2)
        self.interval_combo.connect("changed", self._on_interval)
        row = self._add(grid, row, t("set.interval"), self.interval_combo,
                        t("set.interval_hint"))

        self.tray_switch = Gtk.Switch(halign=Gtk.Align.START)
        self.tray_switch.set_active(bool(settings["start_in_tray"]) and tray_available)
        self.tray_switch.set_sensitive(tray_available)
        self.tray_switch.connect("state-set", self._on_bool, "start_in_tray")
        row = self._add(grid, row, t("set.tray"), self.tray_switch,
                        t("set.tray_hint") if tray_available else t("set.tray_missing"))

        self.auto_switch = Gtk.Switch(halign=Gtk.Align.START)
        self.auto_switch.set_active(bool(settings["auto_open"]))
        self.auto_switch.connect("state-set", self._on_bool, "auto_open")
        row = self._add(grid, row, t("set.autoopen"), self.auto_switch,
                        t("set.autoopen_hint"))

        levels = [lv for lv, _k in self.LEVELS]
        self.level_combo = Gtk.ComboBoxText()
        for _lv, key in self.LEVELS:
            self.level_combo.append_text(t(key))
        self.level_combo.set_active(levels.index(settings["auto_open_level"])
                                    if settings["auto_open_level"] in levels else 1)
        self.level_combo.connect("changed", self._on_level)
        row = self._add(grid, row, t("set.autoopen_level"), self.level_combo)

        self.notify_switch = Gtk.Switch(halign=Gtk.Align.START)
        self.notify_switch.set_active(bool(settings["notify"]))
        self.notify_switch.connect("state-set", self._on_bool, "notify")
        row = self._add(grid, row, t("set.notify"), self.notify_switch)

        self.notify_level_combo = Gtk.ComboBoxText()
        for _lv, key in self.LEVELS:
            self.notify_level_combo.append_text(t(key))
        self.notify_level_combo.set_active(
            levels.index(settings["notify_level"])
            if settings["notify_level"] in levels else 2)
        self.notify_level_combo.connect("changed", self._on_notify_level)
        row = self._add(grid, row, t("set.notify_level"), self.notify_level_combo,
                        t("set.notify_level_hint"))

        self.cooldown_combo = Gtk.ComboBoxText()
        for seconds in self.COOLDOWNS:
            self.cooldown_combo.append_text(t("set.minutes", n=num(seconds / 60, 0)))
        self.cooldown_combo.set_active(
            self.COOLDOWNS.index(settings["notify_cooldown"])
            if settings["notify_cooldown"] in self.COOLDOWNS else 1)
        self.cooldown_combo.connect("changed", self._on_cooldown)
        row = self._add(grid, row, t("set.notify_cooldown"), self.cooldown_combo,
                        t("set.notify_cooldown_hint"))

        self.panel_switch = Gtk.Switch(halign=Gtk.Align.START)
        self.panel_switch.set_active(bool(settings["panel_label"]))
        self.panel_switch.set_sensitive(tray_available)
        self.panel_switch.connect("state-set", self._on_bool, "panel_label")
        row = self._add(grid, row, t("set.panel_label"), self.panel_switch)

        self.autostart_switch = Gtk.Switch(halign=Gtk.Align.START)
        self.autostart_switch.set_active(Settings.autostart_enabled())
        self.autostart_switch.connect("state-set", self._on_autostart)
        row = self._add(grid, row, t("set.autostart"), self.autostart_switch,
                        t("set.autostart_hint", path=AUTOSTART_FILE))

        row = self._add(grid, row, t("set.record_file"),
                        small(settings["history_file"], "fg-sub"))

        self.pack_start(card(t("page.settings"), grid, expand=False), False, False, 0)
        self.status = small("", "fg-muted")
        self.pack_start(self.status, False, False, 0)
        self._loading = False

    @staticmethod
    def _add(grid, row, label, widget, hint=None):
        grid.attach(small(label, "fg-ink"), 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)
        if hint:
            grid.attach(small(hint, "fg-muted", wrap=64), 2, row, 1, 1)
        return row + 1

    def _saved(self):
        self.status.set_text(t("set.saved", path=CONFIG_FILE))

    def _on_lang(self, combo):
        if self._loading:
            return
        code = self.LANGS[max(0, combo.get_active())][0]
        self.settings.set("lang", code)
        self.on_change("lang", code)

    def _on_interval(self, combo):
        if self._loading:
            return
        value = self.INTERVALS[max(0, combo.get_active())]
        self.settings.set("interval", value)
        self.on_change("interval", value)
        self._saved()

    def _on_notify_level(self, combo):
        if self._loading:
            return
        self.settings.set("notify_level", self.LEVELS[max(0, combo.get_active())][0])
        self._saved()

    def _on_cooldown(self, combo):
        if self._loading:
            return
        self.settings.set("notify_cooldown",
                          self.COOLDOWNS[max(0, combo.get_active())])
        self._saved()

    def _on_level(self, combo):
        if self._loading:
            return
        self.settings.set("auto_open_level", self.LEVELS[max(0, combo.get_active())][0])
        self._saved()

    def _on_bool(self, _switch, state, key):
        if self._loading:
            return False
        self.settings.set(key, bool(state))
        self.on_change(key, bool(state))
        self._saved()
        return False

    def _on_autostart(self, _switch, state):
        # Compare against reality too: this handler writes a file outside our own
        # config, so a spurious emission has to stay a no-op. A program that
        # quietly enters itself into the user's autostart is exactly the kind of
        # surprise that gets a tool uninstalled.
        if self._loading or bool(state) == Settings.autostart_enabled():
            return False
        Settings.set_autostart(bool(state))
        self._saved()
        return False


# ---------------------------------------------------------------------------
# tray
# ---------------------------------------------------------------------------
class Tray:
    """Status icon with a menu. Three backends in order of fitness:

      XApp          native on Cinnamon/Mint, renders icon *and* label
      AppIndicator  GNOME/KDE/Unity
      Gtk.StatusIcon last resort, deprecated but still carried by most panels

    If none of them comes up, `available` stays False and the window must not
    start hidden - an invisible program with no way back is worse than a
    visible one.
    """

    ICONS = {"good": "utilities-system-monitor", "warning": "dialog-warning",
             "serious": "dialog-warning", "critical": "dialog-error"}

    def __init__(self, on_show, on_console, on_settings, on_quit):
        self.on_show = on_show
        self.on_console = on_console
        self.on_settings = on_settings
        self.on_quit = on_quit
        self.available = False
        self.backend = None
        self.xapp = None
        self.indicator = None
        self.status_icon = None
        self._level_item = None
        self.menu = None
        self._build_menu()
        self._build_icon()

    def _icon(self, status):
        name = self.ICONS.get(status, "utilities-system-monitor")
        return name if Gtk.IconTheme.get_default().has_icon(name) \
            else "utilities-system-monitor"

    def _build_menu(self):
        self.menu = Gtk.Menu()
        self._level_item = Gtk.MenuItem(label="…")
        self._level_item.set_sensitive(False)
        self.menu.append(self._level_item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.show_item = Gtk.MenuItem(label=t("menu.show"))
        self.show_item.connect("activate", lambda *_a: self.on_show())
        self.menu.append(self.show_item)
        for label, callback in ((t("menu.console"), self.on_console),
                                (t("menu.settings"), self.on_settings)):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda *_a, cb=callback: cb())
            self.menu.append(item)
        self.menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label=t("menu.quit"))
        quit_item.connect("activate", lambda *_a: self.on_quit())
        self.menu.append(quit_item)
        self.menu.show_all()

    def _build_icon(self):
        if XApp is not None:
            try:
                self.xapp = XApp.StatusIcon()
                self.xapp.set_name("freeze-guard")
                self.xapp.set_icon_name(self._icon("good"))
                self.xapp.set_tooltip_text(t("app.title"))
                self.xapp.set_secondary_menu(self.menu)   # right click
                self.xapp.connect("activate", lambda *_a: self.on_show())
                self.backend = "xapp"
                self.available = True
                return
            except Exception:  # pragma: no cover - no XApp support at runtime
                self.xapp = None
        if AppIndicator is not None:
            try:
                self.indicator = AppIndicator.Indicator.new(
                    "freeze-guard", self._icon("good"),
                    AppIndicator.IndicatorCategory.SYSTEM_SERVICES)
                self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
                self.indicator.set_menu(self.menu)
                self.indicator.set_title(t("app.title"))
                self.indicator.set_secondary_activate_target(self.show_item)
                self.backend = "appindicator"
                self.available = True
                return
            except Exception:  # pragma: no cover - indicator backend missing
                self.indicator = None
        try:
            self.status_icon = Gtk.StatusIcon.new_from_icon_name(self._icon("good"))
            self.status_icon.set_tooltip_text(t("app.title"))
            self.status_icon.connect("activate", lambda *_a: self.on_show())
            self.status_icon.connect(
                "popup-menu",
                lambda icon, button, ts: self.menu.popup(None, None, None, None,
                                                         button, ts))
            self.backend = "statusicon"
            self.available = True
        except Exception:  # pragma: no cover
            self.status_icon = None

    def update(self, level_text, status, label):
        tip = t("tray.level", level=level_text)
        if self._level_item is not None:
            self._level_item.set_label(tip)
        icon = self._icon(status)
        if self.xapp is not None:
            self.xapp.set_icon_name(icon)
            self.xapp.set_label(label or "")
            self.xapp.set_tooltip_text(tip)
        elif self.indicator is not None:
            self.indicator.set_icon_full(icon, level_text)
            self.indicator.set_label(label or "", "")
        elif self.status_icon is not None:
            self.status_icon.set_from_icon_name(icon)
            self.status_icon.set_tooltip_text(tip)

    def rebuild(self):
        """Language changed - menu labels are baked in, so build a new menu."""
        self._build_menu()
        if self.xapp is not None:
            self.xapp.set_secondary_menu(self.menu)
        elif self.indicator is not None:
            self.indicator.set_menu(self.menu)
            self.indicator.set_secondary_activate_target(self.show_item)


# ---------------------------------------------------------------------------
# window
# ---------------------------------------------------------------------------
class Window(Gtk.Window):
    PAGES = ("live", "history", "log", "sim", "settings")

    def __init__(self, settings, page="live", guard=None):
        super().__init__(title=t("app.title"))
        self.settings = settings
        self.guard = guard          # released on quit, see quit()
        self.control = None         # answers a second start, see main()
        self.set_default_size(1200, 820)
        self.set_icon_name("utilities-system-monitor")
        apply_theme(self)

        self.ncpu = os.cpu_count() or 1
        self._level = None
        self._notify_id = 0          # replace this bubble instead of adding one
        self._notified_level = GREEN  # highest level announced this episode
        # The reason is kept as key+args, never as rendered text: on a language
        # switch the banner has to be re-rendered, and a stored sentence would
        # stay in the old language.
        self._reason_source = {"reason": "", "reason_i18n": None}
        self._last_notify = 0.0
        self._auto_armed = True     # hysteresis for the automatic pop-up
        self._start_page = self.PAGES.index(page)
        self._quitting = False
        self._status_timer = None

        self.header = Gtk.HeaderBar(show_close_button=True)
        self.set_titlebar(self.header)
        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.root.get_style_context().add_class("fg-plane")
        self.root.set_border_width(10)
        self.add(self.root)

        self.tray = Tray(self.present_window, self.open_console,
                         lambda: self.show_page("settings"), self.quit)
        self._build_ui()

        self.poller = Poller(self.on_sample, interval=settings["interval"])
        if self.poller.sampler.kmsg.fd is None:
            self.live.set_kmsg_unavailable()
        self.poller.start()

        gtk_settings = Gtk.Settings.get_default()
        if gtk_settings is not None:
            gtk_settings.connect("notify::gtk-theme-name", lambda *_a: self._retheme())
            gtk_settings.connect("notify::gtk-application-prefer-dark-theme",
                                 lambda *_a: self._retheme())
        # Closing keeps the guard watching from the tray; only Quit ends the
        # process. Without a tray, closing really does quit.
        self.connect("delete-event", self._on_delete)

    # -- construction -------------------------------------------------------
    def _build_ui(self):
        for child in self.header.get_children():
            child.destroy()
        for child in self.root.get_children():
            child.destroy()

        self.header.set_title(t("app.title"))
        self.header.set_subtitle(t("app.subtitle"))
        self.pause = Gtk.ToggleButton(label=t("hdr.pause"))
        self.pause.connect("toggled", self._on_pause)
        self.header.pack_start(self.pause)

        menu_btn = Gtk.MenuButton()
        menu_btn.add(Gtk.Image.new_from_icon_name("open-menu-symbolic",
                                                  Gtk.IconSize.BUTTON))
        menu = Gtk.Menu()
        item = Gtk.MenuItem(label=t("menu.settings"))
        item.connect("activate", lambda *_a: self.show_page("settings"))
        menu.append(item)
        lang_item = Gtk.MenuItem(label=t("menu.language"))
        lang_menu = Gtk.Menu()
        group = None
        for code, name in SettingsPage.LANGS:
            radio = Gtk.RadioMenuItem(label=name)
            if group is None:
                group = radio
            else:
                radio.join_group(group)
            radio.set_active(code == LANG)
            radio.connect("toggled", self._on_lang_menu, code)
            lang_menu.append(radio)
        lang_item.set_submenu(lang_menu)
        menu.append(lang_item)
        item = Gtk.MenuItem(label=t("menu.console"))
        item.connect("activate", lambda *_a: self.open_console())
        menu.append(item)
        if self.tray.available:
            item = Gtk.MenuItem(label=t("menu.tray"))
            item.connect("activate", lambda *_a: self.hide())
            menu.append(item)
        item = Gtk.MenuItem(label=t("menu.copy_install"))
        item.connect("activate", lambda *_a: self.copy(INSTALL_CMD))
        menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        item = Gtk.MenuItem(label=t("menu.quit"))
        item.connect("activate", lambda *_a: self.quit())
        menu.append(item)
        menu.show_all()
        menu_btn.set_popup(menu)
        self.header.pack_end(menu_btn)

        self.banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.banner.get_style_context().add_class("fg-banner")
        self.banner_icon = Gtk.Label(label="…")
        self.banner_level = Gtk.Label(label="…")
        self.banner_level.get_style_context().add_class("fg-badge")
        self.banner_reason = Gtk.Label(xalign=0.0, label="")
        self.banner_reason.get_style_context().add_class("fg-ink")
        self.banner_reason.set_ellipsize(Pango.EllipsizeMode.END)
        self.banner_status = small("", "fg-sub", xalign=1.0)
        self.banner_time = small("", "fg-sub")
        self.banner.pack_start(self.banner_icon, False, False, 0)
        self.banner.pack_start(self.banner_level, False, False, 0)
        self.banner.pack_start(self.banner_reason, True, True, 0)
        self.banner.pack_end(self.banner_time, False, False, 0)
        self.banner.pack_end(self.banner_status, False, False, 8)
        self.root.pack_start(self.banner, False, False, 0)

        self.live = LivePage(self.kill, self.status, self.ncpu)
        self.history = HistoryPage(self.copy, self.settings["history_file"])
        self.log = LogPage()
        self.sim = SimPage(self.kill)
        self.settings_page = SettingsPage(self.settings, self.on_setting,
                                          self.tray.available)
        self.notebook = Gtk.Notebook()
        for child, key in ((self.live, "page.live"), (self.history, "page.history"),
                           (self.log, "page.log"), (self.sim, "page.sim"),
                           (self.settings_page, "page.settings")):
            holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            holder.get_style_context().add_class("fg-plane")
            holder.set_border_width(10)
            holder.pack_start(child, True, True, 0)
            self.notebook.append_page(holder, Gtk.Label(label=t(key)))
        self.notebook.connect("switch-page", self._on_switch_page)
        self.root.pack_start(self.notebook, True, True, 0)
        self.root.show_all()
        self.header.show_all()
        if self._start_page:
            GLib.idle_add(self.notebook.set_current_page, self._start_page)

    def _rebuild_for_language(self):
        self._start_page = self.notebook.get_current_page()
        self._build_ui()
        self.tray.rebuild()
        if self._level is not None:
            self._paint_level(self._level, reason_text(self._reason_source))

    # -- plumbing -----------------------------------------------------------
    def _retheme(self):
        apply_theme(self)
        self.queue_draw()

    def _on_pause(self, btn):
        self.poller.set_paused(btn.get_active())
        self.banner_time.set_text(t("hdr.paused") if btn.get_active() else "")

    def _on_switch_page(self, _nb, _page, index):
        if index == 2 and not self.log.loaded:
            self.log.refresh()

    def _on_lang_menu(self, item, code):
        if item.get_active() and code != LANG:
            self.settings.set("lang", code)
            self.on_setting("lang", code)

    def on_setting(self, key, value):
        global LANG
        if key == "lang":
            LANG = value
            self._rebuild_for_language()
        elif key == "interval":
            self.poller.interval = value

    def show_page(self, name):
        self.present_window()
        self.notebook.set_current_page(self.PAGES.index(name))

    def present_window(self):
        self.show_all()
        self.deiconify()
        self.present_with_time(int(time.time()))

    def _on_delete(self, *_a):
        if self.tray.available and not self._quitting:
            self.hide()
            # Closing a window and expecting the program to be gone is the normal
            # expectation; say once that it is not, otherwise the notifications
            # that keep arriving afterwards look like they come from nowhere.
            if not self.settings["tray_hint_shown"]:
                self.settings.set("tray_hint_shown", True)
                self._hint_notification(t("tray.hint_title"), t("tray.hint_body"))
            return True  # keep watching from the tray
        self.quit()
        return False

    @staticmethod
    def _hint_notification(title, body):
        """Low urgency and transient: a hint must never join the alert stack."""
        try:
            subprocess.Popen(
                ["notify-send", "-a", t("app.title"), "-u", "low", "-e",
                 "-t", "10000", "-i", NOTIFY_ICON, title, body],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def quit(self):
        self._quitting = True
        self.poller.stop()
        if self.control is not None:
            self.control.stop()
        if self.guard is not None:
            self.guard.release()
        app = self.get_application()
        if app is not None:
            app.quit()
        else:
            Gtk.main_quit()

    def copy(self, text):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        self.status(text)

    def status(self, text):
        """Transient message next to the clock, cleared after 10 s so a stale
        'SIGTERM sent' never masquerades as the current state."""
        self.banner_status.set_text(text)
        if self._status_timer:
            GLib.source_remove(self._status_timer)
        self._status_timer = GLib.timeout_add_seconds(10, self._clear_status)

    def _clear_status(self):
        self.banner_status.set_text("")
        self._status_timer = None
        return False

    def kill(self, pid, comm):
        kill_process(pid, comm, self.status)

    def open_console(self):
        target = os.path.join(APP_DIR, "run-monitor.py")
        if not os.path.exists(target):
            target = "fg-monitor"
        for term, arg in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"),
                          ("xfce4-terminal", "-x"), ("xterm", "-e")):
            if GLib.find_program_in_path(term):
                try:
                    subprocess.Popen([term, arg, target], start_new_session=True)
                    return
                except OSError:
                    continue
        self.status(t("err.no_terminal"))

    # -- samples ------------------------------------------------------------
    def on_sample(self, s):
        level = s["level"]
        self._reason_source = {"reason": s.get("reason", ""),
                               "reason_i18n": s.get("reason_i18n")}
        self._paint_level(level, reason_text(s))
        if not self.pause.get_active():
            self.banner_time.set_text(time.strftime("%H:%M:%S", time.localtime(s["t"])))

        self.live.update(s)
        if self.notebook.get_current_page() == 3:
            self.sim.update(s)

        self.tray.update(
            t(LEVEL_KEY[level]), LEVEL_STATUS[level],
            f"{num(s['mem_avail_pct'], 0)} %" if self.settings["panel_label"] else "")

        threshold = self.settings["auto_open_level"]
        if level < threshold:
            self._auto_armed = True
        elif self.settings["auto_open"] and self._auto_armed and not self.get_visible():
            # Once per incident: re-arming only after the level has dropped
            # again keeps a flapping PSI value from stealing focus every tick.
            self._auto_armed = False
            self.present_window()

        self._update_notification(level, reason_text(s))
        self._level = level
        return False

    # -- notifications ------------------------------------------------------
    def _update_notification(self, level, reason):
        """At most one visible bubble, and only when things get worse.

        The previous version announced every upward level change with urgency
        critical. A critical notification never expires, so a PSI value flapping
        between two levels produced a stack of bubbles nobody could clear. Now:
        one bubble that is replaced in place, sent only on escalation, rate
        limited, and closed again once the pressure is gone.
        """
        threshold = self.settings["notify_level"]
        if level < threshold:
            if self._notified_level >= threshold:
                self._close_notification()
            self._notified_level = GREEN
            return
        if not self.settings["notify"] or level <= self._notified_level:
            return
        if time.time() - self._last_notify < self.settings["notify_cooldown"]:
            return
        self._last_notify = time.time()
        self._notified_level = level
        self._send_notification(t(LEVEL_KEY[level]), reason)

    def _send_notification(self, level, reason):
        """notify-send in a thread; -p reports the id we reuse with -r."""
        previous = self._notify_id

        def work():
            args = ["notify-send", "-a", t("app.title"), "-u", "normal",
                    "-t", str(NOTIFY_TIMEOUT_MS), "-i", NOTIFY_ICON, "-p"]
            if previous:
                args += ["-r", str(previous)]
            args += [f"{t('app.title')}: {level}", reason]
            try:
                out = subprocess.run(args, capture_output=True, text=True,
                                     timeout=10).stdout
                GLib.idle_add(self._remember_notification, int(out.strip() or 0))
            except (OSError, subprocess.SubprocessError, ValueError):
                pass

        threading.Thread(target=work, daemon=True).start()

    def _remember_notification(self, notify_id):
        self._notify_id = notify_id
        if notify_id:
            GLib.timeout_add_seconds(NOTIFY_VISIBLE_SECONDS, self._auto_close,
                                     notify_id)
        return False

    def _auto_close(self, notify_id):
        """Take the bubble away after its time is up.

        Necessary because the expire hint is advisory and this desktop ignores it.
        Only closes the bubble it was scheduled for, so a newer one survives.
        """
        if self._notify_id == notify_id:
            self._close_notification()
        return False

    def _close_notification(self):
        """Takes the bubble away when the reason for it is gone."""
        notify_id, self._notify_id = self._notify_id, 0
        if not notify_id or not GLib.find_program_in_path("gdbus"):
            return

        def work():
            try:
                subprocess.run(
                    ["gdbus", "call", "--session",
                     "--dest", "org.freedesktop.Notifications",
                     "--object-path", "/org/freedesktop/Notifications",
                     "--method", "org.freedesktop.Notifications.CloseNotification",
                     str(notify_id)],
                    capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass

        threading.Thread(target=work, daemon=True).start()

    def _paint_level(self, level, reason):
        self.banner_icon.set_text(LEVEL_ICON[level])
        self.banner_level.set_text(t(LEVEL_KEY[level]))
        self.banner_reason.set_text(reason)
        status = LEVEL_STATUS[level]
        for widget, prefix in ((self.banner, "fg-banner-"),
                               (self.banner_icon, "fg-icon-")):
            sc = widget.get_style_context()
            for cls in ("good", "warning", "serious", "critical"):
                sc.remove_class(prefix + cls)
            sc.add_class(prefix + status)


class App(Gtk.Application):
    """Hosts the window.

    Deliberately NON_UNIQUE: uniqueness is enforced by the lock file in
    fg_instance, the same way the other tools in ~/Applications do it. Two
    mechanisms guarding the same thing means two answers when they disagree.
    """

    def __init__(self, settings, page, show_window, guard):
        super().__init__(application_id="de.local.FreezeGuard",
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.settings = settings
        self.page = page
        self.show_window = show_window
        self.guard = guard
        self.win = None

    def do_activate(self):
        if self.win is None:
            self.win = Window(self.settings, page=self.page, guard=self.guard)
            self.add_window(self.win)
            # Keep running while the window is hidden in the tray: without the
            # hold, GApplication would consider itself done.
            self.hold()
            # A second start asks through the control socket; answer it on the
            # main loop, never from the socket thread.
            self.win.control = fg_instance.InstanceControlServer(
                lambda: GLib.idle_add(self.win.present_window))
            self.win.control.start()
            # Never start invisible without a tray: that would be a program
            # with no way back.
            if self.show_window or not self.win.tray.available:
                self.win.show_all()
        else:
            self.win.present_window()


def report_already_running(focused_existing, owner_pid):
    """Tell the user why nothing opened.

    A second start that exits without a word looks like a broken launcher, so it
    says what happened - and if the running instance could not be reached, how to
    get rid of it.
    """
    if focused_existing:
        text, kind = t("instance.focused"), Gtk.MessageType.INFO
    elif owner_pid:
        text, kind = t("instance.blocked", pid=owner_pid), Gtk.MessageType.WARNING
    else:
        text, kind = t("instance.blocked_nopid"), Gtk.MessageType.WARNING
    print(f"{t('instance.title')}: {text}", file=sys.stderr)
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    dialog = Gtk.MessageDialog(modal=True, message_type=kind,
                               buttons=Gtk.ButtonsType.OK,
                               text=t("instance.title"), secondary_text=text)
    dialog.set_icon_name(NOTIFY_ICON)
    dialog.run()
    dialog.destroy()


def main():
    global LANG
    set_process_name("fg-gui")
    settings = Settings()
    ap = argparse.ArgumentParser(
        prog="run.py",
        description="Graphical dashboard for the freeze protection "
                    "(console variant: freeze-guard monitor)")
    ap.add_argument("--interval", type=float, default=None,
                    help="sample interval in seconds (default from the settings)")
    ap.add_argument("--file", default=None, help="recording for the History page")
    ap.add_argument("--lang", choices=sorted(STR), default=None,
                    help="en or de (default from the settings)")
    ap.add_argument("--page", choices=Window.PAGES, default="live",
                    help="page to start on")
    ap.add_argument("--window", action="store_true",
                    help="start with the window visible, ignoring the tray setting")
    ap.add_argument("--quiet", action="store_true",
                    help="when another instance already runs, exit silently "
                         "(used by the autostart entry)")
    args = ap.parse_args()

    if args.interval:
        settings.data["interval"] = min(60.0, max(1.0, args.interval))
    if args.file:
        settings.data["history_file"] = args.file
    if args.lang:
        settings.data["lang"] = args.lang
    LANG = settings["lang"]

    if not os.path.exists("/proc/pressure/memory"):
        sys.exit("PSI (/proc/pressure/memory) is not available - kernel too old?")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        sys.exit("No graphical session (DISPLAY is empty).\n"
                 "In a terminal:  freeze-guard monitor")

    may_continue, guard = fg_instance.enforce_single_instance(
        quiet=args.quiet, on_blocked=report_already_running)
    if not may_continue:
        sys.exit(1)

    show = args.window or not settings["start_in_tray"]
    app = App(settings, args.page, show, guard)
    try:
        # argv is not forwarded: our options are parsed above, GApplication would
        # only reject them.
        sys.exit(app.run([]))
    finally:
        guard.release()


if __name__ == "__main__":
    main()
