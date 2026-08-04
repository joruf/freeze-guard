#!/bin/bash
# freeze-guard - second-layer protection against the freezes on this VM.
#
# Layer 1 is earlyoom (C, mlockall'd, survives thrashing).
# This daemon adds what earlyoom does not do:
#
#   A) Normalises oom_score_adj on VS Code / Claude Code helpers. VS Code ships
#      extension hosts with oom_score_adj=300, the highest score on the system -
#      which is why the kernel OOM killer took the Claude Code host on Jul 30
#      while the actual memory hog survived.
#
#   B) Kills the real hog when PSI shows the machine is genuinely stalled.
#
#   C) Warns (does NOT kill) about CPU runaways. A busy loop alone does not
#      freeze Linux - the scheduler is fair - so killing on CPU would only cost
#      the user legitimate builds. But a runaway is worth knowing about, and it
#      makes a process the prime suspect once memory pressure does hit.
#
# Observed freeze signature (Aug 03, 10:14->10:17): blk_mq_run_work_fn hogged
# CPU, doubling every few seconds, alongside InnoDB memory pressure events. The
# CPU heat is real, but it is the kernel spinning in block I/O reclaim - not an
# application loop.
#
set -u

# --- tunables ---------------------------------------------------------------
PSI_FULL_THRESHOLD=20      # % of last 60s with ALL tasks stalled on memory
MEM_AVAIL_PCT_MAX=15       # only act when MemAvailable is genuinely low
SUSTAIN_SECONDS=60         # both conditions must hold this long before killing
CHECK_INTERVAL=10
TERM_GRACE=8               # seconds between SIGTERM and SIGKILL
OOM_FIXUP_EVERY=6          # run the oom_score_adj sweep every N ticks (=60s)

CPU_RUNAWAY_PCT=90         # % of one core
CPU_RUNAWAY_SECONDS=300    # sustained this long -> warn (never kill)

STATE_FILE=/run/freeze-guard.stalled_since

# comm is capped at 15 chars by the kernel, so these must match the TRUNCATED
# names - "fg-daemon.sh" arrives as "freeze-guard".
WHITELIST='^(systemd|systemd-.*|init|Xorg|Xwayland|cinnamon|cinnamon-.*|nemo|muffin|sshd|NetworkManager|wpa_supplicant|dbus-daemon|dbus-broker.*|pipewire|pipewire-.*|wireplumber|pulseaudio|lightdm|gdm.*|rtkit-daemon|polkitd|udisksd|earlyoom|freeze-guard|fg-daemon[.]sh|fg-monitor|fg-gui|logger|awk|sleep|kill)$'

# Preferred victims: build/dev processes that are safe to lose and restart, plus
# browser content processes (the session is restored). Deliberately NOT here:
# mariadbd (data integrity) and joplin/editors (unsaved work) - those stay
# ordinary candidates, ranked purely by size.
PREFER='^(node|esbuild|tsserver|webpack|rollup|vite|next-server|jest|eslint|tsc|php|php8[.][0-9]|composer|Web Content|Isolated Web Co|chrome|chromium)$'

# Killed only as a last resort - losing these costs the user work.
PROTECTED='^(code|claude|codium)$'

HZ=$(getconf CLK_TCK 2>/dev/null || echo 100)
PAGE_KB=$(( $(getconf PAGE_SIZE 2>/dev/null || echo 4096) / 1024 ))
declare -A CPU_PREV=() CPU_HIGH_SINCE=() CPU_WARNED=()

log()  { logger -t freeze-guard -p daemon.warning -- "$1"; }
info() { logger -t freeze-guard -p daemon.info -- "$1"; }

# --- A) keep the IDE out of the kernel's crosshairs -------------------------
normalise_oom_scores() {
    local pid comm adj
    for pid in $(pgrep -x 'code|codium|claude' 2>/dev/null); do
        [ -r "/proc/$pid/oom_score_adj" ] || continue
        adj=$(cat "/proc/$pid/oom_score_adj" 2>/dev/null) || continue
        if [ "$adj" -ge 200 ] 2>/dev/null; then
            echo 0 > "/proc/$pid/oom_score_adj" 2>/dev/null
        fi
    done
}

