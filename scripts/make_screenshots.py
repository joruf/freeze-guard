#!/usr/bin/env python3
"""Regenerates the README screenshots in docs/screenshots/.

Each page is captured from a freshly started dashboard, so the pictures always
show the shipped chrome rather than a hand-drawn mock-up. One page needs data
that does not exist on a healthy machine - a freeze - so the History page is fed
a synthetic recording; that file is written into a temporary directory and the
caption in the README says so.

Requirements: a running X session plus gnome-screenshot, xdotool, wmctrl and
Pillow. Without them the script explains what is missing and exits non-zero.

    scripts/make_screenshots.py            all pages, light theme
    scripts/make_screenshots.py --dark     the same in the dark palette
    scripts/make_screenshots.py live log   only these pages
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "screenshots"
GUI = ROOT / "run.py"
WINDOW_TITLE = "Freeze Guard"

# page -> (file name, seconds to let the charts fill, use the real defaults)
#
# The settings page is captured "pristine": with the demo recording and the
# one-second interval the picture would document temp paths and a value nobody
# ships. Every other page benefits from the faster interval.
PAGES = {
    "live": ("live", 40, False),
    "history": ("history", 4, False),
    "log": ("log", 6, False),
    "sim": ("simulation", 4, False),
    "settings": ("settings", 2, True),
}

TOOLS = ("gnome-screenshot", "xdotool", "wmctrl")


def check_environment():
    missing = [tool for tool in TOOLS if not shutil.which(tool)]
    try:
        import PIL  # noqa: F401
    except ImportError:
        missing.append("python3-pil")
    if not os.environ.get("DISPLAY"):
        missing.append("an X session (DISPLAY)")
    if missing:
        sys.exit("cannot take screenshots, missing: " + ", ".join(missing))


def demo_recording(path):
    """A synthetic recording with one outage, shaped like the real thing.

    The freezes on this machine predate the recorder, so no genuine file with a
    gap exists yet. The curve therefore reproduces the documented sequence:
    quiet, then memory drains while swap-out and PSI climb, then the machine
    stops writing for nine minutes, then a fresh boot. Clearly labelled as demo
    data in the README.
    """
    import math
    import random

    random.seed(11)
    columns = ("t,level,mem_avail_pct,swap_used_pct,mem_some10,mem_full10,mem_full60,"
               "io_full10,swapin_kbs,swapout_kbs,majflt_s,cpu_us,cpu_sy,cpu_wa,cpu_si,"
               "load1,kmsg_total,top")
    interval, calm, ramp, recovery, outage = 5, 240, 260, 150, 9 * 60 + 40
    now = time.time()
    total = (calm + ramp + recovery) * interval + outage
    stamp = now - total
    rows, kmsg = [], 0

    def level_for(avail, full10, swapout):
        if full10 >= 30:
            return "KRITISCH"
        if full10 >= 5:
            return "WARNUNG"
        if avail < 25 or swapout > 0:
            return "ERHOEHT"
        return "OK"

    def row(avail, swap_used, some10, full10, full60, io, swapin, swapout, majflt,
            us, sy, wa, si, load, top):
        return (f"{stamp:.0f},{level_for(avail, full10, swapout)},{avail:.1f},"
                f"{swap_used:.1f},{some10:.1f},{full10:.1f},{full60:.1f},{io:.1f},"
                f"{swapin:.0f},{swapout:.0f},{majflt:.0f},{us:.0f},{sy:.0f},{wa:.0f},"
                f"{si:.0f},{load:.2f},{kmsg},\"{top}\"")

    for i in range(calm):
        jitter = random.uniform(-2, 2)
        rows.append(row(41 + jitter, 12 + random.uniform(-1, 1),
                        random.uniform(0, 0.4), 0, 0.1, random.uniform(0, 0.5),
                        0, 0, random.randint(0, 6), 10 + random.uniform(-4, 8),
                        3, 0, 1, 1.6 + random.uniform(-0.4, 0.6),
                        "MEM code:2280M firefox:840M claude:430M | "
                        "CPU code:14% cinnamon:5% claude:4%"))
        stamp += interval
    for i in range(ramp):
        p = i / (ramp - 1)
        avail = 41 - 34 * p ** 1.4 + random.uniform(-0.8, 0.8)
        full10 = max(0.0, (p ** 3) * 58)
        swapout = int((p ** 2.2) * 39000)
        if p > 0.72:
            kmsg += random.randint(1, 11)
        rows.append(row(avail, min(100, 12 + 86 * p), min(70, 4 + 62 * p), full10,
                        min(80, 34 * p ** 1.5), min(90, 70 * p ** 2),
                        int(swapout * 0.35), swapout, int(60 + 900 * p),
                        int(34 + 26 * p), int(9 + 46 * p), int(4 + 34 * p),
                        int(1 + 17 * p), 3 + 23 * p,
                        "MEM code:3410M node:2870M firefox:1210M | "
                        "CPU kswapd0:97% node:118% code:38%"))
        stamp += interval
    stamp += outage
    kmsg = 0
    for i in range(recovery):
        rows.append(row(56 + random.uniform(-1.5, 1.5), random.uniform(0, 1.5),
                        random.uniform(0, 0.2), 0, 0.1, random.uniform(0, 0.3),
                        0, 0, random.randint(0, 3), 7 + random.uniform(-3, 6),
                        2, 0, 0, 0.9 + random.uniform(-0.3, 0.5),
                        "MEM code:910M firefox:520M | CPU code:11% cinnamon:4%"))
        stamp += interval

    with open(path, "w") as f:
        f.write(columns + "\n" + "\n".join(rows) + "\n")
    return path


def window_id():
    out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if WINDOW_TITLE in line:
            return line.split()[0]
    return None


def geometry(wid):
    out = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                         capture_output=True, text=True).stdout
    values = dict(re.findall(r"^(\w+)=(-?\d+)$", out, re.M))
    return (int(values["X"]), int(values["Y"]),
            int(values["WIDTH"]), int(values["HEIGHT"]))


def capture(wid, target):
    """Full-screen shot, then crop the window including its title bar."""
    from PIL import Image

    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        subprocess.run(["gnome-screenshot", "-f", tmp.name], check=True)
        x, y, width, height = geometry(wid)
        image = Image.open(tmp.name)
        box = (max(0, x - 2), max(0, y - 34),
               min(image.width, x + width + 2), min(image.height, y + height + 2))
        image.crop(box).save(target)
    return target


def shoot(page, history_file, dark, suffix):
    name, settle, pristine = PAGES[page]
    target = OUT_DIR / f"{name}{suffix}.png"
    env = dict(os.environ)
    if dark:
        env["GTK_THEME"] = "Mint-Y-Dark"
    command = [sys.executable, str(GUI), "--window", "--page", page]
    if pristine:
        # real defaults: shipped interval, real recording path, real config path
        command += ["--interval", "5"]
    else:
        env["XDG_CONFIG_HOME"] = str(Path(history_file).parent / "config")
        command += ["--interval", "1", "--file", history_file]
    proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)
    try:
        deadline = time.time() + 20
        wid = None
        while time.time() < deadline and not wid:
            time.sleep(0.5)
            wid = window_id()
        if not wid:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"the window never appeared: {output.strip()}")
        subprocess.run(["wmctrl", "-i", "-r", wid, "-b", "add,above"], check=False)
        subprocess.run(["xdotool", "windowraise", wid], check=False)
        print(f"    letting {page} settle for {settle} s")
        time.sleep(settle)
        capture(wid, target)
        subprocess.run(["wmctrl", "-i", "-r", wid, "-b", "remove,above"], check=False)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(1.0)   # let the single-instance name be released
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pages", nargs="*", choices=list(PAGES), default=list(PAGES),
                        help="pages to capture (default: all)")
    parser.add_argument("--dark", action="store_true",
                        help="capture with the dark palette")
    args = parser.parse_args()

    check_environment()
    if window_id():
        sys.exit("a Freeze Guard window is already open - close it first, otherwise "
                 "the single instance guard hands the screenshot to that one")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "-dark" if args.dark else ""
    with tempfile.TemporaryDirectory() as tmp:
        history = demo_recording(os.path.join(tmp, "demo-history.csv"))
        for page in args.pages:
            print(f"==> {page}")
            target = shoot(page, history, args.dark, suffix)
            print(f"    {target.relative_to(ROOT)}")
    print("\ndone. Screenshots contain real process names from this machine - "
          "check before publishing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
