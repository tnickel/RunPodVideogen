#!/usr/bin/env python3
"""Read-only Linux GPU preflight using the driver directly, without PyTorch."""

import ctypes
import errno
import os
import sys


def _cuda_error_text(driver, code):
    parts = []
    for name in ("cuGetErrorName", "cuGetErrorString"):
        try:
            function = getattr(driver, name)
            function.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)]
            function.restype = ctypes.c_int
            value = ctypes.c_char_p()
            if function(code, ctypes.byref(value)) == 0 and value.value:
                parts.append(value.value.decode("utf-8", errors="replace"))
        except (AttributeError, OSError):
            # Error names are supplementary; preserve the numeric result.
            pass
    return ": ".join(parts)


def check_cuda():
    """Return (ready, explanation); never create devices or change drivers."""
    if not sys.platform.startswith("linux"):
        return False, "Diese CUDA-Prüfung ist nur für Linux-Pods vorgesehen."

    errors = []
    try:
        descriptor = os.open("/dev/nvidia-uvm", os.O_RDWR)
        os.close(descriptor)
    except OSError as exc:
        if exc.errno == errno.EIO:
            errors.append(
                "/dev/nvidia-uvm meldet einen Ein-/Ausgabefehler (EIO, errno 5). "
                "Der Container kann das NVIDIA-UVM-Gerät nicht nutzen; "
                "GPU-Zugriff auf Host-/Container-Ebene bei RunPod prüfen."
            )
        else:
            errors.append(
                f"NVIDIA-UVM-Gerät /dev/nvidia-uvm nicht zugreifbar "
                f"(errno {exc.errno}: {exc.strerror})."
            )

    try:
        driver = ctypes.CDLL("libcuda.so.1")
        driver.cuInit.argtypes = [ctypes.c_uint]
        driver.cuInit.restype = ctypes.c_int
        code = driver.cuInit(0)
        if code != 0:
            detail = _cuda_error_text(driver, code)
            errors.append(
                f"CUDA-Treiberinitialisierung fehlgeschlagen: cuInit(0) = {code}"
                f"{': ' + detail if detail else ''}. "
                "Treiberzugriff, geladene libcuda und GPU-Sichtbarkeit prüfen."
            )
    except (OSError, AttributeError) as exc:
        errors.append(f"CUDA-Treiberbibliothek libcuda.so.1 nicht nutzbar: {exc}.")

    if errors:
        return False, "\n".join(errors)
    return True, "NVIDIA-UVM-Gerät zugreifbar; CUDA-Treiber initialisiert (cuInit=0)."


def main():
    ready, detail = check_cuda()
    if ready:
        print(f"CUDA_READY: {detail}")
        return 0
    print(f"CUDA_ERROR: {detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
