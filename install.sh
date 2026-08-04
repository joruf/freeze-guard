#!/bin/bash
# Installs the layered freeze protection for this Linux Mint VM.
# Idempotent - safe to re-run. Uninstall: sudo freeze-guard uninstall
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen: sudo $0"; exit 1; }
[ -r /proc/pressure/memory ] || { echo "FEHLER: PSI nicht verfügbar - Kernel zu alt."; exit 1; }

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$1"; }

# --- 0) remove the old load-based watchdog ---------------------------------
say "Alten load-basierten Watchdog entfernen"
systemctl disable --now hang-watchdog.service 2>/dev/null || true
rm -f /etc/systemd/system/hang-watchdog.service /usr/local/bin/hang-watchdog.sh
echo "   erledigt"

# --- 1) packages ------------------------------------------------------------
say "Pakete installieren (earlyoom, zram-generator)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq earlyoom systemd-zram-generator

# --- 2) zram: compressed swap in RAM ---------------------------------------
# The freezes came from swapping to the VMware virtual disk: the block layer
# burned CPU (blk_mq_run_work_fn hogged CPU ... 1027 times) until everything
# stopped. zram compresses pages in RAM instead - no block I/O, no stall.
say "zram-Swap einrichten (max. 8 GB, zstd)"
cat > /etc/systemd/zram-generator.conf <<'EOF'
[zram0]
zram-size = min(ram / 2, 8192)
compression-algorithm = zstd
swap-priority = 100
fs-type = swap
EOF
systemctl daemon-reload
systemctl restart systemd-zram-setup@zram0.service 2>/dev/null || \
    systemctl start systemd-zram-setup@zram0.service 2>/dev/null || true

# --- 3) kernel tuning -------------------------------------------------------
say "Kernel-Parameter setzen"
cat > /etc/sysctl.d/99-freeze-guard.conf <<'EOF'
# Tuned for zram-backed swap on a VMware guest.

# zram is far cheaper than disk swap, so lean on it hard (max is 200).
vm.swappiness = 180

# zram is random-access; reading ahead wastes work.
vm.page-cluster = 0

# Start reclaiming much earlier. The default of 10 (=0.1% of RAM) let the
# machine run into the wall before the kernel reacted - the main reason it
# froze outright instead of merely getting slow.
vm.watermark_scale_factor = 125

# Smaller writeback bursts keep the block layer from stalling everything.
vm.dirty_background_ratio = 5
vm.dirty_ratio = 10
EOF
sysctl -q --load=/etc/sysctl.d/99-freeze-guard.conf

# --- 4) earlyoom: layer 1 ---------------------------------------------------
# earlyoom is C and mlockall'd, so it stays responsive while the system
# thrashes - which a shell script cannot. It acts long before the kernel OOM
# killer does.
#
# NOTE: the arguments deliberately live in a drop-in ExecStart, not in
# /etc/default/earlyoom. systemd expands $EARLYOOM_ARGS by splitting on
# whitespace WITHOUT honouring quotes, so a regex written there would reach
# earlyoom with literal quote characters (matching nothing) and would break
# apart at the space in "Web Content". In a unit file systemd parses the quotes
# properly, so both problems disappear.
say "earlyoom konfigurieren"
rm -f /etc/default/earlyoom
mkdir -p /etc/systemd/system/earlyoom.service.d
cat > /etc/systemd/system/earlyoom.service.d/override.conf <<'EOF'
[Service]
# -m 10,5 : SIGTERM at 10% free RAM, SIGKILL at 5%
# -s 10,5 : ...and only once swap is nearly exhausted too (AND condition)
# -r 900  : memory report every 15 min
# -n      : desktop notification when it acts
ExecStart=
ExecStart=/usr/bin/earlyoom -m 10,5 -s 10,5 -r 900 -n \
  --avoid "^(systemd|systemd-.*|Xorg|Xwayland|cinnamon.*|nemo|muffin|sshd|NetworkManager|dbus-daemon|dbus-broker.*|pipewire.*|wireplumber|pulseaudio|lightdm|rtkit-daemon|polkitd|mariadbd|code|claude|codium)$" \
  --prefer "^(node|esbuild|tsserver|webpack|rollup|vite|next-server|jest|eslint|tsc|php|php8[.][0-9]|composer|Web Content|Isolated Web Co|chrome|chromium)$"
EOF

# --- 5) freeze-guard: layer 2 -------------------------------------------
say "freeze-guard installieren"
install -m 755 "$SRC_DIR/fg-daemon.sh" /usr/local/bin/fg-daemon.sh
cat > /etc/systemd/system/freeze-guard.service <<'EOF'
[Unit]
Description=freeze-guard - PSI-based freeze protection and IDE OOM-score fixup
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/local/bin/fg-daemon.sh
Restart=always
RestartSec=5

