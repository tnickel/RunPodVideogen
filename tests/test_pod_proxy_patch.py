import ast
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts.patch_wan2gp_proxy import (
    KNOWN_ORIGINAL,
    main,
    patch_wan2gp_proxy,
    validate_public_origin,
)


PUBLIC_ORIGIN = "https://testpod0000a1b2c3-7860.proxy.runpod.net"
NEW_ORIGIN = "https://newpod123-7860.proxy.runpod.net"


@pytest.fixture()
def auth_file(tmp_path):
    path = tmp_path / "shared" / "authentication" / "web.py"
    path.parent.mkdir(parents=True)
    path.write_text("# keep this preamble\n" + KNOWN_ORIGINAL + "\nSENTINEL = 42\n", encoding="utf-8")
    return tmp_path, path


def load_same_origin(path):
    # Execute the actual patched function, without importing upstream dependencies.
    namespace = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    assert namespace["SENTINEL"] == 42
    return namespace["same_origin"]


def connection(origin, *, scheme="http", netloc="100.65.18.106:60047", extra_headers=None):
    headers = {} if origin is None else {"origin": origin}
    headers.update(extra_headers or {})
    return SimpleNamespace(headers=headers, url=SimpleNamespace(scheme=scheme, netloc=netloc))


@pytest.mark.parametrize("scheme", ["http", "ws"])
def test_public_origin_passes_with_rewritten_proxy_host(auth_file, scheme):
    wan_dir, path = auth_file
    assert patch_wan2gp_proxy(wan_dir, PUBLIC_ORIGIN) == "patched"
    assert load_same_origin(path)(connection(PUBLIC_ORIGIN, scheme=scheme)) is True


@pytest.mark.parametrize("origin", [
    "https://attacker.example",
    "https://otherpod-7860.proxy.runpod.net",
    "https://testpod0000a1b2c3-7860.proxy.runpod.net.attacker.example",
    "http://testpod0000a1b2c3-7860.proxy.runpod.net",
    "null",
    "",
])
@pytest.mark.parametrize("scheme", ["http", "ws"])
def test_foreign_origins_stay_blocked_even_with_spoofed_forwarded_headers(auth_file, origin, scheme):
    wan_dir, path = auth_file
    patch_wan2gp_proxy(wan_dir, PUBLIC_ORIGIN)
    request = connection(origin, scheme=scheme, extra_headers={
        "x-forwarded-host": origin.removeprefix("https://"),
        "x-forwarded-proto": "https",
        "forwarded": f'host="{origin}";proto=https',
    })
    assert load_same_origin(path)(request) is False


@pytest.mark.parametrize("scheme, origin", [
    ("http", "http://localhost:7860"),
    ("https", "https://localhost:7860"),
    ("ws", "http://localhost:7860"),
    ("wss", "https://localhost:7860"),
    ("http", None),
    ("ws", None),
])
def test_original_local_and_missing_origin_behavior_is_preserved(auth_file, scheme, origin):
    wan_dir, path = auth_file
    patch_wan2gp_proxy(wan_dir, PUBLIC_ORIGIN)
    assert load_same_origin(path)(connection(origin, scheme=scheme, netloc="localhost:7860")) is True


def test_reapply_is_idempotent_and_new_pod_replaces_old_origin(auth_file):
    wan_dir, path = auth_file
    original_bytes = path.read_bytes()
    assert patch_wan2gp_proxy(wan_dir, PUBLIC_ORIGIN) == "patched"
    first_bytes = path.read_bytes()
    first_mtime = path.stat().st_mtime_ns
    assert patch_wan2gp_proxy(wan_dir, PUBLIC_ORIGIN) == "unchanged"
    assert path.read_bytes() == first_bytes
    assert path.stat().st_mtime_ns == first_mtime
    assert patch_wan2gp_proxy(wan_dir, NEW_ORIGIN) == "patched"
    check = load_same_origin(path)
    assert check(connection(NEW_ORIGIN)) is True
    assert check(connection(PUBLIC_ORIGIN)) is False
    assert path.with_name("web.py.runpod.bak").read_bytes() == original_bytes
    assert path.read_text(encoding="utf-8").startswith("# keep this preamble\n")


