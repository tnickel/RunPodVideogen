/*
 * Runpod's published FLUX.1-dev image benchmark, retrieved 2026-09-17.
 * Source/query and limitations: doc/gpu_benchmarks.md.
 * Values are seconds per image, not WanGP or MiniMax H3 video timings.
 */
window.GpuBenchmarkData = Object.freeze({
    label: 'FLUX-Tempo (Orientierung)',
    description: 'Runpod: FLUX.1-dev, 1024 × 1024 Pixel, 28 Schritte; Sekunden pro Bild.',
    source_url: 'https://www.runpod.io/gpu-compare/a40-vs-l40s',
    data_url: 'https://benchmarks.getrunpod.io/',
    measured_at: null,
    retrieved_at: '2026-09-17',
    note: 'Bild-Benchmark zur Orientierung; keine gemessene WanGP- oder MiniMax-H3-Renderzeit. Modell, VRAM und Einstellungen können die Rangfolge ändern. Datentyp und Quantisierung sind in der Quelle nicht angegeben.',
    seconds: Object.freeze({
        'NVIDIA A40': 32.79,
        'NVIDIA RTX A6000': 25.61,
        'NVIDIA L40': 23.98,
        'NVIDIA RTX 6000 Ada Generation': 21.34,
        'NVIDIA L40S': 15.45,
        'NVIDIA A100 80GB PCIe': 14.20,
        'NVIDIA A100-SXM4-80GB': 12.72,
        'NVIDIA H100 PCIe': 9.43,
        'NVIDIA H100 NVL': 9.32,
        'NVIDIA RTX PRO 6000 Blackwell Server Edition': 8.32,
        'NVIDIA RTX PRO 6000 Blackwell Server Edition MIG 2g.48gb': 16.95,
        'NVIDIA H100 80GB HBM3': 5.86,
        'NVIDIA H200': 5.66,
        'NVIDIA B300 SXM6 AC': 4.01,
        'NVIDIA B200': 3.25,
    }),
});