# Must stay schedulable while everything else stalls.
Nice=-15
IOSchedulingClass=realtime
IOSchedulingPriority=0
OOMScoreAdjust=-1000
MemoryAccounting=yes
MemoryMin=32M

[Install]
WantedBy=multi-user.target
EOF

# --- 6) black-box recorder --------------------------------------------------
# There is no sysstat on this machine, so after a hard reset nothing shows what
# the VM was doing while it died. This records it.
say "Blackbox-Aufzeichnung installieren"
install -m 755 "$SRC_DIR/run-monitor.py" /usr/local/bin/fg-monitor
cat > /etc/systemd/system/freeze-guard-recorder.service <<'EOF'
[Unit]
Description=freeze-guard-recorder - records memory/CPU history so freezes leave evidence
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/local/bin/fg-monitor --log --interval 5 --file /var/log/freeze-guard-history.csv
Restart=always
RestartSec=5
Nice=-5
OOMScoreAdjust=-900
MemoryAccounting=yes
MemoryMin=16M

[Install]
WantedBy=multi-user.target
EOF

# --- 7) enable everything ---------------------------------------------------
say "Dienste starten"
systemctl daemon-reload
systemctl enable --now earlyoom.service
systemctl restart earlyoom.service
systemctl enable --now freeze-guard.service
systemctl restart freeze-guard.service
systemctl enable --now freeze-guard-recorder.service
systemctl restart freeze-guard-recorder.service

# The CLI and the GUI stay symlinks so edits in ~/Applications take effect
# immediately; the daemons are copies so they do not depend on /home being
# mounted at boot. `vfg` is kept as the short alias (`fg` cannot be used - it is
# a bash builtin and would never be reached).
ln -sf "$SRC_DIR/freeze-guard" /usr/local/bin/freeze-guard
ln -sf "$SRC_DIR/freeze-guard" /usr/local/bin/vfg
ln -sf "$SRC_DIR/run.py" /usr/local/bin/fg-gui

# --- 7b) desktop entry for the graphical dashboard --------------------------
say "Menüeintrag für die Oberfläche anlegen"
if ! python3 -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null; then
    echo "   GTK3-Bindings fehlen - installiere python3-gi"
    apt-get install -y -qq python3-gi gir1.2-gtk-3.0 || true
fi
cat > /usr/share/applications/freeze-guard.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Freeze Guard
Name[de]=Freeze Guard
Comment=Memory pressure and freeze early warning
Comment[de]=Speicherdruck und Freeze-Frühwarnung
Exec=/usr/local/bin/fg-gui
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;
Keywords=memory;RAM;swap;freeze;monitor;Speicher;
StartupNotify=true
EOF
command -v update-desktop-database >/dev/null && \
    update-desktop-database /usr/share/applications 2>/dev/null || true

# --- 8) verify --------------------------------------------------------------
say "Prüfung"
fail=0
for s in earlyoom freeze-guard freeze-guard-recorder; do
    state=$(systemctl is-active "$s.service" 2>/dev/null || true)
    if [ "$state" = "active" ]; then
        printf '   \033[1;32m%-18s aktiv\033[0m\n' "$s"
    else
        printf '   \033[1;31m%-18s %s\033[0m\n' "$s" "$state"; fail=1
    fi
done

if swapon --show=NAME --noheadings 2>/dev/null | grep -q zram; then
    printf '   \033[1;32m%-18s aktiv\033[0m\n' "zram"
else
    printf '   \033[1;33m%-18s nicht aktiv (ggf. Neustart nötig)\033[0m\n' "zram"; fail=1
fi

# Confirm earlyoom really got its regex - the quoting bug this override fixes
# would show up here as literal quotes in the command line.
if tr '\0' ' ' < "/proc/$(pgrep -x earlyoom | head -1)/cmdline" 2>/dev/null | grep -q 'avoid.*code'; then
    printf '   \033[1;32m%-18s Regex korrekt übergeben\033[0m\n' "earlyoom-args"
else
    printf '   \033[1;33m%-18s Regex prüfen: freeze-guard status\033[0m\n' "earlyoom-args"
fi

echo
swapon --show 2>/dev/null || true
echo
echo "   Oberfläche       :  freeze-guard ui        (oder Menü: Freeze Guard)"
echo "   Live im Terminal :  freeze-guard monitor"
echo "   Auswahl          :  freeze-guard           (fragt Konsole oder Fenster)"
echo "   Kurzstatus       :  freeze-guard status"
echo "   Historie/Logs    :  freeze-guard log     bzw.  freeze-guard history"
echo "   Rückbau          :  sudo freeze-guard uninstall"
[ "$fail" -eq 0 ] && echo && echo "   Alles aktiv." || { echo; echo "   Bitte obige Meldungen prüfen."; }