@pytest.mark.parametrize("unknown_source", [
    "def same_origin(connection):\n    return True\n",
    KNOWN_ORIGINAL.replace("origin is None", 'origin == "null"'),
    "@custom_decorator\n" + KNOWN_ORIGINAL,
    KNOWN_ORIGINAL + "\n" + KNOWN_ORIGINAL,
    "def renamed_function(connection):\n    return True\n",
])
def test_unknown_upstream_is_refused_without_touching_files(auth_file, unknown_source):
    wan_dir, path = auth_file
    path.write_text(unknown_source, encoding="utf-8")
    original_bytes = path.read_bytes()
    with pytest.raises(ValueError, match="Refusing to patch"):
        patch_wan2gp_proxy(wan_dir, PUBLIC_ORIGIN)
    assert path.read_bytes() == original_bytes
    assert not path.with_name("web.py.runpod.bak").exists()


def test_modified_previous_patch_is_refused(auth_file):
    wan_dir, path = auth_file
    patch_wan2gp_proxy(wan_dir, PUBLIC_ORIGIN)
    path.write_text(path.read_text(encoding="utf-8").replace("origin is None", "origin is not None"), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unknown upstream"):
        patch_wan2gp_proxy(wan_dir, NEW_ORIGIN)
    assert path.read_bytes() == before


@pytest.mark.parametrize("origin", [
    "https://pod-0.proxy.runpod.net",
    "https://pod-65536.proxy.runpod.net",
    "http://pod-7860.proxy.runpod.net",
    "https://pod-7860.proxy.runpod.net/",
    "https://pod-7860.proxy.runpod.net/path",
    "https://pod-7860.proxy.runpod.net?x=1",
    "https://pod-7860.proxy.runpod.net#fragment",
    "https://user@pod-7860.proxy.runpod.net",
    "https://pod-7860.proxy.runpod.net:443",
    "https://*.proxy.runpod.net",
    "https://pod-7860.proxy.runpod.net\n",
    "https://POD-7860.proxy.runpod.net",
])
def test_invalid_origin_is_rejected_before_modifying_file(auth_file, origin):
    wan_dir, path = auth_file
    before = path.read_bytes()
    with pytest.raises(ValueError, match="public origin"):
        patch_wan2gp_proxy(wan_dir, origin)
    assert path.read_bytes() == before
    assert not path.with_name("web.py.runpod.bak").exists()


@pytest.mark.parametrize("origin", ["https://a-1.proxy.runpod.net", "https://z9-65535.proxy.runpod.net"])
def test_valid_port_boundaries(origin):
    assert validate_public_origin(origin) == origin


def test_old_wan_version_without_auth_file_is_skipped(tmp_path):
    assert patch_wan2gp_proxy(tmp_path, PUBLIC_ORIGIN) == "skipped"
    assert list(tmp_path.iterdir()) == []


def test_crlf_and_bom_are_preserved(auth_file):
    wan_dir, path = auth_file
    source = path.read_text(encoding="utf-8").replace("\n", "\r\n")
    original_bytes = source.encode("utf-8-sig")
    path.write_bytes(original_bytes)
    patch_wan2gp_proxy(wan_dir, PUBLIC_ORIGIN)
    result = path.read_bytes()
    assert result.startswith(b"\xef\xbb\xbf")
    assert b"\n" not in result.replace(b"\r\n", b"")
    ast.parse(result.decode("utf-8-sig"))
    assert path.with_name("web.py.runpod.bak").read_bytes() == original_bytes


def test_cli_accepts_explicit_wan_dir(auth_file, capsys):
    wan_dir, path = auth_file
    assert main(["--public-origin", PUBLIC_ORIGIN, "--wan-dir", str(wan_dir)]) == 0
    assert "patched" in capsys.readouterr().out
    assert load_same_origin(path)(connection(PUBLIC_ORIGIN)) is True


def test_cli_runs_from_stdin_outside_repository(auth_file, tmp_path):
    wan_dir, path = auth_file
    script = Path(__file__).resolve().parents[1] / "scripts" / "patch_wan2gp_proxy.py"
    result = subprocess.run(
        [sys.executable, "-", "--public-origin", PUBLIC_ORIGIN, "--wan-dir", str(wan_dir)],
        input=script.read_text(encoding="utf-8"), text=True, capture_output=True,
        cwd=tmp_path, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "patched" in result.stdout
    assert load_same_origin(path)(connection(PUBLIC_ORIGIN)) is True


def test_cli_unknown_source_fails_with_nonzero_exit(auth_file, capsys):
    wan_dir, path = auth_file
    path.write_text("def same_origin(connection):\n    return True\n", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        main(["--public-origin", PUBLIC_ORIGIN, "--wan-dir", str(wan_dir)])
    assert error.value.code == 1
    assert "unknown upstream" in capsys.readouterr().err
