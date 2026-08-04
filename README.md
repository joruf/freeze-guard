# Freeze Guard

Schutz und Überwachung gegen Komplett-Freezes unter Speicherdruck. Entstanden
auf dieser Linux-Mint-VM (VMware, 21 GB RAM, 16 vCPU), die am 03.08.2026
viermal an einem Tag einfror.

## Nur für VMs? Nein — VMs treffen es nur früher

Alle vier Schichten sind generisches Linux: zram, earlyoom, PSI-Auswertung,
`watermark_scale_factor`. Nichts davon setzt einen Hypervisor voraus.

Was Virtualisierung ändert, ist die **Härte des Ausgangs**. Die Todesspirale
lebt von der Latenz des Auslagerungsziels: eine VMware-VDisk liefert unter Last
ein Vielfaches der Latenz einer lokalen NVMe und schwankt dabei stark. Auf Blech
mit NVMe-Swap endet dieselbe Speichernot deshalb meist bei *langsam*, in der VM
bei *tot*. Frühwarnung, Forensik und earlyoom greifen auf beiden identisch —
`freeze-guard` ist auf einem Laptop nicht unnötig, nur seltener dramatisch.

## Ist es die CPU oder der Speicher? — Beides, in dieser Reihenfolge

Der Lüfter bläst vor jedem Freeze auf Anschlag, und das ist **kein Zufall und
keine Einbildung**: Die CPU brennt tatsächlich. Nur brennt sie nicht in einer
Anwendung, sondern im Kernel.

Der Ablauf am 03.08. zwischen 10:14 und 10:17, aus dem Journal:

```
10:14:04  blk_mq_run_work_fn hogged CPU for >10000us    11 times
10:14:41                                                 19 times
10:14:54                                                 35 times
10:15:20                                                 67 times
10:16:40                                                131 times
10:17:20                                                259 → 515 → 1027 times
          mariadbd: InnoDB: Memory pressure event
```

`blk_mq_run_work_fn` ist der Block-I/O-Worker des Kernels. *"hogged CPU for
>10000us"* heißt wörtlich: Er hat die CPU über 10 ms am Stück belegt — echte,
heiße Last. Die Verdopplungsreihe zeigt, wie sich eine **Dauerschleife im
Kernel** aufschaukelt: Speicher wird knapp → der Kernel lagert auf die
VMware-VDisk aus → die I/O-Latenz explodiert → der Kernel kommt mit dem
Zurückholen nicht nach → er lagert mehr aus. Nach gut drei Minuten steht alles.

Es ist also eine Dauerschleife — aber im Kernel, ausgelöst durch Speicherdruck.
Die Grafikkarte scheidet aus: Die VM hat nur eine emulierte SVGA ohne
GPU-Passthrough.

**Kann eine Anwendung trotzdem der Auslöser sein?** Ja, und das ist offen:
Ein Prozess, der in einer Schleife Speicher belegt, erzeugt genau diesen Druck.
Bisher lässt es sich nicht beweisen, weil `sysstat`/`sar` nicht installiert ist
und nach jedem harten Reset **keinerlei Historie** existiert. Genau diese Lücke
schließt der Recorder (`run-monitor.py --log`).

## Warum der alte Watchdog nicht half

`/usr/local/bin/hang-watchdog.sh` löste bei `loadavg > nproc × 1.5` (= 24) aus
und schoss den Top-CPU-Prozess mit `kill -9` ab. Drei Probleme:

1. **Falsches Signal.** Load 24 erreicht auf 16 vCPUs jeder parallele Build.
2. **Zu spät.** Beim echten Freeze steht der Block-Layer, nicht die Anwendung.
3. **Er hätte selbst nicht mehr gelaufen** — ein Bash-Skript wird während eines
   Thrashing-Stalls nicht mehr zuverlässig gescheduled.

Der schwarze Claude-Code-Plugin-Screen kam übrigens *nicht* von diesem Watchdog
(der war da schon gelöscht), sondern vom Kernel-OOM-Killer:

```
Jul 30 12:21:36  Out of memory: Killed process 4276 (code) ... oom_score_adj:300
```

VS Code setzt auf Extension-Hosts `oom_score_adj=300` → OOM-Score ~880, der
höchste im System. Der Kernel greift damit **immer zuerst den Extension-Host**,
egal wer wirklich frisst.

## Was installiert wird

