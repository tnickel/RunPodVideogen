"""CUDA preflight tests using only fake devices and fake driver calls."""

import errno
from types import SimpleNamespace

import pytest

from scripts import check_pod_cuda as check


@pytest.fixture
def fake_gpu(monkeypatch):
    opened = []
    closed = []
    driver = SimpleNamespace(cuInit=lambda flags: 0)
    monkeypatch.setattr(check.sys, "platform", "linux")
    monkeypatch.setattr(check.os, "open", lambda path, flags: opened.append((path, flags)) or 42)
    monkeypatch.setattr(check.os, "close", closed.append)
    monkeypatch.setattr(check.ctypes, "CDLL", lambda name: driver)
    return SimpleNamespace(driver=driver, opened=opened, closed=closed)


def test_success_reports_ready_and_immediately_closes_uvm(fake_gpu, capsys):
    def init(flags):
        assert flags == 0
        assert fake_gpu.closed == [42]
        return 0

    fake_gpu.driver.cuInit = init
    assert check.main() == 0
    output = capsys.readouterr()
    assert output.out.startswith("CUDA_READY:")
    assert output.err == ""
    assert fake_gpu.opened == [("/dev/nvidia-uvm", check.os.O_RDWR)]


def test_missing_libcuda_fails_clearly(fake_gpu, monkeypatch, capsys):
    def missing_library(name):
        assert name == "libcuda.so.1"
        raise OSError("cannot open shared object file")

    monkeypatch.setattr(check.ctypes, "CDLL", missing_library)
    assert check.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "libcuda.so.1 nicht nutzbar" in output.err
    assert fake_gpu.closed == [42]


@pytest.mark.parametrize("has_error_names", [True, False])
def test_cuinit_unknown_error_retains_code_and_optional_names(fake_gpu, capsys, has_error_names):
    fake_gpu.driver.cuInit = lambda flags: 999
    if has_error_names:
        def error_name(code, pointer):
            assert code == 999
            pointer._obj.value = b"CUDA_ERROR_UNKNOWN"
            return 0

        def error_string(code, pointer):
            pointer._obj.value = b"unknown error"
            return 0

        fake_gpu.driver.cuGetErrorName = error_name
        fake_gpu.driver.cuGetErrorString = error_string

    assert check.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "cuInit(0) = 999" in output.err
    if has_error_names:
        assert "CUDA_ERROR_UNKNOWN: unknown error" in output.err
    assert fake_gpu.closed == [42]


def test_uvm_io_failure_is_explicit_even_when_driver_also_fails(fake_gpu, monkeypatch, capsys):
    def io_failure(path, flags):
        raise OSError(errno.EIO, "Input/output error", path)

    monkeypatch.setattr(check.os, "open", io_failure)
    fake_gpu.driver.cuInit = lambda flags: 999
    assert check.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "EIO, errno 5" in output.err
    assert "Host-/Container-Ebene" in output.err
    assert "cuInit(0) = 999" in output.err
    assert fake_gpu.closed == []


def test_missing_device_does_not_create_device(fake_gpu, monkeypatch):
    def missing_device(path, flags):
        assert flags == check.os.O_RDWR
        assert flags & check.os.O_CREAT == 0
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)

    monkeypatch.setattr(check.os, "open", missing_device)
    ready, detail = check.check_cuda()
    assert ready is False
    assert "errno 2" in detail
    assert fake_gpu.closed == []


def test_non_linux_fails_without_accessing_devices(fake_gpu, monkeypatch):
    monkeypatch.setattr(check.sys, "platform", "win32")
    ready, detail = check.check_cuda()
    assert ready is False
    assert "Linux" in detail
    assert fake_gpu.opened == []