# --- B) PSI-based stall detection -------------------------------------------
mem_psi_full_avg60() {
    local v
    v=$(awk '/^full/{for(i=2;i<=NF;i++) if($i ~ /^avg60=/){sub(/^avg60=/,"",$i); print $i; exit}}' \
        /proc/pressure/memory 2>/dev/null)
    echo "${v:-0}"
}

mem_available_pct() {
    awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{if(t>0) printf "%.1f", a*100/t; else print 100}' \
        /proc/meminfo 2>/dev/null || echo 100
}

# Weighted pick: freeing memory is the point, so size dominates and the tier
# only tilts the scale. A strict tier order would let a 300 MB build helper
# shield a multi-GB hog. A process flagged as a CPU runaway is the prime
# suspect and gets an extra nudge.
MIN_RSS_KB=204800   # 200 MB - below this a kill would not buy us anything
select_victim() {
    local suspects="${1:-}"
    ps -eo pid=,rss=,comm= --sort=-rss 2>/dev/null | awk \
        -v wl="$WHITELIST" -v pf="$PREFER" -v pr="$PROTECTED" \
        -v self="$$" -v minrss="$MIN_RSS_KB" -v susp="$suspects" '
        BEGIN { n = split(susp, s, " "); for (i = 1; i <= n; i++) is_susp[s[i]] = 1 }
        {
            pid = $1; rss = $2;
            comm = "";
            for (i = 3; i <= NF; i++) comm = comm (i > 3 ? " " : "") $i;
            if (pid <= 2 || pid == self) next;
            if (rss + 0 <= 0) next;
            if (comm ~ wl) next;
            w = 1.0;
            if (comm ~ pf)      w = 3.0;
            else if (comm ~ pr) w = 0.15;
            if (pid in is_susp) w = w * 2.0;
            score = rss * w;
            if (score > best) { best = score; bpid = pid; bcomm = comm; brss = rss }
        }
        END { if (bpid != "" && brss + 0 >= minrss + 0) print bpid, bcomm, brss }'
}

# --- C) CPU runaway detection (warn only) -----------------------------------
# Reads every process in ONE awk pass over /proc/*/stat. Scanning only the
# biggest memory users would miss a busy loop holding a few MB - precisely the
# runaway we want to see. Returns the offending PIDs as kill-suspects.
scan_all_procs() {
    awk '
        FNR == 1 {
            split(FILENAME, pp, "/"); pid = pp[3];
            # comm sits between the first "(" and the LAST ")" and may itself
            # contain spaces and parens, so anchor on a greedy match.
            s = index($0, "(");
            if (!match($0, /.*\)/)) next;
            e = RSTART + RLENGTH - 1;
            comm = substr($0, s + 1, e - s - 1);
            gsub(/ /, "_", comm);
            split(substr($0, e + 2), f, " ");
            # in that remainder: utime=12, stime=13, rss(pages)=22
            print pid, f[12] + f[13], f[22], comm;
        }' /proc/[0-9]*/stat 2>/dev/null
}

detect_cpu_runaways() {
    local now=$1 pid ticks rsspg comm prev delta pct since rss_kb
    local out=""
    while read -r pid ticks rsspg comm; do
        [ -n "$ticks" ] || continue
        prev=${CPU_PREV[$pid]:-}
        CPU_PREV[$pid]=$ticks
        [ -n "$prev" ] || continue

        delta=$(( ticks - prev ))
        [ "$delta" -ge 0 ] 2>/dev/null || { unset 'CPU_HIGH_SINCE[$pid]'; continue; }
        pct=$(( delta * 100 / (CHECK_INTERVAL * HZ) ))

        if [ "$pct" -ge "$CPU_RUNAWAY_PCT" ]; then
            since=${CPU_HIGH_SINCE[$pid]:-}
            if [ -z "$since" ]; then
                CPU_HIGH_SINCE[$pid]=$now
            elif [ $(( now - since )) -ge "$CPU_RUNAWAY_SECONDS" ]; then
                out="$out $pid"
                if [ -z "${CPU_WARNED[$pid]:-}" ]; then
                    rss_kb=$(( rsspg * PAGE_KB ))
                    log "CPU-DAUERLAST pid=$pid ($comm) hält ${pct}% einer CPU seit $(( now - since ))s, RSS $(( rss_kb / 1024 )) MB -- nur Warnung, kein Kill"
                    CPU_WARNED[$pid]=1
                fi
            fi
        else
            unset 'CPU_HIGH_SINCE[$pid]' 'CPU_WARNED[$pid]'
        fi
    done < <(scan_all_procs)
    echo "${out# }"
}