| Schicht | Komponente | Aufgabe |
|---|---|---|
| 0 | **zram** (max. 8 GB, zstd) | Swap komprimiert im RAM statt auf VDisk → kein Block-I/O, keine Kernel-Schleife. Wichtigste Einzelmaßnahme. |
| 0 | **sysctl** | `watermark_scale_factor 10→125`: der Kernel räumt früh auf, statt in die Wand zu laufen. |
| 1 | **earlyoom** | Greift bei <10 % freiem RAM ein. In C, per `mlockall` im RAM fixiert — bleibt handlungsfähig, während alles thrasht. |
| 2 | **freeze-guard** | (a) normalisiert `oom_score_adj` der IDE, (b) killt bei echtem PSI-Stall, (c) **warnt** bei CPU-Dauerlast. |
| 3 | **freeze-guard-recorder** | Blackbox: schreibt alle 5 s eine Messzeile, damit der nächste Freeze Beweise hinterlässt. |

### Wann Schicht 2 tötet

Bewusst eng — beide Bedingungen müssen **60 s durchgehend** gelten:

- `/proc/pressure/memory` → `full avg60 ≥ 20 %`
- `MemAvailable < 15 %`

Load average spielt keine Rolle mehr.

### Wann sie nur warnt

Ein Prozess mit **≥ 90 % einer CPU über 5 Minuten** wird ins Journal gemeldet,
aber **nicht** gekillt. Eine reine Rechenschleife friert Linux nicht ein — der
Scheduler ist fair — und ein Kill würde nur legitime Builds kosten. Sie macht
den Prozess aber zum Hauptverdächtigen: Kommt später Speicherdruck dazu, zählt
er bei der Opferauswahl doppelt.

### Wen es killt

Speicher freizugeben ist der Zweck, also entscheidet die **Größe** — die
Kategorie gewichtet nur. Punktzahl = `RSS × Gewicht`, höchste gewinnt:

| Gewicht | Kategorie | Beispiele |
|---|---|---|
| ×3,0 | gefahrlos neustartbar | `node`, `esbuild`, `vite`, `webpack`, `tsc`, `php`, Browser-Renderer |
| ×1,0 | normal | Firefox, Joplin, MariaDB |
| ×0,15 | IDE | `code`, `claude` — nur wenn sie alles überragen |
| ×2 extra | CPU-Dauerlast | Verdacht auf den Verursacher |
| — | nie | `systemd`, `Xorg`, `cinnamon`, `sshd`, `dbus`, Audio, Display-Manager |

Unter 200 MB wird nichts gekillt — das würde den Stall nur vertagen.
Erst `SIGTERM`, nach 8 s `SIGKILL`. Alles landet im Journal und als
Desktop-Benachrichtigung.

## Installation

```bash
sudo ~/Applications/freeze-guard/install.sh
```

Kein Neustart nötig. Der alte `hang-watchdog` wird dabei entfernt.

## Bedienung — Konsole oder Fenster, freie Wahl

```bash
freeze-guard              # fragt im Terminal, was geöffnet werden soll
freeze-guard ui           # grafische Oberfläche (auch: Menü → Freeze Guard)
freeze-guard monitor      # Live-Dashboard im Terminal ([q] beendet)
freeze-guard status       # Kurzstatus: Speicher, zram, PSI, Dienste, OOM-Scores
freeze-guard history      # Forensik: was lief vor dem letzten Ausfall?
freeze-guard log          # was earlyoom / Guard / Kernel gekillt haben
freeze-guard dryrun       # wen es JETZT träfe — reine Simulation
sudo freeze-guard uninstall
```

`vfg` ist der Kurz-Alias für `freeze-guard`. **`fg` wäre keiner**: das ist ein
Bash-Builtin und würde ein Programm dieses Namens im PATH nie erreichen.

Konsole und Oberfläche messen identisch — `run.py` importiert den Sampler aus
`run-monitor.py`. Zwei Implementierungen würden im Ernstfall unterschiedliche
Zahlen behaupten, und dann glaubt man keiner mehr.

### Die Oberfläche

Fünf Seiten, alle auf denselben Messungen:

| Seite | Inhalt |
|---|---|
| **Live** | Speicher- und Swap-Balken, Druck/Auslagern/CPU als Verlaufsdiagramme, größte Speicher- und CPU-Verbraucher, Schalter für die Schutzschichten |
| **Verlauf** | die Aufzeichnung als Zeitachse; rote Flächen sind Ausfälle, darunter aufklappbar die letzten Messungen davor |
| **Protokoll** | dieselben Journal-Abfragen wie `freeze-guard log` |
| **Simulation** | wen der Guard jetzt beenden würde, mit Punktzahl und Kategorie |
| **Einstellungen** | Sprache, Intervall, Systemleiste, automatisches Öffnen, Benachrichtigungen, Autostart |

Eigenschaften, die im Ernstfall zählen:

- **Sprache Englisch/Deutsch**, Standard Englisch, umschaltbar im Menü und in den
  Einstellungen. Die Oberfläche baut sich sofort neu auf, ohne Neustart.
- **Start in der Systemleiste** (Standard). Das Symbol trägt die Stufe als Farbe
  und den freien Speicher als Text. Auf Cinnamon läuft das über XApp — das ist
  das einzige Backend dort, das den Text neben dem Symbol wirklich anzeigt;
  darunter AppIndicator, zuletzt `Gtk.StatusIcon`. Findet sich keins, startet das
  Fenster immer sichtbar: ein unsichtbares Programm ohne Weg zurück ist
  schlimmer als ein sichtbares.
- **Holt sich selbst nach vorn**, sobald die Stufe die eingestellte Schwelle
  erreicht (Standard WARNUNG) — genau einmal pro Vorfall. Erst wenn die Stufe
  wieder darunter fällt, ist es erneut scharf; sonst würde ein zappelnder
  PSI-Wert alle paar Sekunden den Fokus klauen. Für CPU-Dauerlast (Stufe ERHÖHT)
  die Schwelle auf ERHÖHT stellen.
- **Prozess beenden direkt aus der Liste**: die Spalte „Beenden“ schickt sofort
  `SIGTERM` und nach 8 s `SIGKILL` — ohne Rückfrage, weil ein Dialog während
  eines Stalls auch erst gescheduled und gelesen werden müsste. Vor dem `SIGKILL`
  wird `/proc/PID/comm` erneut geprüft, damit eine in der Zwischenzeit
  wiederverwendete PID nicht den falschen Prozess trifft. Fremde Prozesse laufen
  über `pkexec`; das Passwort fragt der Desktop, nie dieses Programm.
- **Schutzschichten an/aus**: die Schalter führen `systemctl enable --now` bzw.
  `disable --now` über `pkexec` aus, „Alle aktivieren“ alle drei auf einmal.
  Fehlt eine Unit, ist der Schalter aus und nennt den Installationsbefehl.
- **Intervall** Standard 5 s, einstellbar 1/2/5/10/30 s. Jeder Takt liest ganz
  `/proc`; 5 s sind schonend, 1 s zeigt Spitzen.

Einstellungen liegen in `~/.config/freeze-guard/settings.json`, der Autostart in
`~/.config/autostart/freeze-guard.desktop`. Kommandozeile für Sonderfälle:

```bash
fg-gui --window --lang de --interval 1 --page history
fg-gui --file /pfad/zu/einer/kopierten-history.csv    # fremde Aufzeichnung ansehen
```

### Frühwarnsignale, in der Reihenfolge ihres Auftretens

Konsole und Oberfläche zeigen sie von oben nach unten. Wer die ersten beiden
sieht, hat noch Minuten Zeit:

1. **Auslagern aus > 0 kB/s** — Seiten verlassen den RAM
2. **Major Faults steigen** — sie werden von der Platte zurückgeholt
3. **PSI some avg10 steigt** — Tasks warten auf Speicher
4. **PSI full avg10 steigt** — *alle* Tasks blockieren, die Todesspirale
5. **Kernel-Stalls +N NEU** — der Block-Layer brennt; jetzt bleiben Sekunden

Bei Stufe KRITISCH piept das Konsolen-Dashboard; die Oberfläche schickt eine
Desktop-Benachrichtigung und holt sich nach vorn.

### Forensik nach einem Freeze

```bash
freeze-guard history
```

Der Recorder schreibt alle 5 s. Eine **Lücke in der Zeitreihe ist der Freeze** —
`freeze-guard history` findet diese Lücken und zeigt die letzten Messungen davor,
inklusive dem größten Speicherverbraucher *und* dem größten CPU-Verbraucher
getrennt. Damit lässt sich beim nächsten Mal belegen, ob ein Anwendungsprozess
der Auslöser war.

