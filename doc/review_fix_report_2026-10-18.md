# Reparaturbericht zur Nachprüfung des Projektcodes

Stand: 18. Oktober 2026

Projekt: `D:\AntiGravitySoftware\GitWorkspace\RunPodVideogen`

Die verbliebenen Befunde der letzten Nachprüfung sind behoben. Zusätzlich wurde eine bei der Integration reproduzierte Löschlücke zwischen Hashprüfung und Löschbefehl geschlossen. Der abschließende Python-Lauf ergibt **266 bestandene Tests, einen übersprungenen Test und keinen Fehler**; alle **42 JavaScript-Tests bestehen**.

Die Änderungen bauen auf dem vorhandenen, bereits uncommitteten Reparaturstand auf. Die zuvor implementierten Korrekturen für Kostenverbuchung, Konfigurationsmaskierung, verspätete Buchungsantworten und den vom Video-Sync getrennten Watchdog bleiben erhalten. Dieser Bericht ist die Übergabe an die weiterarbeitende KI.

## Behobene Fehler

### Vorhandene Pods bleiben nach einem Preflightfehler erhalten

In `backend/orchestrator.py` setzt ein erfolgreiches datenerhaltendes Pausieren den Status auf `STOPPED`. Aufgelaufene Sessionkosten werden verbucht; der Preflightfehler bleibt im Detail beziehungsweise Startfortschritt erkennbar. Der Hintergrunddienst erkennt außerdem ein von RunPod bestätigtes `EXITED`, beendet die Kostenmessung und führt keine spätere automatische Terminierung dieses gestoppten Pods aus.

Weiterhin laufende Pods im Fehlerstatus unterliegen weiterhin dem konfigurierten Auto-Shutdown. Regressionstests prüfen SSH-, CUDA- und Netzwerkfehler bei bestehenden Pods mit anschließend überschrittenem Abschalttimer im Modus `terminate` sowie tatsächlich laufende Fehler-Pods unter beiden Abschaltmodi.

### Dateinamenskollisionen überschreiben keine vorhandenen Videos

In `backend/ssh_client.py` wird der Eigentümer eines lokalen Namens anhand der verifizierten Zuordnung berücksichtigt. Das gilt auch über mehrere Sync-Durchläufe: Wird zuerst `clip:a.mp4` geladen und erscheint später zusätzlich `clip_a.mp4`, bleiben beide Dateien erhalten. Tests decken gleiche, unterschiedliche und identische Inhalte sowie Windows-Groß-/Kleinschreibung ab.

Neue Dateien werden im selben Verzeichnis per Hardlink aus einer vollständig geladenen temporären Datei veröffentlicht. Eine zwischenzeitlich belegte Zieldatei wird dabei nicht überschrieben. Änderungen einer bekannten Remote-Datei mit gleicher Größe und neuer Mtime erzeugen eine zusätzliche lokale Version.

### Alte Zuordnungen berechtigen nicht zum Löschen auf dem Pod

Die bisherige `.name_map.json` allein gilt nicht mehr als Herkunftsnachweis. Eine neue `.name_map_verified.json` speichert für tatsächlich geladene beziehungsweise durch Inhaltsvergleich bestätigte Dateien Remote-Namen, SHA-256, Größe und Remote-Mtime.

Alte Zuordnungen werden durch erneuten Download und Vergleich des vollständigen Inhalts migriert. Gleich große Dateien mit unterschiedlichem Inhalt werden nicht verwechselt; vorhandene lokale Daten bleiben erhalten. Ohne verifizierte Zuordnung löscht die Galerie nur die lokale Datei und meldet, dass die Remote-Datei erhalten bleibt.

### Fehler bei HTTP führen nicht zum Verwerfen eines Pods

`_preflight_network_speed()` in `backend/orchestrator.py` verwendet Curl mit `--fail` und erhält weiterhin den tatsächlichen Exitcode. HTTP-Fehler gelten damit als nicht ausführbare Messung und liefern `None`, ebenso wie DNS-, TLS-, Timeout- und ungültige Messergebnisse. Nur eine erfolgreiche gültige Messung unter dem Grenzwert kann den Netzwerk-Preflight ablehnen.

Ein echter lokaler HTTP-503-Test prüft den Curl-Fehlercode 22 und das Überspringen der Messung.

### SSH und SFTP warten auch vor der Befehlsannahme nicht unbegrenzt

`backend/ssh_client.py` begrenzt nun auch die Wartephase innerhalb von Paramikos Exec- beziehungsweise SFTP-Subsystem-Anforderung. Ein kurzlebiger Deadline-Timer schließt bei Ablauf die besitzende Verbindung und weckt damit die internen ACK-Wartevorgänge. Die Timer werden beendet und gejoint; es werden keine blockierten Befehlsworker angesammelt.