# Drop bookkeeping for processes that no longer exist, so the arrays cannot
# grow without bound over days of uptime.
prune_cpu_state() {
    local pid
    for pid in "${!CPU_PREV[@]}"; do
        [ -d "/proc/$pid" ] || unset 'CPU_PREV[$pid]' 'CPU_HIGH_SINCE[$pid]' 'CPU_WARNED[$pid]'
    done
}

notify_desktop() {
    local msg="$1" uid duser
    duser=$(loginctl list-sessions --no-legend 2>/dev/null | awk '$3!="" {print $3; exit}')
    [ -n "$duser" ] || duser=$(stat -c %U /run/user/1000 2>/dev/null)
    [ -n "$duser" ] || return 0
    uid=$(id -u "$duser" 2>/dev/null) || return 0
    [ -S "/run/user/$uid/bus" ] || return 0
    # runuser, not sudo: no PAM tty requirement, cannot block on a prompt.
    timeout 5 runuser -u "$duser" -- env DISPLAY=:0 \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
        notify-send -u critical "Freeze Guard" "$msg" 2>/dev/null || true
}

# --- main loop --------------------------------------------------------------
info "started (kill: psi>=${PSI_FULL_THRESHOLD}% + memavail<${MEM_AVAIL_PCT_MAX}% für ${SUSTAIN_SECONDS}s | warn: cpu>=${CPU_RUNAWAY_PCT}% für ${CPU_RUNAWAY_SECONDS}s)"

tick=0
while true; do
    tick=$(( tick + 1 ))
    now=$(date +%s)

    [ $(( tick % OOM_FIXUP_EVERY )) -eq 1 ] && normalise_oom_scores
    [ $(( tick % 30 )) -eq 0 ] && prune_cpu_state

    suspects=$(detect_cpu_runaways "$now")

    psi=$(mem_psi_full_avg60)
    avail=$(mem_available_pct)

    stalled=$(awk -v p="$psi" -v pt="$PSI_FULL_THRESHOLD" -v a="$avail" -v at="$MEM_AVAIL_PCT_MAX" \
        'BEGIN{print (p+0>=pt+0 && a+0<at+0) ? 1 : 0}')

    if [ "$stalled" = "1" ]; then
        [ -f "$STATE_FILE" ] || echo "$now" > "$STATE_FILE"
        since=$(cat "$STATE_FILE" 2>/dev/null || echo "$now")
        duration=$(( now - since ))

        if [ "$duration" -ge "$SUSTAIN_SECONDS" ]; then
            victim=$(select_victim "$suspects")
            if [ -n "$victim" ]; then
                # comm may contain spaces ("Web Content"): first field is the
                # pid, last is the rss, everything between is the name.
                vpid=$(echo "$victim"  | awk '{print $1}')
                vrss=$(echo "$victim"  | awk '{print $NF}')
                vcomm=$(echo "$victim" | awk '{$1=""; $NF=""; sub(/^ +/,""); sub(/ +$/,""); print}')
                vmb=$(( vrss / 1024 ))

                log "STALL ${duration}s (mem-psi=${psi}% avail=${avail}%) -- beende pid=$vpid ($vcomm, ${vmb} MB)"
                notify_desktop "System hängt seit ${duration}s. Beende $vcomm (${vmb} MB), um die VM am Leben zu halten."

                kill -TERM "$vpid" 2>/dev/null
                for _ in $(seq "$TERM_GRACE"); do
                    kill -0 "$vpid" 2>/dev/null || break
                    sleep 1
                done
                if kill -0 "$vpid" 2>/dev/null; then
                    log "pid=$vpid ignorierte SIGTERM -- SIGKILL"
                    kill -KILL "$vpid" 2>/dev/null
                fi
            else
                log "STALL ${duration}s, aber kein geeignetes Ziel über $(( MIN_RSS_KB / 1024 )) MB gefunden"
            fi
            rm -f "$STATE_FILE"
        fi
    else
        rm -f "$STATE_FILE"
    fi

    sleep "$CHECK_INTERVAL"
done
