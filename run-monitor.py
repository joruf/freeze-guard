#!/usr/bin/env python3
"""run-monitor.py - live dashboard and black-box recorder for freezes.

Installed as the command `fg-monitor`.

Two modes:

  fg-monitor            live curses dashboard, refreshes once per second
  fg-monitor --log      headless recorder, appends a CSV row every 5 s

Why the recorder matters: the freezes on this VM leave no trace. sysstat/sar is
not installed, so after a hard reset nobody can tell what the machine was doing
in the three minutes it was dying. The recorder fsyncs whenever the alert level
leaves green, so the evidence survives a hard reset.

Everything is read from /proc - no external processes are spawned per tick, and
both loops sleep between samples (no busy-waiting).

Early-warning signals, in the order they historically light up:
  1. swap-out rate > 0            pages start leaving RAM
  2. major fault rate rises       the machine is fetching pages back from disk
  3. PSI memory some avg10 rises  tasks begin waiting on memory
  4. PSI memory full avg10 rises  ALL tasks stall - the death spiral
  5. blk_mq kernel messages       the block layer burns CPU (this is what makes
                                  the host fan scream right before the freeze)
"""

import argparse
import curses
import os
import sys
import time
from collections import deque

HZ = os.sysconf("SC_CLK_TCK")
PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024
HISTORY_FILE = "/var/log/freeze-guard-history.csv"
MAX_HISTORY_BYTES = 20 * 1024 * 1024  # rotate at 20 MB

GREEN, YELLOW, ORANGE, RED = 0, 1, 2, 3
LEVEL_NAME = {GREEN: "OK", YELLOW: "ERHOEHT", ORANGE: "WARNUNG", RED: "KRITISCH"}
SPARK = " ▁▂▃▄▅▆▇█"


# --------------------------------------------------------------------------
# /proc readers
# --------------------------------------------------------------------------
def read_meminfo():
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            out[k] = int(v.split()[0])  # kB
    return out


def read_psi(resource):
    """Returns {'some_avg10':.., 'some_avg60':.., 'full_avg10':.., 'full_avg60':..}"""
    out = {}
    try:
        with open(f"/proc/pressure/{resource}") as f:
            for line in f:
                parts = line.split()
                kind = parts[0]  # some | full
                for p in parts[1:]:
                    if "=" in p:
                        k, v = p.split("=", 1)
                        if k in ("avg10", "avg60"):
                            out[f"{kind}_{k}"] = float(v)
    except OSError:
        pass
    return out


def read_vmstat():
    out = {}
    try:
        with open("/proc/vmstat") as f:
            for line in f:
                k, _, v = line.partition(" ")
                out[k] = int(v)
    except OSError:
        pass
    return out


def read_cpu_times():
    with open("/proc/stat") as f:
        parts = f.readline().split()
    vals = [int(x) for x in parts[1:]]
    # user nice system idle iowait irq softirq steal
    return vals[:8]


def read_loadavg():
    with open("/proc/loadavg") as f:
        p = f.read().split()
    return float(p[0]), p[3]


def read_swaps():
    """[(name, type, size_kb, used_kb, prio)]"""
    out = []
    try:
        with open("/proc/swaps") as f:
            next(f, None)
            for line in f:
                p = line.split()
                if len(p) >= 5:
                    out.append((p[0], p[1], int(p[2]), int(p[3]), int(p[4])))
    except OSError:
        pass
    return out


