#!/usr/bin/env bash
# ==============================================================================
# RunPod VideoGen Studio - Pod Bootstrap Script
# ==============================================================================
# Dieses Skript richtet Wan2GP auf einer frischen RunPod-Instanz (PyTorch Container)
# vollautomatisch ein und behebt alle bekannten Inkompatibilitäten (Triton, PyTorch).
#
# Ausführung auf dem Pod:
#   bash /workspace/bootstrap_pod.sh
# ==============================================================================

set -e

echo "=== [1/5] Erstelle Verzeichnisse & Umgebungsvariablen ==="
mkdir -p /workspace/.cache/huggingface
mkdir -p /workspace/Wan2GP/outputs
mkdir -p /workspace/Wan2GP/ckpts
export HF_HOME=/workspace/.cache/huggingface

echo "=== [2/5] Wan2GP Repository prüfen / klonen ==="
if [ ! -d "/workspace/Wan2GP/.git" ]; then
    echo "Klone Wan2GP nach /workspace/Wan2GP..."
    cd /workspace
    git clone https://github.com/deepbeepmeep/Wan2GP.git
    cd /workspace/Wan2GP
    echo "Installiere Abhängigkeiten..."
    pip install --no-cache-dir -r requirements.txt
else
    echo "Wan2GP ist bereits auf dem persistenten Volume vorhanden."
fi

echo "=== [3/5] Triton-Kompatibilitätspatch anwenden ==="
# Problem: Container nutzt Triton 3.0.0, Wan2GP nutzt do_not_specialize_on_alignment (erst ab Triton 3.2)
python3 -c "
import triton
p = triton.__file__
with open(p, 'r') as f:
    s = f.read()
if '_compatible_jit' not in s:
    wrapper = '''
_orig_jit = jit
def _compatible_jit(*args, **kwargs):
    kwargs.pop('do_not_specialize_on_alignment', None)
    return _orig_jit(*args, **kwargs)
jit = _compatible_jit
'''
    with open(p, 'a') as f:
        f.write('\\n' + wrapper + '\\n')
    print('Triton-Patch erfolgreich angewendet.')
else:
    print('Triton-Patch bereits vorhanden.')
" 2>/dev/null || true

echo "=== [4/5] PyTorch 2.5 torch.nn.Buffer Polyfill injizieren ==="
# Problem: Container nutzt PyTorch 2.4.1, mmaudio (MiniMax H3) benötigt torch.nn.Buffer (erst ab PyTorch 2.5)
python3 -c "
wgp_path = '/workspace/Wan2GP/wgp.py'
try:
    with open(wgp_path, 'r') as f:
        c = f.read()
    if 'class _Buffer' not in c:
        polyfill = '''# --- Compatibility Polyfill for torch.nn.Buffer (PyTorch < 2.5) ---
import torch
import torch.nn as nn
if not hasattr(nn, \"Buffer\"):
    class _Buffer(torch.Tensor):
        def __new__(cls, data=None, *, persistent=True):
            if data is None: data = torch.empty(0)
            t = data.detach().as_subclass(cls) if isinstance(data, torch.Tensor) else torch.tensor(data).as_subclass(cls)
            t.persistent = persistent
            return t
    nn.Buffer = _Buffer
    _orig_module_setattr = nn.Module.__setattr__
    def _compatible_module_setattr(self, name, value):
        if isinstance(value, _Buffer):
            self.register_buffer(name, value, persistent=getattr(value, \"persistent\", True))
        else:
            _orig_module_setattr(self, name, value)
    nn.Module.__setattr__ = _compatible_module_setattr
# ------------------------------------------------------------------
'''
        with open(wgp_path, 'w') as f:
            f.write(polyfill + '\\n' + c)
        print('torch.nn.Buffer Polyfill erfolgreich in wgp.py injiziert.')
    else:
        print('torch.nn.Buffer Polyfill bereits vorhanden.')
except Exception as e:
    print('Fehler bei Polyfill-Injektion:', e)
" 2>/dev/null || true

echo "=== [5/5] Starte Wan2GP WebUI ==="
if pgrep -f "wgp.py" > /dev/null; then
    echo "Wan2GP läuft bereits (PID: \$(pgrep -f 'wgp.py'))."
else
    cd /workspace/Wan2GP
    setsid python wgp.py --server-port 7860 --server-name 0.0.0.0 </dev/null > /workspace/wan2gp.log 2>&1 &
    sleep 3
    echo "Wan2GP gestartet auf Port 7860."
fi

echo "=== Bootstrap abgeschlossen! ==="
