# MCT – ESP32 + MicroPython

MicroPython-Projekt für einen ESP32 (`Generic ESP32 module`, MicroPython v1.29). Der Code liegt in `src/`
und spiegelt das Dateisystem des Boards (`src/main.py` → `/main.py`, `src/lib` → `/lib`).

Es gibt zwei Wege, Code auf das Board zu bringen:

- **USB** mit `mpremote` und den VS-Code-Tasks `MPR: …`.
- **Bluetooth** mit dem Werkzeug **`mpble`**: Auf dem Board läuft ein kleiner BLE-Loader, mit dem du
  Dateien hochlädst und Skripte startest, ganz ohne Kabel.

## Einrichtung

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .          # installiert mpremote, bleak und den Befehl `mpble` (Python 3.10 oder neuer)
```

`pip install -e .` holt am Ende auch die MicroPython-Stubs für IntelliSense nach `typings/` (nur wenn der
Ordner fehlt) und bricht ab, wenn keine venv aktiv ist. Aktualisieren geht mit dem Task
`MPY: Install/update stubs`.

Alle Abhängigkeiten stehen in `pyproject.toml`. Es gibt keine `requirements.txt`.

Der serielle Port für die `MPR:`-Tasks steht in `.vscode/settings.json` (`esp32.port`). `mpremote devs`
listet die vorhandenen Ports auf.

## USB-Ablauf (mpremote)

Über die VS-Code-Tasks (**Terminal → Task ausführen**):

| Task | Was er macht |
| --- | --- |
| `MPR: Run current file` | Führt die geöffnete Datei aus dem RAM aus; `src/` ist unter `/remote` eingebunden |
| `MPR: Deploy src to device` | Kopiert `src/` in den Flash und startet neu |
| `MPR: REPL`, `MPR: List device files`, … | Hilfsfunktionen |

**Einmal per USB übertragen** (`MPR: Deploy src to device`), damit der BLE-Loader auf dem Board liegt.
Danach geht alles auch per Bluetooth.

## Bluetooth-Ablauf (mpble)

### So funktioniert es

`boot.py` startet mit `ble_loader.start()` den Loader in einem Hintergrund-Thread. Das Board meldet sich
danach per Bluetooth als **`MCT-ESP`** und wartet auf Befehle. `main.py` und die USB-REPL laufen
trotzdem wie gewohnt. Beim Booten gibt das Board seine Bluetooth-Adresse auf der seriellen Konsole aus.

`mpble` ist das Gegenstück auf dem PC. Jeder Aufruf macht dasselbe: Board finden, verbinden, einen
Befehl ausführen, Verbindung trennen.

### Erste Schritte

```powershell
mpble link        # Board per USB anschließen, Adresse merken (einmalig, siehe unten)
mpble ping        # Verbindung testen, Ausgabe: pong
mpble sync src --reset    # geänderte Dateien hochladen und neu starten, damit main.py läuft
```

### Das Board finden: `link` und `unlink`

`mpble` braucht die Bluetooth-Adresse des Boards. Sie wird in dieser Reihenfolge ermittelt, die erste
passende Quelle gilt:

1. `--address AA:BB:CC:DD:EE:FF` bei einem einzelnen Befehl,
2. die Umgebungsvariable `MCT_BLE_ADDRESS`,
3. die Adresse, die `mpble link` gespeichert hat,
4. eine Suche nach einem Gerät namens `MCT-ESP` (dauert ca. 5 Sekunden und wird nicht gespeichert).

**`mpble link`** liest die Adresse direkt vom Board, das per USB angeschlossen ist, und speichert sie.
Danach verbindet sich `mpble` ohne Suche. Dabei wird geprüft, ob der Loader auf dem Board geladen ist.
Sonst gibt es einen Fehler und nichts wird gespeichert:

| Fehlermeldung | Bedeutung |
| --- | --- |
| `ble_loader is not on this board` | Die Datei `lib/ble_loader.py` fehlt: Projekt per USB übertragen |
| `ble_loader is on this board but not running` | `boot.py` ruft `ble_loader.start()` nicht auf |
| `ble_loader on this board is too old` | Der Loader kennt `link` noch nicht: Projekt neu übertragen |

```powershell
mpble link                 # nimmt das eine angeschlossene USB-Serial-Gerät
mpble link --port COM4     # oder Port angeben (auch über die Variable MCT_PORT)
mpble unlink               # gespeicherte Adresse vergessen, danach wird wieder gesucht
```

- `link` unterbricht kurz ein laufendes `main.py`, weil es die REPL des Boards benutzt.
- Die Adresse liegt in der Datei `tools/.mpble-address`. Sie steht in der `.gitignore` und wird
  nicht eingecheckt, jeder Rechner hat also sein eigenes Board.
- Antwortet das verknüpfte Board nicht (zum Beispiel nach einem Boardwechsel), weist `mpble` darauf hin:
  einfach erneut `mpble link` mit dem neuen Board ausführen, oder `mpble unlink`.
- In VS Code erledigt das der Task `BLE: Link board (address via USB)`. Er nutzt den Port aus
  `esp32.port` (bei `auto` das eine angeschlossene USB-Serial-Gerät). Die anderen BLE-Tasks brauchen
  keine Adresse: Sie nehmen die gespeicherte oder suchen das Board.

### Alle Befehle

Globale Optionen stehen **vor** dem Befehl, zum Beispiel `mpble --address AA:BB:CC:DD:EE:FF sync src`.

| Option | Bedeutung |
| --- | --- |
| `--address AA:BB:CC:DD:EE:FF` | mit diesem Board verbinden, keine Suche |
| `--scan-time SEKUNDEN` | Dauer der Suche (Standard 5) |
| `--name NAME` | gesuchter Bluetooth-Name (Standard `MCT-ESP`) |
| `--pin PIN` | PIN des Boards, siehe Abschnitt Sicherheit (auch `MCT_BLE_PIN`) |

| Befehl | Was er macht |
| --- | --- |
| `ping` | prüft, ob das Board antwortet (`pong`) |
| `link [--port COM4]` | merkt sich die Adresse des per USB angeschlossenen Boards (siehe oben) |
| `unlink` | vergisst die gemerkte Adresse |
| `repl` | interaktive Python-Konsole auf dem Board (siehe unten) |
| `scan` | listet Bluetooth-Geräte in der Nähe; `*` markiert Boards mit dem Loader |
| `sync [ORDNER] [--reset]` | spiegelt `ORDNER` (Standard `src`) nach `/` auf dem Board; Dateien mit gleicher Prüfsumme werden übersprungen, gelöscht wird nichts. `--reset` startet das Board danach neu, damit `boot.py`/`main.py` mit den neuen Dateien laufen |
| `put LOKAL [ZIEL]` | lädt eine einzelne Datei hoch (Standardziel `/<Dateiname>`) |
| `exec LOKAL [--stop] [--detach]` | lädt ein Skript als `/_run.py` hoch, startet es und zeigt seine `print()`-Ausgabe live. Module, die es importiert, müssen schon auf dem Board liegen (vorher `sync`). Mit `--stop` wird ein noch laufendes Programm vorher beendet (das Board startet dafür neu), sonst meldet das Board „busy“ |
| `run ZIEL [--stop] [--detach]` | startet eine Datei, die schon auf dem Board liegt. Mit `--detach` läuft sie weiter, auch wenn die Verbindung getrennt wird. `--stop` wie bei `exec` |
| `stop` | stoppt das laufende Skript (setzt dafür das Board zurück) |
| `reset` | startet das Board neu |
| `autorun PFAD` / `autorun --clear` | führt `PFAD` bei jedem Start aus (5 s nach dem Booten); `--clear` oder kein Pfad entfernt es |
| `ls [PFAD]` | listet ein Verzeichnis (Standard `/`) |
| `rm PFAD` | löscht eine Datei |
| `unpair` | entfernt eine Windows-Kopplung mit dem Board (siehe Fehlersuche) |

`mpble --help` und `mpble BEFEHL --help` zeigen die Kurzhilfe (auf Englisch). Strg+C bei einem laufenden
`exec`/`run` sendet *stop*. `--stop` hilft, wenn ein Programm weiterläuft, obwohl `mpble` nicht mehr
verbunden ist (z. B. Fenster geschlossen, Task beendet, `--detach`): ohne die Option lehnt das Board
jeden neuen Upload mit „busy“ ab. Läuft nichts, kostet `--stop` nichts.

### Die Konsole: `repl`

`mpble repl` öffnet eine Python-Konsole auf dem Board, wie die USB-REPL, nur per Bluetooth:

```
>>> x = 5
>>> x * 2
10
>>> for i in range(3):
...     print("i =", i)
...
i = 0
i = 1
i = 2
>>> import machine
>>> machine.freq()
160000000
```

- Ausdrücke geben ihren Wert aus, Variablen bleiben zwischen den Eingaben erhalten (auch wenn du die
  Verbindung trennst und neu verbindest, bis das Board neu startet).
- Mehrzeilige Blöcke (`for`, `if`, `def`, …) werden mit einer leeren Zeile abgeschlossen.
- Ein Fehler zeigt den Traceback, die Sitzung läuft weiter.
- **Strg+C** an der Eingabe verwirft die Zeile. Strg+C **während Code läuft** setzt das Board zurück
  (anders kann man MicroPython hier nicht unterbrechen), die Variablen sind dann weg und die Konsole endet.
- Beenden mit `exit()` oder Strg+Z und Enter (Linux/macOS: Strg+D).
- Der Code läuft neben `main.py` im selben Speicher. Während etwas läuft, nimmt das Board keine anderen
  Befehle an.
- Hängt an der USB-Buchse gleichzeitig ein serielles Terminal, erscheinen dessen Tastatureingaben (das Board
  gibt sie als Echo aus) auch in der Bluetooth-Ausgabe.

### Typische Abläufe

| Ich möchte … | Befehl |
| --- | --- |
| meine Änderungen hochladen und `main.py` starten | `mpble sync src --reset` |
| ein Skript ausprobieren, ohne es zu behalten | `mpble exec src\test\praktikum_01.py` |
| dasselbe, auch wenn das vorige Programm noch läuft | `mpble exec --stop src\test\praktikum_01.py` (der VS-Code-Task macht das immer) |
| ein Programm starten und die Verbindung trennen | `mpble run /main.py --detach` |
| etwas bei jedem Start ausführen | `mpble autorun /main.py` |
| sehen, was auf dem Board liegt | `mpble ls /` |
| etwas schnell ausprobieren | `mpble repl` |

VS-Code-Tasks: `BLE: Link board (address via USB)`, `BLE: Sync src to device`, `BLE: Run current file`,
`BLE: REPL`, `BLE: Stop / reset device`.

### Was beim Hochladen angezeigt wird

Zuerst die Verbindung (mit der ausgehandelten MTU), dann pro Datei ein Fortschrittsbalken mit Größe und
Geschwindigkeit und am Ende die Prüfsumme, die das Board bestätigt hat:

```
Syncing 5 files from src ...
= /boot.py  unchanged
> /lib/ble_loader.py  [######              ]  32%  3.7 KB/11.6 KB  9.8 KB/s
> /lib/ble_loader.py  11.6 KB  1.4 s  8.1 KB/s  crc 3fa1c2d9 ok
Done: 1 uploaded (11.6 KB), 4 unchanged, 1.6 s
```

Der Balken bewegt sich nur in einem echten Terminal. Bei umgeleiteter Ausgabe gibt es eine Zeile pro Datei.

### Verhalten im Detail

- **Sicheres Hochladen:** Eine Datei wird zuerst als `<Name>.part` geschrieben, per CRC32 geprüft und
  erst dann umbenannt. Eine abgebrochene Übertragung beschädigt also nie die vorhandene Datei.
- **`main.py` läuft weiter:** Der Loader arbeitet in einem eigenen Thread. Ein kaputtes `main.py`
  verhindert nicht, dass `mpble sync` funktioniert.
- **Ausgabe:** Solange ein PC verbunden ist, wird alles, was ein Skript ausgibt, per Bluetooth
  zurückgeschickt. Es erscheint zusätzlich auf der USB-Konsole.
- **Importe:** Von einem Skript geladene Module werden vor dem nächsten Start verworfen, damit neu
  hochgeladene Bibliotheken wirklich verwendet werden.
- **Eine Verbindung:** Der ESP akzeptiert nur einen Bluetooth-Client. Solange ein `mpble`-Befehl
  verbunden ist, kann niemand sonst das Board finden.
- **Beschäftigt:** Läuft ein per Bluetooth gestartetes Skript, werden nur `ping`, `stop` und `reset`
  angenommen. Ein Skript lässt sich nur durch einen Reset des Boards stoppen. Dabei trennt das Board die
  Verbindung sauber und startet neu; `main.py` läuft danach wieder wie nach jedem Einschalten.
- **Viel Ausgabe:** Ausgaben werden gesammelt und in vollen Paketen gesendet. Druckt ein Skript schneller,
  als Bluetooth übertragen kann (etwa 5 KB/s), wird es gebremst. Stopp funktioniert trotzdem.
- **Task beenden:** In VS Code stoppt Strg+C im Task-Terminal das Programm sofort. „Terminate Task“ beendet
  nur `mpble`; das Board merkt erst nach einigen Sekunden, dass die Verbindung weg ist, und das Programm
  läuft weiter (dafür gibt es `--stop`).
- **Gemeinsame Ressourcen:** Ein per Bluetooth gestartetes Skript läuft neben `main.py` und nutzt
  denselben Speicher und dieselben Pins.
- **Fehlerhaftes Autorun:** Der Loader wartet nach dem Booten 5 s, bevor er das Autorun-Skript
  startet. In dieser Zeit entfernt `mpble autorun --clear` ein Skript, das hängt oder abstürzt.

**Das Board nicht in den Windows-Bluetooth-Einstellungen koppeln.** Das ist nicht nötig und stört die
Verbindung (siehe Fehlersuche).

### Sicherheit

Jeder in Bluetooth-Reichweite kann Code auf das Board laden und ausführen, außer du setzt eine PIN:

```powershell
"1234" | Set-Content pin.txt -NoNewline
mpble put pin.txt /ble_pin           # gilt nach dem nächsten Reset
$env:MCT_BLE_PIN = "1234"            # oder bei jedem Befehl --pin 1234 angeben
```

Die PIN wird unverschlüsselt übertragen und hält deshalb nur Gelegenheits-Nutzer fern. Entfernen mit
`mpble --pin 1234 rm /ble_pin`.

### Umgebungsvariablen und Dateien

| Name | Zweck |
| --- | --- |
| `MCT_BLE_ADDRESS` | Adresse des Boards, hat Vorrang vor der mit `link` gespeicherten |
| `MCT_BLE_PIN` | PIN des Boards |
| `MCT_PORT` | serieller Port für `mpble link` |
| `tools/.mpble-address` | die mit `link` gespeicherte Adresse (git-ignoriert) |
| `/ble_pin` auf dem Board | PIN-Datei |
| `/autorun` auf dem Board | Pfad des Skripts, das beim Start läuft |

## Fehlersuche

- **`Device 'MCT-ESP' not found`** – Der Loader läuft nicht (erst per USB übertragen), ein anderer
  Client ist verbunden oder das Board ist außer Reichweite. `mpble scan` zeigt, was sichtbar ist.
- **`The linked board … did not answer`** – Die mit `link` gespeicherte Adresse gehört zu einem anderen
  Board. Mit dem richtigen Board per USB `mpble link` ausführen oder `mpble unlink`.
- **`Cannot link COM4: …`** – siehe die Tabelle bei `link`. Bei `No USB serial device found` ist das Board
  nicht angeschlossen; bei `Several USB serial devices found` den Port mit `--port` wählen.
- **`Could not start notify … Unreachable` / „Windows has the board paired“** – Das Board wurde in
  Windows gekoppelt. Windows verschlüsselt dann jede Verbindung mit dem gespeicherten Schlüssel, der
  Loader speichert aber keine Schlüssel (MicroPython vergisst sie beim Reset), und die Verbindung bricht
  sofort ab. Lösung: `mpble unpair` oder `MCT-ESP` unter *Einstellungen → Bluetooth & Geräte* entfernen.
- **`mpremote: could not enter raw repl`** – Etwas blockiert `boot.py`, zum Beispiel ein blockierendes
  `ble_loader.serve()` dort (stattdessen `start()` verwenden). In einem seriellen Terminal Strg+C
  drücken und dann `mpremote connect COM4 resume ...` nutzen, das den Soft-Reset überspringt.
- **`boot.py` oder `lib/ble_loader.py` per Bluetooth kaputt gemacht** – Der Loader startet nicht mehr.
  Per USB reparieren. Ein kaputtes `main.py` ist harmlos.
- **Git Bash macht aus `/lib` einen Windows-Pfad** – PowerShell verwenden oder `export MSYS_NO_PATHCONV=1`.

## Aufbau

```
src/
  boot.py, main.py      Einstiegspunkte auf dem Board (boot.py startet den BLE-Loader, main.py ist dein Programm)
  lib/ble_loader.py     BLE-Dienst: Dateien hochladen, starten/stoppen, Autorun (läuft auf dem ESP32)
  lib/blink.py          LED-Beispiel
  test/                 Skripte zum Ausprobieren mit `mpble exec`
tools/ble_push.py       der Befehl `mpble` (läuft auf dem PC, nutzt bleak)
pyproject.toml          Abhängigkeiten und der Befehl `mpble`
typings/                MicroPython-Stubs für IntelliSense (nicht eingecheckt)
```

### Protokoll (zum Weiterentwickeln)

Eigener GATT-Dienst `7d3c0001-8b2e-4f6a-9c1d-5e7a2b4f9a10`: RX (schreiben) `…0002`, TX (benachrichtigen)
`…0003`. In beide Richtungen werden Frames der Form `[Länge u16 BE][Typ u8][Daten]` gesendet. Die Opcodes
und Antworttypen stehen am Anfang von `src/util/ble_loader.py` und sind in `tools/ble_push.py` gespiegelt.