Sobald der Alarm über OK steigt, wird jede Zeile per `fsync` auf die Platte
gezwungen — sonst wäre sie nach einem harten Reset verloren.

## Wenn es trotzdem wieder hängt

`freeze-guard history` und `freeze-guard log` zeigen, ob eine Schicht gegriffen hat. Falls nicht,
sind die Schwellen zu konservativ — in `/usr/local/bin/fg-daemon.sh`
`PSI_FULL_THRESHOLD` senken (z. B. auf 10), dann
`sudo systemctl restart freeze-guard`.

Bleibt der Druck dauerhaft hoch, ist die VM zu klein für die Workload
(VS Code + mehrere Claude-Instanzen + MariaDB + Apache + Firefox) — dann hilft
nur mehr RAM in den VMware-Einstellungen.

## Swap ist voll — vergrößern?

**Nein, nicht den Plattenswap.** Ein voller Swap ist für sich kein Notfall: dort
liegen kalte Seiten, die niemand zurückgefordert hat. Gefährlich ist die *Rate*
(Auslagern kB/s) und der PSI-Druck — genau das, was die Oberfläche anzeigt.

Mehr Plattenswap macht es sogar **schlimmer**: es vergrößert die Menge an Seiten,
die der Kernel auf das langsame Gerät schieben kann, und verlängert damit die
Thrashing-Phase vor dem OOM-Kill. Das ist die Todesspirale aus dem ersten
Abschnitt, nur mit mehr Anlauf.

Richtig ist **zram** — komprimierter Swap im RAM, kein Block-I/O:

```bash
sudo ~/Applications/freeze-guard/install.sh    # richtet zram + alles andere ein
```

Nur zram, ohne den Rest:

```bash
sudo apt install systemd-zram-generator
sudo tee /etc/systemd/zram-generator.conf >/dev/null <<'EOF'
[zram0]
zram-size = min(ram / 2, 8192)
compression-algorithm = zstd
swap-priority = 100
fs-type = swap
EOF
sudo systemctl daemon-reload
sudo systemctl start systemd-zram-setup@zram0.service
swapon --show      # zram0 muss Priorität 100 haben
```

zram schafft keinen Speicher aus nichts, es tauscht CPU gegen Kapazität: zstd
komprimiert Heap und Textseiten grob 2–3:1, 8 GB zram fassen also ungefähr
16–24 GB kalte Seiten. Deckel bei `ram / 2`, denn zram liegt selbst im RAM.

Der vorhandene `/swapfile` bleibt als Überlauf liegen — mit Priorität 100 für
zram nimmt der Kernel die Platte erst, wenn zram voll ist. Wer die Datei
trotzdem vergrößern will, braucht **erst freien Plattenplatz** (hier war `/` zu
93 % voll, das ist der eigentliche Blocker):

```bash
sudo swapoff /swapfile
sudo fallocate -l 6G /swapfile     # oder: dd if=/dev/zero of=/swapfile bs=1M count=6144
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
# in /etc/fstab die Priorität festschreiben, damit zram gewinnt:
#   /swapfile none swap sw,pri=10 0 0
```

## Dateien

| Datei | Rolle |
|---|---|
| `run.py` | grafisches Dashboard, installiert als Befehl `fg-gui` |
| `run-monitor.py` | Konsolen-Dashboard und Blackbox-Recorder, installiert als `fg-monitor` |
| `fg-daemon.sh` | der Guard-Daemon (Schicht 2), installiert als `fg-daemon.sh` |
| `freeze-guard` | die CLI, installiert als `freeze-guard` und `vfg` |
| `fg_platform.py` | Fähigkeitsmatrix je Maschinenprofil, Grundlage von `doctor` und OS-Test |
| `install.sh` | richtet alle vier Schichten ein, idempotent |
| `tests/` | Unit-, E2E- und OS-Tests |

Die beiden Python-Einstiegspunkte heißen im Repository `run.py` und
`run-monitor.py` (wie üblich für ein Projekt, das man auch direkt aus dem
Quellordner startet), installiert werden sie als suffixlose Befehle `fg-gui` und
`fg-monitor`. `install.sh` ist die einzige Stelle, die beide Namen kennt.

Aus dem Quellordner heraus geht alles ohne Installation:

```bash
./run.py --window --lang de        # Oberfläche
./run-monitor.py --once            # eine Momentaufnahme im Terminal
./freeze-guard doctor              # Fähigkeitsbericht dieser Maschine
```

## Tests

```bash
scripts/run-tests.sh          # alles (213 Tests, ~25 s)
scripts/run-tests.sh unit     # ohne Unterprozesse, < 1 s
scripts/run-tests.sh e2e      # Recorder, Kill-Pfad, Dashboard-Ablauf
scripts/run-tests.sh os       # Plattformmatrix + Bericht dieser Maschine
```

Ohne `pytest` fällt das Skript auf `unittest discover` zurück, ohne Display auf
`xvfb-run`. Was geprüft wird:

| Bereich | Inhalt |
|---|---|
| **Ampel** | jeder Zweig von `alert_level()`, die Priorität zwischen den Zweigen, und dass jeder Zweig einen übersetzbaren Schlüssel liefert |
| **/proc-Leser** | Felder, Einheiten, Monotonie der Zähler, `comm`-Kappung bei 15 Zeichen, Sampler-Vertrag (alle Schlüssel, die Recorder und Oberfläche lesen) |
| **Opferauswahl** | Gewichte, 200-MB-Grenze, Verdachts-Verdopplung, Schutzliste — **und ein Vergleich der Regeln mit `fg-daemon.sh`**, damit die Simulation nicht anders rechnet als der Daemon |
| **Aufzeichnung** | CSV-Format, Lückenerkennung (>60 s = Ausfall, 45 s = Zeitplan-Rauschen), kaputte Zeilen, Rechte, Zeitfenster, Rotation |
| **Sprachen** | Schlüsselparität EN/DE, gleiche Platzhalter, jeder im Code benutzte Schlüssel existiert, jeder Reason-Schlüssel des Monitors ist übersetzt |
| **Einstellungen** | Defaults, Clamping, kaputtes JSON, atomares Speichern, Autostart-Datei, nur-lesbares HOME |
| **Widgets** | Achsenskalierung, Kapazität, Zeitachse mit Lücken, Hover-Zuordnung, Tabellen-Aktualisierung ohne Verlust von Auswahl und Scrollposition |
| **E2E Dashboard** | ein Fenster durch einen echten Vorfall: grün → knapp → Warnung → kritisch → zurück, Selbstöffnen genau einmal pro Vorfall, Sprachwechsel im Betrieb, Dienst-Schalter, Schutz-Karte in allen drei Zuständen |
| **E2E Recorder** | Recorder als eigener Prozess, echte CSV, simulierter Freeze durch Loch in der Zeitreihe, beide Leser finden ihn; `SIGKILL` des Recorders darf keine halbe Zeile hinterlassen |
| **E2E Kill** | echte Kindprozesse: `SIGTERM`, Eskalation zu `SIGKILL`, PID-Reuse-Schutz, und dass für eigene Prozesse **nie** `pkexec` anläuft |
| **CLI** | Hilfe, Exit-Codes, `status`, `dryrun`, `doctor`, `--history` auf synthetischer Aufzeichnung, Shell-Syntax aller Skripte |
| **OS-Test** | neun Maschinenprofile (PSI fehlt, kein systemd, cgroup v1, Container, Wayland, headless, Windows, macOS) gegen die dokumentierte Matrix; dazu der Bericht dieser Maschine |

Es werden nur Prozesse signalisiert, die die Tests selbst gestartet haben, und
kein Test ruft `pkexec` auf — eine Testsuite, die einen Passwortdialog öffnet,
läuft niemand zweimal.

## Eigenlast der Programme

Alle Schleifen schlafen zwischen den Messungen, keine belegt eine CPU:

- `freeze-guard`: alle 10 s ein `awk`-Durchlauf über `/proc/*/stat`
  (612 Prozesse in 26 ms ≈ 0,26 % einer CPU), OOM-Fixup nur alle 60 s
- `freeze-guard-recorder`: alle 5 s, liest nur `/proc`, startet keine Unterprozesse
- `fg-monitor` (`run-monitor.py`): 1× pro Sekunde, nur solange das Fenster offen ist
- `fg-gui` (`run.py`): Standard alle 5 s ein `/proc`-Durchlauf im Hintergrund-Thread
  (~700 Prozesse ≈ 2 % einer CPU bei 5 s), gezeichnet wird nur, was sichtbar
  ist; in der Systemleiste läuft nur die Messung weiter