Beide Ausgabeströme werden weiterhin während der Befehlsausführung geleert. SFTP verwendet 30 Sekunden für einzelne I/O- beziehungsweise Initialisierungsoperationen und zusätzlich 15 Minuten als absolute Frist pro Download. Unvollständige Downloads werden nicht als fertige Dateien veröffentlicht. Tests verwenden unter anderem echte lokale Paramiko-Channels ohne Netzwerktransport für fehlende Bestätigungen und blockierenden Empfang.

### API Schlüssel bleiben auch aus Tracebacks entfernt

`backend/runpod_client.py` konstruiert eine bereinigte Request-Ausnahme und hebt sie erst außerhalb des ursprünglichen Exception-Handlers an. Die unbereinigte Ausnahme wird weder als `__cause__` noch als `__context__` weitergegeben.

Die Tests prüfen neben `str(exception)` den formatierten vollständigen Traceback sowie beide Exception-Verknüpfungen. Die zentrale Maskierung von GET- und POST-Konfigurationsantworten bleibt bestehen.

### Guthabeneingaben bleiben bis Speichern oder Abbrechen erhalten

`static/js/app.js` verwendet einen expliziten Änderungszustand auf Basis tatsächlicher Eingabeereignisse. Ein bearbeitetes Guthabenfeld bleibt auch nach Fokuswechsel, Statuspoll und Kontostandsaktualisierung erhalten. Bloßer Fokus und das Speichern anderer Einstellungen führen nicht zu einer Guthabenkorrektur. Abbrechen und erneutes Öffnen verwerfen die alte Eingabe.

Manuelle Werte müssen vollständig numerisch, endlich und nicht negativ sein. Fehlgeschlagene Konfigurations- oder Korrekturantworten werden angezeigt; Dialog und ausstehende Eingabe bleiben erhalten. Mehrfaches gleichzeitiges Speichern wird verhindert.

### Guthabenkorrekturen verwenden einen konsistenten Kostenstand

In `server.py` werden historische und aktive Kosten für Statusabfragen gemeinsam unter `orchestrator.lock` gelesen. Die manuelle Neukalibrierung liest nach dem Netzwerkzugriff einen aktuellen Kostenstand und speichert die neue Basis unter demselben Lock. Ein gleichzeitiger Stop kann dadurch keinen gemischten Stand aus alter Historie und bereits zurückgesetzten Sessionkosten erzeugen.

Die Rechnung bleibt:

```text
angezeigtes Guthaben = account_balance - spent_total - accumulated_cost
neue Basis bei manueller Korrektur = Eingabe + spent_total + accumulated_cost
```

Netzwerkzugriffe halten diesen Lock nicht. Die HTTP-Schnittstelle weist ungültige Korrekturbeträge vor einem Netzwerkzugriff zurück. Tests reproduzieren eine Kostenverbuchung zwischen anfänglichem Konfigurationslesen und Korrektur sowie Kostenänderungen während der Accountabfrage.

### Löschen auf dem Pod schützt inzwischen ersetzte Dateien

Bei der Integration ließ sich ein weiterer Fehler reproduzieren: Zwei separate SSH-Aufrufe für `sha256sum` und `rm` konnten eine nach der Prüfung neu entstandene Datei löschen.

`SSHManager.delete_verified_file()` führt deshalb einen einzigen Remote-Pythonvorgang aus. Er verschiebt den zu prüfenden Verzeichniseintrag atomar auf einen eindeutigen Quarantänepfad im selben Verzeichnis, prüft die verschobene reguläre Datei auf Inhalt und Änderungen während des Lesens und entfernt nur das geprüfte Original. Eine anschließend am ursprünglichen Pfad neu angelegte Datei bleibt erhalten.

Bei abweichendem Inhalt oder Fehler wird das verschobene Original ohne Überschreiben wiederhergestellt. Ist der ursprüngliche Name inzwischen belegt, bleibt eine sichtbare Sicherung mit dem Präfix `preserved-` und der Medienendung erhalten. Die Tests führen denselben Pythoncode lokal aus und simulieren Pfadaustausch sowie Fehler beim Hashen, Löschen, Wiederherstellen und Sichern. Apostrophe, Semikolons und Zeilenumbrüche werden als Argumente übergeben; der Heredoc erlaubt keine Shell-Expansion.

## Schnittstellen und Migration

`backend/ssh_client.py` stellt für `PodOrchestrator.delete_video()` bereit:

