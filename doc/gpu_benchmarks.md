# GPU-Geschwindigkeitsvergleich

Die GPU-Auswahl verwendet einen veröffentlichten Runpod-Bildbenchmark als Orientierung.
Die Werte sind **keine Messungen der Videogenerierung mit WanGP oder MiniMax H3**.
Es werden keine MiniMax-Renderzeiten aus diesen Werten hochgerechnet.

## Quelle und Test

- Anbieter und Primärquelle: [Runpod GPU Benchmarks](https://www.runpod.io/gpu-compare/a40-vs-l40s).
- Daten: `https://benchmarks.getrunpod.io/`; Schema: `https://benchmarks.getrunpod.io/schema`.
- Abgerufen am: 2026-09-17.
- Modell: `black-forest-labs/flux.1-dev`.
- Auflösung: 1024 × 1024 Pixel.
- Schritte: 28.
- Metrik: `seconds_per_image` (kleiner ist schneller).

Die öffentliche Vergleichsseite beschreibt Bildbenchmarks mit Hugging Face Diffusers
und nennt pauschal Mai 2025. Der aktuelle API-Datensatz enthält auch später ergänzte
GPU-Typen, aber keine Messdaten pro Datensatz. Daher ist `measured_at` ausdrücklich
unbekannt (`null`); das Abrufdatum wird separat gespeichert.

Die Quelle nennt weder Datentyp noch Quantisierung, Diffusers-Version, Warm-up oder
Batchgröße der einzelnen Bildmessungen (die API liefert `batchSize: null`). Identisch
sind die veröffentlichten Modell-, Auflösungs-, Schritt- und Metrikparameter. Eine
vollständig reproduzierbare, auf allen Karten identische numerische Konfiguration
ist dadurch nicht nachgewiesen. Die UI darf insbesondere kein „BF16-Benchmark“
oder eine garantierte Beschleunigung von MiniMax H3 daraus machen.

Die Daten lassen sich ohne API-Schlüssel aus der öffentlichen Benchmark-API abrufen:

```json
{
  "benchmarkType": "image",
  "gpus": ["NVIDIA A40", "NVIDIA L40S"],
  "modelName": "black-forest-labs/flux.1-dev",
  "metric": "seconds_per_image",
  "stepCount": 28,
  "width": 1024,
  "height": 1024
}
```

Diese JSON-Abfrage wird per POST mit `Content-Type: application/json` an den
Datenendpunkt gesendet. Die vollständige Liste für `gpus` stammt aus
`data.image.gpus` des Schemas. Die Schlüssel in
`static/js/gpu-performance.js` entsprechen exakt den `gpuModel`-Werten der Antwort.
Eine nicht exakt passende GPU-ID erhält keinen vermuteten Alias-Wert.

## Berechnung

Für eine gewählte Referenzkarte mit Zeit `t_ref` und eine
Vergleichskarte mit Zeit `t_gpu` und Stundenpreis `p_gpu`:

```text
Tempo-Faktor = t_ref / t_gpu
Effizienz = Tempo-Faktor / p_gpu
```

Ein Tempo-Faktor von 2 bedeutet doppelte Geschwindigkeit **im genannten
FLUX-Bildbenchmark**. Die Effizienz zeigt Referenztempo pro GPU-Stundenpreis in
US-Dollar; höher ist besser. Das ist kein auf 1 normierter Vergleich mit dem Preis
der Referenzkarte. Beispielsweise ergeben 2× Tempo bei 0,80 $/h eine Effizienz von
2,5. So bleibt der Vergleich auch möglich, wenn die Referenzkarte in der gewählten
Cloud keinen Preis hat. Die Stundenpreise kommen aus den aktuellen GPU-Angeboten
oder ausdrücklich gekennzeichneten Katalogpreisen; Kosten für Speicher, Einrichtung, Downloads und
Leerlauf sind nicht Teil dieses GPU-Preisvergleichs. Bei fehlenden Zeiten oder
fehlenden beziehungsweise nicht positiven Preisen bleibt der jeweilige Wert leer.

## Lücken und Grenzen

Für RTX 3090, RTX 4090, RTX 5090 und weitere Karten liefert diese konkrete
FLUX-Abfrage keine Messwerte. Die Auswahl zeigt dafür „— / Kein Messwert“ an. Es werden keine
SDXL-Werte, theoretischen TFLOPS, andere Quantisierungen oder frei geschätzten
Faktoren eingesetzt, um diese Lücken zu füllen. Das Fehlen einer Messung ist kein
Nachweis, dass die betreffende GPU ein anderes Modell nicht ausführen kann.

Die tatsächliche MiniMax-H3-Geschwindigkeit hängt unter anderem von Modellvariante,
Quantisierung, Auflösung, Videolänge, VRAM und nötigem CPU-Offloading ab. Weniger
VRAM ist ein Speicherhinweis, keine pauschale Mindestanforderung. Belastbare
MiniMax-Faktoren erfordern denselben vollständigen Video-Workflow auf den
verglichenen GPUs.
