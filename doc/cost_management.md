# Kostenanzeige und Pod-Zustände

Die GPU-Auswahl konfiguriert die **nächste Pod-Erstellung**. Sie ändert weder die Hardware noch den Preis eines vorhandenen Pods. Das Kosten-Cockpit verwendet ausschließlich `costPerHr`, das RunPod für die aktuelle Pod-ID meldet.

## Preis während des Starts

- Beim Start wird der vorherige GPU-Preis verworfen.
- Bis RunPod einen gültigen Preis liefert, zeigt die Oberfläche „—“ und „Pod-Preis wird abgefragt“. Es gibt keinen erfundenen Standardpreis.
- Der Preis wird schon während der Bereitstellung abgefragt, bevor SSH bereit ist.
- Antworten eines früheren Startvorgangs dürfen die aktuelle Pod-ID, den Preis oder den Status nicht überschreiben.
- Tages-/Wochenwerte sind Hochrechnungen der **GPU-Rate**, keine Abrechnung.
- Speicher wird separat angezeigt. Die Sitzungsanzeige ist eine Schätzung, keine vollständige RunPod-Rechnung. Sie erfasst nicht zuverlässig die gesamte Bereitstellungszeit oder andere Ressourcen im Konto.

## Speicher

Der Launcher legt eine Container-Disk und eine Pod-Volume-Disk unter `/workspace` an. Das ist kein separat angelegtes Network Volume. Der frühere pauschale Netzwerkspeicher-Aufschlag von 0,07 $/GB/Monat passte deshalb nicht zu diesem Pod.

Die [RunPod-Preisübersicht](https://docs.runpod.io/pods/pricing) unterscheidet (abgerufen am 17.09.2026):

| Speicher | Pod läuft | Pod gestoppt |
| --- | --- | --- |
| Container-Disk | 0,10 $/GB/Monat | Keine Berechnung; Daten werden gelöscht |
| Pod-Volume-Disk | 0,10 $/GB/Monat | 0,20 $/GB/Monat |
| Network Volume unter 1 TB | 0,07 $/GB/Monat | 0,07 $/GB/Monat |

Maßgeblich sind die tatsächlich gebuchten Ressourcen und RunPods Abrechnung. Das Cockpit addiert keinen unbestätigten Speicherbetrag zur gemeldeten GPU-Rate.

## Starten, Stoppen und Löschen

| Zustand | Bedeutung im Studio |
| --- | --- |
| STARTING | Pod wird angefragt/bereitgestellt; ein gebuchter Pod kann bereits Kosten verursachen. |
| BOOTSTRAPPING | Software wird eingerichtet; noch keine Aussage, dass die WebUI bereit ist. |
| RUNNING | WanGP-WebUI wurde erfolgreich geprüft. Die GPU wird auch im Leerlauf berechnet. |
| STOPPED | GPU pausiert; das Pod-Volume bleibt erhalten und verursacht Speicherkosten. |
| TERMINATED / OFFLINE ohne Pod-ID | Für diesen Pod ist keine GPU oder Pod-Disk mehr reserviert. Andere Ressourcen im Konto sind davon unabhängig. |
| ERROR mit Pod-ID | Einrichtung oder Steuerung fehlgeschlagen; der Pod kann weiterhin Kosten verursachen. |

Stoppen/Löschen während der Einrichtung bricht den zugehörigen Startvorgang ab. Ein danach eintreffendes SSH- oder API-Ergebnis darf den Stop-/Löschstatus nicht wieder auf ERROR oder RUNNING setzen. Ein fehlgeschlagenes Stop-/Löschkommando behält die Pod-ID und meldet ERROR, damit erneutes Steuern möglich bleibt.

Beim Löschen gehen die Daten auf der Pod-Disk verloren. Bereits lokal synchronisierte Ausgaben bleiben erhalten. Ein neuer Pod benötigt eine neue Installation und gegebenenfalls Modell-Downloads; deren Dauer ist nicht fest.