def scan_processes(prev=None, interval=1.0):
    """Every process with its RSS and its CPU% over the last interval.

    Returns (by_rss, by_cpu, ticks). Ranking by memory alone would hide a busy
    loop that holds only a few MB - which is exactly the runaway case we need to
    catch - so both orderings are produced.
    """
    procs, cur = [], {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                data = f.read()
            # comm may contain spaces and parens - split after the LAST ')'
            rest = data[data.rindex(")") + 2:].split()
            utime, stime = int(rest[11]), int(rest[12])
            rss_kb = int(rest[21]) * PAGE_KB
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
        except (OSError, ValueError, IndexError):
            continue
        ticks = utime + stime
        cur[pid] = ticks
        pct = 0.0
        if prev and pid in prev and interval > 0:
            pct = max(0.0, (ticks - prev[pid]) * 100.0 / (interval * HZ))
        if rss_kb <= 0 and pct <= 0:
            continue
        procs.append((rss_kb, pct, comm, pid))

    by_rss = sorted(procs, key=lambda p: -p[0])
    by_cpu = sorted(procs, key=lambda p: -p[1])
    return by_rss, by_cpu, cur


def service_active(name):
    """systemd state without spawning a process every tick."""
    try:
        with open(f"/sys/fs/cgroup/system.slice/{name}.service/cgroup.procs") as f:
            return bool(f.read().strip())
    except OSError:
        return None  # unknown / not installed


class KmsgWatcher:
    """Counts block-layer stall messages - the signal that preceded every
    freeze on this machine by about three minutes."""

    PATTERNS = (b"blk_mq_run_work_fn", b"hung_task", b"soft lockup")

    def __init__(self):
        self.fd = None
        self.count = 0
        try:
            self.fd = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
            os.lseek(self.fd, 0, os.SEEK_END)
        except OSError:
            self.fd = None

    def poll(self):
        """Returns how many new matching messages appeared since last poll."""
        if self.fd is None:
            return 0
        new = 0
        for _ in range(200):  # bounded: never spin on a flood
            try:
                buf = os.read(self.fd, 8192)
            except (BlockingIOError, OSError):
                break
            if not buf:
                break
            if any(p in buf for p in self.PATTERNS):
                new += 1
        self.count += new
        return new


# --------------------------------------------------------------------------
# sampling
# --------------------------------------------------------------------------
class Sampler:
    def __init__(self):
        self.prev_vmstat = read_vmstat()
        self.prev_cpu = read_cpu_times()
        self.prev_procs = None
        self.prev_t = time.monotonic()
        self.kmsg = KmsgWatcher()
        self.hist = {k: deque(maxlen=60) for k in
                     ("mem_some10", "mem_full10", "mem_full60", "io_full10", "swapout")}

    def sample(self, want_procs=True):
        now = time.monotonic()
        dt = max(1e-6, now - self.prev_t)
        self.prev_t = now

        mi = read_meminfo()
        psi_mem = read_psi("memory")
        psi_io = read_psi("io")
        psi_cpu = read_psi("cpu")
        vm = read_vmstat()
        cpu = read_cpu_times()
        load1, _ = read_loadavg()

        # rates
        def rate(key):
            return max(0, vm.get(key, 0) - self.prev_vmstat.get(key, 0)) / dt

        swapin = rate("pswpin") * PAGE_KB      # kB/s
        swapout = rate("pswpout") * PAGE_KB
        majflt = rate("pgmajfault")
        self.prev_vmstat = vm

        # cpu percentages
        d = [c - p for c, p in zip(cpu, self.prev_cpu)]
        self.prev_cpu = cpu
        tot = sum(d) or 1
        cpu_pct = {
            "us": 100.0 * (d[0] + d[1]) / tot,
            "sy": 100.0 * d[2] / tot,
            "id": 100.0 * d[3] / tot,
            "wa": 100.0 * d[4] / tot,
            "si": 100.0 * (d[5] + d[6]) / tot,
        }

        by_rss, by_cpu = [], []
        if want_procs:
            by_rss, by_cpu, self.prev_procs = scan_processes(self.prev_procs, dt)

        mem_total = mi.get("MemTotal", 1)
        mem_avail = mi.get("MemAvailable", 0)
        swap_total = mi.get("SwapTotal", 0)
        swap_free = mi.get("SwapFree", 0)

        s = {
            "t": time.time(),
            "mem_total_kb": mem_total,
            "mem_avail_kb": mem_avail,
            "mem_avail_pct": 100.0 * mem_avail / mem_total,
            "swap_total_kb": swap_total,
            "swap_used_kb": swap_total - swap_free,
            "swap_used_pct": (100.0 * (swap_total - swap_free) / swap_total) if swap_total else 0.0,
            "dirty_kb": mi.get("Dirty", 0),
            "mem_some10": psi_mem.get("some_avg10", 0.0),
            "mem_some60": psi_mem.get("some_avg60", 0.0),
            "mem_full10": psi_mem.get("full_avg10", 0.0),
            "mem_full60": psi_mem.get("full_avg60", 0.0),
            "io_full10": psi_io.get("full_avg10", 0.0),
            "cpu_some10": psi_cpu.get("some_avg10", 0.0),
            "swapin_kbs": swapin,
            "swapout_kbs": swapout,
            "majflt_s": majflt,
            "load1": load1,
            "kmsg_new": self.kmsg.poll(),
            "kmsg_total": self.kmsg.count,
            "by_rss": by_rss[:8],
            "by_cpu": by_cpu[:5],
            # Untruncated, for consumers that must not rank on a partial view:
            # run.py's victim simulation mirrors the daemon, and the daemon
            # scans every process. Same list objects, so this costs nothing.
            "by_rss_all": by_rss,
            "by_cpu_all": by_cpu,
            "runaways": [p for p in by_cpu[:5] if p[1] >= 90],
        }
        s.update({f"cpu_{k}": v for k, v in cpu_pct.items()})
        s["level"], s["reason"], s["reason_i18n"] = alert_level(s)

        for k, key in (("mem_some10", "mem_some10"), ("mem_full10", "mem_full10"),
                       ("mem_full60", "mem_full60"), ("io_full10", "io_full10")):
            self.hist[k].append(s[key])
        self.hist["swapout"].append(swapout)
        return s


def alert_level(s):
    """Maps a sample onto a traffic light. Ordered so the earliest signal that
    still has time to act wins.

    Returns (level, german_text, (i18n_key, format_args)). The key exists so
    run.py can show the same reason in its own language instead of trying to
    recognise these sentences by their wording.
    """
    # RED: this is the guard's own kill condition
    if s["mem_full60"] >= 20 and s["mem_avail_pct"] < 15:
        return (RED, "Stall-Bedingung erfüllt - der Guard greift gleich ein",
                ("reason.stall", {}))
    if s["mem_full10"] >= 30:
        return RED, "Alle Tasks blockieren auf Speicher", ("reason.all_blocked", {})
    if s["kmsg_new"] > 0:
        return (RED, "Kernel meldet Block-Layer-Stalls (Vorbote des Freeze)",
                ("reason.kernel_stalls", {}))

    if s["mem_full10"] >= 5:
        return (ORANGE, "Totalblockaden auf Speicher haben begonnen",
                ("reason.full_started", {}))
    if s["mem_avail_pct"] < 12:
        return (ORANGE, "Weniger als 12% Speicher verfügbar",
                ("reason.below12", {}))
    if s["swapout_kbs"] > 5000:
        return (ORANGE, "Starkes Auslagern - Thrashing-Gefahr",
                ("reason.thrashing", {}))

    if s["mem_some10"] >= 10:
        return YELLOW, "Tasks warten spürbar auf Speicher", ("reason.waiting", {})
    if s["mem_avail_pct"] < 25:
        return YELLOW, "Speicher wird knapp", ("reason.tight", {})
    if s["swapout_kbs"] > 0 or s["majflt_s"] > 50:
        return YELLOW, "Auslagerung hat eingesetzt", ("reason.swapping", {})
    if s.get("runaways"):
        # A busy loop alone does not freeze Linux, but it is worth seeing - and
        # it becomes the prime suspect the moment memory gets tight.
        r = s["runaways"][0]
        return (YELLOW, f"Dauerlast: {r[2]} bei {r[1]:.0f}% CPU",
                ("reason.runaway", {"comm": r[2], "pct": f"{r[1]:.0f}"}))

    return GREEN, "unauffällig", ("reason.calm", {})


# --------------------------------------------------------------------------
# recorder
# --------------------------------------------------------------------------
CSV_COLUMNS = [
    "t", "level", "mem_avail_pct", "swap_used_pct", "mem_some10", "mem_full10",
    "mem_full60", "io_full10", "swapin_kbs", "swapout_kbs", "majflt_s",
    "cpu_us", "cpu_sy", "cpu_wa", "cpu_si", "load1", "kmsg_total", "top",
]


def rotate_if_needed(path):
    try:
        if os.path.getsize(path) > MAX_HISTORY_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass


def run_recorder(interval, path):
    sampler = Sampler()
    rotate_if_needed(path)
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    f = open(path, "a", buffering=1)
    if new_file:
        f.write(",".join(CSV_COLUMNS) + "\n")

    last_rotate_check = time.monotonic()
    try:
        while True:
            s = sampler.sample(want_procs=True)
            # Record both dimensions: the memory hog and the CPU burner are
            # often different processes, and after a hard reset this line is
            # the only evidence of which one was which.
            top = ("MEM " + " ".join(f"{c}:{r // 1024}M" for r, p, c, _ in s["by_rss"][:3])
                   + " | CPU " + " ".join(f"{c}:{p:.0f}%" for r, p, c, _ in s["by_cpu"][:3]))
            row = [
                f"{s['t']:.0f}", LEVEL_NAME[s["level"]],
                f"{s['mem_avail_pct']:.1f}", f"{s['swap_used_pct']:.1f}",
                f"{s['mem_some10']:.1f}", f"{s['mem_full10']:.1f}",
                f"{s['mem_full60']:.1f}", f"{s['io_full10']:.1f}",
                f"{s['swapin_kbs']:.0f}", f"{s['swapout_kbs']:.0f}",
                f"{s['majflt_s']:.0f}", f"{s['cpu_us']:.0f}", f"{s['cpu_sy']:.0f}",
                f"{s['cpu_wa']:.0f}", f"{s['cpu_si']:.0f}", f"{s['load1']:.2f}",
                str(s["kmsg_total"]), f'"{top}"',
            ]
            f.write(",".join(row) + "\n")

            # Green means the data can wait in the page cache. Anything else may
            # be the last line before a hard reset, so force it to disk.
            if s["level"] != GREEN:
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass

            if time.monotonic() - last_rotate_check > 600:
                last_rotate_check = time.monotonic()
                if os.path.getsize(path) > MAX_HISTORY_BYTES:
                    f.close()
                    rotate_if_needed(path)
                    f = open(path, "a", buffering=1)
                    f.write(",".join(CSV_COLUMNS) + "\n")

            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        f.close()


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------
def spark(values, width=24):
    if not values:
        return " " * width
    vals = list(values)[-width:]
    hi = max(max(vals), 1e-9)
    out = "".join(SPARK[min(len(SPARK) - 1, int(v / hi * (len(SPARK) - 1)))] for v in vals)
    return out.rjust(width)


def bar(frac, width, fill="█", empty="░"):
    frac = max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    return fill * n + empty * (width - n)


def human_gb(kb):
    return f"{kb / 1048576:.1f}"


def run_dashboard(stdscr, interval):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.use_default_colors()
    for i in range(1, 8):
        try:
            curses.init_pair(i, i, -1)
        except curses.error:
            pass
    C = {GREEN: curses.color_pair(2), YELLOW: curses.color_pair(3),
         ORANGE: curses.color_pair(1) | curses.A_BOLD, RED: curses.color_pair(1) | curses.A_BOLD}
    DIM, BOLD = curses.A_DIM, curses.A_BOLD

    sampler = Sampler()
    paused = False
    last_alarm = 0.0

    while True:
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q"), 27):
            return
        if ch in (ord("p"), ord("P")):
            paused = not paused

        if not paused:
            s = sampler.sample()

        stdscr.erase()
        h, w = stdscr.getmaxyx()
        y = 0

        def put(text, attr=0, indent=1):
            nonlocal y
            if y < h - 1:
                try:
                    stdscr.addnstr(y, indent, text, max(0, w - indent - 1), attr)
                except curses.error:
                    pass
                y += 1

        lvl = s["level"]
        badge = f"[ {LEVEL_NAME[lvl]} ]"
        hdr = f"FREEZE GUARD MONITOR{'  (PAUSE)' if paused else ''}"
        put(f"{hdr}{badge:>{max(1, w - len(hdr) - 3)}}", C[lvl] | BOLD)
        put(f"{time.strftime('%H:%M:%S')}   {s['reason']}", DIM)
        put("─" * (w - 2), DIM)

        # memory - kept to two lines so the process lists survive a 24-row term
        bw = max(8, min(24, w - 52))
        used_frac = 1 - s["mem_avail_pct"] / 100
        put(f"RAM   {bar(used_frac, bw)} "
            f"{human_gb(s['mem_total_kb'] - s['mem_avail_kb'])}/{human_gb(s['mem_total_kb'])} GB"
            f"   frei {s['mem_avail_pct']:.0f}%",
            C[GREEN] if s["mem_avail_pct"] > 25 else C[ORANGE])

        zram = [x for x in read_swaps() if "zram" in x[0]]
        ztxt = (f"zram {human_gb(zram[0][2])} GB" if zram else "zram AUS")
        if s["swap_total_kb"]:
            put(f"Swap  {bar(s['swap_used_pct'] / 100, bw)} "
                f"{human_gb(s['swap_used_kb'])}/{human_gb(s['swap_total_kb'])} GB"
                f"   belegt {s['swap_used_pct']:.0f}%   {ztxt}",
                C[GREEN] if (s["swap_used_pct"] < 40 and zram) else C[ORANGE])
        else:
            put(f"Swap  kein Swap aktiv   {ztxt}", C[ORANGE])
        y += 1

        # pressure + early warning, compact
        f60, f10, sm10 = s["mem_full60"], s["mem_full10"], s["mem_some10"]
        put(f"Druck  some10 {sm10:5.1f}%   full10 {f10:5.1f}%   "
            f"full60 {f60:5.1f}% (Kill ab 20)",
            C[GREEN] if f60 < 20 else C[RED])
        put(f"       full60 {spark(sampler.hist['mem_full60'], 28)}",
            C[GREEN] if f60 < 20 else C[RED])
        sw_attr = C[GREEN] if s["swapout_kbs"] == 0 else (
            C[YELLOW] if s["swapout_kbs"] < 5000 else C[RED])
        put(f"       Auslagern aus {s['swapout_kbs']:7.0f} kB/s  "
            f"{spark(sampler.hist['swapout'], 16)}", sw_attr)
        put(f"       io-full10 {s['io_full10']:5.1f}%   MajFaults {s['majflt_s']:5.0f}/s   "
            f"Kernel-Stalls {s['kmsg_total']}"
            + (f"  +{s['kmsg_new']} NEU!" if s["kmsg_new"] else ""),
            C[RED] if s["kmsg_new"] else
            (C[ORANGE] if (s["io_full10"] > 10 or s["majflt_s"] > 50) else C[GREEN]))
        put(f"CPU    us {s['cpu_us']:4.0f}%  sy {s['cpu_sy']:4.0f}%  "
            f"wa {s['cpu_wa']:4.0f}%  si {s['cpu_si']:4.0f}%   load {s['load1']:.2f}",
            C[ORANGE] if (s["cpu_wa"] > 20 or s["cpu_sy"] > 30) else 0)

        parts = []
        for svc, label in (("earlyoom", "earlyoom"), ("freeze-guard", "guard"),
                           ("freeze-guard-recorder", "rec")):
            parts.append(f"{label}: {'an' if service_active(svc) else 'AUS'}")
        allok = all(service_active(x) for x in ("earlyoom", "freeze-guard"))
        put("Schutz " + "  ".join(parts), C[GREEN] if allok else C[YELLOW])
        y += 1

        # Split whatever rows are left between the two lists instead of letting
        # them fall off the bottom on a short terminal. The CPU list keeps at
        # least two rows - a runaway must never be the thing that gets cut.
        free_rows = max(0, h - y - 3)
        n_cpu = min(4, max(2, free_rows // 3)) if free_rows >= 4 else 0
        n_rss = max(0, free_rows - n_cpu - 1)

        if n_rss:
            put("Größter Speicherverbrauch     RSS      CPU", BOLD)
            for rss_kb, pct, comm, pid in s["by_rss"][:n_rss]:
                put(f"  {comm[:20]:<20} {rss_kb // 1024:6d} MB  {pct:5.1f}%",
                    C[ORANGE] if pct > 90 else 0)
        if n_cpu:
            put("Höchste CPU-Last              RSS      CPU", BOLD)
            shown = 0
            for rss_kb, pct, comm, pid in s["by_cpu"]:
                if pct <= 0 or shown >= n_cpu:
                    break
                put(f"  {comm[:20]:<20} {rss_kb // 1024:6d} MB  {pct:5.1f}%"
                    + ("   <- DAUERLAST" if pct > 90 else ""),
                    C[ORANGE] | BOLD if pct > 90 else 0)
                shown += 1

        if y < h - 1:
            stdscr.addnstr(h - 1, 1, "[q] beenden   [p] pause", w - 2, DIM)

        # audible warning on red, at most every 15s
        if lvl == RED and time.time() - last_alarm > 15:
            last_alarm = time.time()
            curses.beep()

        stdscr.refresh()
        time.sleep(interval)


# --------------------------------------------------------------------------
def show_history(path, hours=48):
    """Post-mortem view. A gap in the timeline IS the freeze: the recorder
    writes every 5s, so anything longer than a minute means the machine was
    dead. The rows just before a gap are what killed it."""
    if not os.path.exists(path):
        print(f"Keine Aufzeichnung unter {path}.")
        print("Läuft der Recorder?   systemctl status freeze-guard-recorder")
        return

    rows = []
    with open(path) as f:
        header = f.readline().rstrip("\n").split(",")
        for line in f:
            parts = line.rstrip("\n").split(",", len(header) - 1)
            if len(parts) == len(header):
                try:
                    parts[0] = float(parts[0])
                except ValueError:
                    continue
                rows.append(parts)
    if not rows:
        print("Aufzeichnung ist noch leer - der Recorder läuft erst seit Kurzem.")
        return

    idx = {name: i for i, name in enumerate(header)}
    cutoff = time.time() - hours * 3600
    recent = [r for r in rows if r[0] >= cutoff]
    ts = lambda r: time.strftime("%d.%m. %H:%M:%S", time.localtime(r[0]))

    print(f"\n\033[1mAufzeichnung\033[0m  {len(rows)} Messungen, "
          f"{ts(rows[0])} bis {ts(rows[-1])}")

    # gaps = the machine stopped writing = freeze or reboot
    gaps = []
    for a, b in zip(recent, recent[1:]):
        if b[0] - a[0] > 60:
            gaps.append((a, b))

    if gaps:
        print(f"\n\033[1;31m{len(gaps)} Ausfall/Neustart in den letzten {hours}h\033[0m")
        for a, b in gaps:
            mins = (b[0] - a[0]) / 60
            print(f"\n  \033[1mAusfall {ts(a)} -> {ts(b)}  ({mins:.0f} min tot)\033[0m")
            print("  Die letzten Messungen davor:")
            tail = [r for r in recent if a[0] - 120 <= r[0] <= a[0]][-8:]
            print(f"    {'Zeit':<10} {'Stufe':<9} {'MemFrei':>7} {'PSIfull':>7} "
                  f"{'SwapAus':>8} {'wa':>3} {'sy':>3}  Top")
            for r in tail:
                print(f"    {time.strftime('%H:%M:%S', time.localtime(r[0])):<10} "
                      f"{r[idx['level']]:<9} {r[idx['mem_avail_pct']]:>6}% "
                      f"{r[idx['mem_full10']]:>6}% {r[idx['swapout_kbs']]:>7} "
                      f"{r[idx['cpu_wa']]:>3} {r[idx['cpu_si']]:>3}  "
                      f"{r[idx['top']].strip(chr(34))[:60]}")
    else:
        print(f"\n\033[1;32mKein Ausfall in den letzten {hours}h.\033[0m")

    alarms = [r for r in recent if r[idx["level"]] != "OK"]
    if alarms:
        print(f"\n\033[1;33m{len(alarms)} Messungen mit Alarmstufe über OK\033[0m "
              f"(letzte 10):")
        for r in alarms[-10:]:
            print(f"  {ts(r)}  {r[idx['level']]:<9} frei {r[idx['mem_avail_pct']]}%  "
                  f"PSIfull10 {r[idx['mem_full10']]}%  Swap-aus {r[idx['swapout_kbs']]} kB/s")
    else:
        print("\nKeine Messung über Stufe OK - der Speicherdruck blieb harmlos.")
    print()


def run_plain(interval, once=False):
    """Line-based fallback for anything without a usable terminal: IDE run
    buttons, file managers, pipes, cron. Same numbers, no curses."""
    sampler = Sampler()
    sampler.sample()          # prime the deltas
    time.sleep(min(1.0, interval))
    first = True
    try:
        while True:
            s = sampler.sample()
            zram = [x for x in read_swaps() if "zram" in x[0]]
            if first or not once:
                print()
            print(f"\033[1m{time.strftime('%H:%M:%S')}  [{LEVEL_NAME[s['level']]}]  "
                  f"{s['reason']}\033[0m")
            print(f"  RAM   {human_gb(s['mem_total_kb'] - s['mem_avail_kb'])}/"
                  f"{human_gb(s['mem_total_kb'])} GB   frei {s['mem_avail_pct']:.0f}%"
                  f"   Swap belegt {s['swap_used_pct']:.0f}%"
                  f"   zram {'aktiv' if zram else 'AUS'}")
            print(f"  Druck some10 {s['mem_some10']:.1f}%  full10 {s['mem_full10']:.1f}%  "
                  f"full60 {s['mem_full60']:.1f}% (Kill ab 20)  io-full10 {s['io_full10']:.1f}%")
            print(f"  Auslagern aus {s['swapout_kbs']:.0f} kB/s   "
                  f"MajFaults {s['majflt_s']:.0f}/s   Kernel-Stalls {s['kmsg_total']}"
                  + (f"  +{s['kmsg_new']} NEU!" if s["kmsg_new"] else ""))
            print(f"  CPU   us {s['cpu_us']:.0f}%  sy {s['cpu_sy']:.0f}%  "
                  f"wa {s['cpu_wa']:.0f}%  si {s['cpu_si']:.0f}%   load {s['load1']:.2f}")
            svc = "  ".join(f"{lbl}: {'an' if service_active(n) else 'AUS'}"
                            for n, lbl in (("earlyoom", "earlyoom"),
                                           ("freeze-guard", "guard"),
                                           ("freeze-guard-recorder", "rec")))
            print(f"  Schutz {svc}")
            print("  Speicher: " + ", ".join(
                f"{c} {r // 1024}MB" for r, p, c, _ in s["by_rss"][:4]))
            busy = [f"{c} {p:.0f}%" for r, p, c, _ in s["by_cpu"][:4] if p > 0]
            print("  CPU-Last: " + (", ".join(busy) if busy else "keine nennenswerte"))
            for r, p, c, _ in s["runaways"]:
                print(f"  \033[1;33m-> DAUERLAST: {c} bei {p:.0f}% CPU, "
                      f"{r // 1024} MB\033[0m")
            if once:
                return
            first = False
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


def terminal_usable():
    """curses needs both a real tty and a TERM entry it can look up."""
    if not sys.stdout.isatty():
        return False, "kein interaktives Terminal (Ausgabe umgeleitet oder aus einer IDE gestartet)"
    term = os.environ.get("TERM", "")
    if not term or term == "dumb":
        return False, f"TERM ist {'nicht gesetzt' if not term else term!r}"
    return True, ""


def set_process_name(name):
    """Show the tool in comm instead of "python3" - the guard's whitelist matches
    on comm, and a recorder that reads as "python3" is both unprotectable and
    unreadable in `ps`. comm is capped at 15 bytes by the kernel."""
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        buf = ctypes.create_string_buffer(name.encode()[:15])
        libc.prctl(15, ctypes.byref(buf), 0, 0, 0)  # 15 = PR_SET_NAME
    except Exception:  # pragma: no cover - cosmetic only, never fatal
        pass


def main():
    set_process_name("fg-monitor")
    ap = argparse.ArgumentParser(
        description="Live-Überwachung und Blackbox-Aufzeichnung für VM-Freezes")
    ap.add_argument("--log", action="store_true",
                    help="Aufzeichnungsmodus statt Dashboard")
    ap.add_argument("--history", action="store_true",
                    help="Aufgezeichnete Historie auswerten (Freeze-Forensik)")
    ap.add_argument("--hours", type=float, default=48,
                    help="Zeitraum für --history (Standard 48)")
    ap.add_argument("--plain", action="store_true",
                    help="Textausgabe statt Vollbild (für IDE-Terminals, Pipes, ssh)")
    ap.add_argument("--once", action="store_true",
                    help="Nur eine Momentaufnahme ausgeben und beenden")
    ap.add_argument("--interval", type=float, default=None,
                    help="Messintervall in Sekunden (Standard: 1 live, 5 im Log)")
    ap.add_argument("--file", default=HISTORY_FILE, help="Zieldatei im Log-Modus")
    args = ap.parse_args()

    if args.history:
        show_history(args.file, args.hours)
        return

    if not os.path.exists("/proc/pressure/memory"):
        sys.exit("PSI (/proc/pressure/memory) nicht verfügbar - Kernel zu alt?")

    if args.log:
        run_recorder(args.interval or 5.0, args.file)
        return

    if args.once:
        run_plain(args.interval or 1.0, once=True)
        return

    if args.plain:
        run_plain(args.interval or 2.0)
        return

    ok, why = terminal_usable()
    if not ok:
        print(f"Vollbild-Dashboard nicht möglich: {why}.")
        print("Weiter im Textmodus - mit Strg+C beenden.\n")
        run_plain(args.interval or 2.0)
        return

    try:
        curses.wrapper(run_dashboard, args.interval or 1.0)
    except KeyboardInterrupt:
        pass
    except curses.error as e:
        print(f"Terminal kommt mit dem Vollbild-Modus nicht zurecht ({e}).")
        print("Weiter im Textmodus - mit Strg+C beenden.\n")
        run_plain(args.interval or 2.0)


if __name__ == "__main__":
    main()