```python
get_verified_identity(local_name, local_path)
# dict mit remote, sha256, size, remote_mtime oder None

remove_name_mapping(local_name)
# Entfernt normale und verifizierte Zuordnung unter gemeinsamem Lock.

SSHManager.delete_verified_file(remote_path, sha256)
# True nur bei bestätigter Löschung des geprüften Originals.
```

Die normale Namenszuordnung bleibt im bisherigen Format `lokaler Name -> Remote-Name`. Die zusätzliche Datei unter `outputs/` verwendet dieses Schema:

```json
{
  "version": 1,
  "entries": {
    "clip_a.mp4": {
      "remote": "clip:a.mp4",
      "sha256": "<SHA256 des bestätigten Inhalts>",
      "size": 12345,
      "remote_mtime": 1700000000
    }
  }
}
```

Jeder Metadatenspeicher wird atomar geschrieben; für eine Löschberechtigung müssen beide übereinstimmen. Nicht verifizierte Altdateien können einmalig einen erneuten vollständigen Download erfordern. Ohne Herkunftsnachweis lokal gelöschte Dateien können erneut synchronisiert werden, solange ihr Remote-Original erhalten bleibt.

Eine Prozessunterbrechung nach dem atomaren Verschieben kann eine `.runpod-delete-*.quarantine` im Remote-Ausgabeordner hinterlassen. Diese enthält die zur Wiederherstellung erhaltenen Originaldaten und darf nicht pauschal gelöscht werden. Nicht bestätigte Remote-Löschungen melden diesen Wiederherstellungspfad; ein normal abgeschlossener erfolgreicher Vorgang hinterlässt keine Quarantänedatei.

`POST /api/config` übernimmt weiterhin keinen Kontostand als neue Berechnungsbasis. Explizite manuelle Korrekturen erfolgen über `/api/account/refresh?set_balance=...`; normale Einstellungsänderungen senden keinen Guthabenwert.

## Testergebnisse

Alle Befehle wurden im Projektstamm ausgeführt.

| Prüfung | Ergebnis |
| --- | --- |
| `python -m pytest tests/ -q --tb=short` | 266 bestanden, 1 übersprungen, 0 fehlgeschlagen |
| Alle vier JavaScript-Testdateien mit `node --test` | 42 bestanden, 0 fehlgeschlagen |
| `python -m compileall -q backend server.py` | erfolgreich |
| `node --check static/js/app.js` | erfolgreich |
| `git diff --check` | erfolgreich |

Der übersprungene Test betrifft das Anlegen eines Symlinks ohne die dafür erforderliche Windows-Berechtigung. Die Python-Suite meldet außerdem eine bestehende Deprecation-Warnung zur Verwendung von `httpx` im Starlette-TestClient.

Der vollständige JavaScript-Aufruf lautet:

```text
node --test tests/gallery_delete_and_balance.test.cjs tests/gpu_comparison.test.cjs tests/pod_cost_display.test.cjs tests/startup_progress_display.test.cjs
```

Die reparierten DOM-Tests treiben echte Handler aus `app.js` mit einem an `index.html` orientierten Test-DOM. Sie prüfen unter anderem den tatsächlichen Galerie-Neuaufbau nach HTTP- und Netzwerkfehlern, erneute Zweiklickbestätigung, Fokuswechsel, unveränderte Guthabenfelder, Abbrechen und fehlerhafte Speicherantworten.

Die Prüfungen verwenden isolierte temporäre Konfigurationen und simulierte Cloudzugriffe. Es wurden keine produktiven Pod-Aktionen, echten SSH-Verbindungen oder echten RunPod-Buchungen ausgeführt. Der HTTP-Preflighttest verwendet ausschließlich einen lokalen Testserver.

## Übergabe an die andere KI

Die bekannten Befunde der letzten Nachprüfung sind erledigt; die Testsuite enthält keine fehlgeschlagenen Tests.

Die Änderungen liegen uncommittet im bestehenden Checkout. Vorhandene Arbeiten in `backend/config_manager.py` und `tests/conftest.py` wurden beibehalten. Die vorhandenen Diagnoseskripte `_diag16.py`, `_diag16b.py` und `_probe_api.py` wurden nicht verändert.

Für weitere Änderungen besonders beachten: Alt-Mappings und Dateigröße allein sind keine Löschberechtigung; Hashprüfung und Pfadlöschung dürfen nicht wieder auf zwei unabhängige SSH-Aufrufe verteilt werden. Kostenhistorie und aktive Sessionkosten müssen unter demselben Lifecycle-Lock gelesen beziehungsweise verschoben werden. Ein reiner Fokusvergleich ersetzt den Änderungszustand im Guthabenformular nicht.
