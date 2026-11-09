"""測試共用 Fixture：提供真實 TCP + Paramiko SSH/SFTP 本地測試伺服器，模擬遠端 Linux 檔案系統。"""

from __future__ import annotations

import errno
import os
import socket
import stat
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paramiko
import pytest


class _LocalSFTPHandle(paramiko.SFTPHandle):
    def __init__(
        self,
        flags: int,
        real_path: Path,
        virtual_path: str,
        on_write_hook: Optional[Callable[[str, bytes], Optional[int]]] = None,
    ) -> None:
        super().__init__(flags)
        self.real_path = real_path
        self.virtual_path = virtual_path
        self._on_write_hook = on_write_hook
        self._fp = open(real_path, "wb")

    def write(self, offset: int, data: bytes) -> int:
        if self._on_write_hook is not None:
            hook_res = self._on_write_hook(self.virtual_path, data)
            if hook_res is not None:
                return hook_res
        try:
            self._fp.seek(offset)
            self._fp.write(data)
            self._fp.flush()
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno or errno.EIO)

    def close(self) -> int:
        try:
            self._fp.close()
        except Exception:
            pass
        return super().close()

    def stat(self) -> paramiko.SFTPAttributes:
        return paramiko.SFTPAttributes.from_stat(os.stat(self.real_path))


class _LocalSFTPServerInterface(paramiko.SFTPServerInterface):
    def __init__(self, server: "_TestSSHServer", *args, **kwargs) -> None:
        super().__init__(server, *args, **kwargs)
        self.ssh_server = server

    def _map_path(self, sftp_path: str) -> Path:
        norm = sftp_path.lstrip("/")
        if not norm:
            return self.ssh_server.root_dir
        return self.ssh_server.root_dir / Path(*norm.split("/"))

    def stat(self, path: str) -> paramiko.SFTPAttributes | int:
        if path in self.ssh_server.permission_denied_paths:
            return paramiko.SFTP_PERMISSION_DENIED
        real = self._map_path(path)
        if not real.exists():
            return paramiko.SFTP_NO_SUCH_FILE
        st = os.stat(real)
        return paramiko.SFTPAttributes.from_stat(st)

    def lstat(self, path: str) -> paramiko.SFTPAttributes | int:
        return self.stat(path)

    def open(self, path: str, flags: int, attr: paramiko.SFTPAttributes) -> paramiko.SFTPHandle | int:
        if path in self.ssh_server.permission_denied_paths:
            return paramiko.SFTP_PERMISSION_DENIED
        if path in self.ssh_server.fail_open_paths:
            return paramiko.SFTP_FAILURE

        real = self._map_path(path)
        if not real.parent.exists():
            return paramiko.SFTP_NO_SUCH_FILE
        if real.exists() and real.is_dir():
            return paramiko.SFTP_FAILURE

        try:
            return _LocalSFTPHandle(
                flags=flags,
                real_path=real,
                virtual_path=path,
                on_write_hook=self.ssh_server.on_write_hook,
            )
        except PermissionError:
            return paramiko.SFTP_PERMISSION_DENIED
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno or errno.EIO)


class _TestSSHServer(paramiko.ServerInterface):
    def __init__(
        self,
        root_dir: Path,
        valid_user: str,
        valid_password: str,
        permission_denied_paths: set[str],
        fail_open_paths: set[str],
        on_write_hook: Optional[Callable[[str, bytes], Optional[int]]],
    ) -> None:
        self.root_dir = root_dir
        self.valid_user = valid_user
        self.valid_password = valid_password
        self.permission_denied_paths = permission_denied_paths
        self.fail_open_paths = fail_open_paths
        self.on_write_hook = on_write_hook

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_auth_password(self, username: str, password: str) -> int:
        if username == self.valid_user and password == self.valid_password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED


class EmbeddedSftpTestServer:
    """本機 TCP 真實 SSH/SFTP 測試伺服器，供 AC-01 ~ AC-28 自動化測試使用。"""

    def __init__(self, root_dir: Path, username: str = "deploy", password: str = "test-pass-123") -> None:
        self.root_dir = root_dir
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.username = username
        self.password = password
        self.host = "127.0.0.1"
        self.host_key = paramiko.RSAKey.generate(2048)

        self.permission_denied_paths: set[str] = set()
        self.fail_open_paths: set[str] = set()
        self.on_write_hook: Optional[Callable[[str, bytes], Optional[int]]] = None
        self.Executed_commands: list[str] = []

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, 0))
        self._sock.listen(16)
        self.port: int = self._sock.getsockname()[1]

        self._running = True
        self._active_transports: list[paramiko.Transport] = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while self._running:
            try:
                client_sock, _ = self._sock.accept()
            except OSError:
                break
            t = threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True)
            t.start()

    def _handle_client(self, client_sock: socket.socket) -> None:
        transport = paramiko.Transport(client_sock)
        transport.add_server_key(self.host_key)
        transport.set_subsystem_handler("sftp", paramiko.SFTPServer, _LocalSFTPServerInterface)
        server_iface = _TestSSHServer(
            root_dir=self.root_dir,
            valid_user=self.username,
            valid_password=self.password,
            permission_denied_paths=self.permission_denied_paths,
            fail_open_paths=self.fail_open_paths,
            on_write_hook=self.on_write_hook,
        )
        self._active_transports.append(transport)
        try:
            transport.start_server(server=server_iface)
            while transport.is_active() and self._running:
                import time

                time.sleep(0.05)
        except Exception:
            pass
        finally:
            try:
                transport.close()
            except Exception:
                pass

    def drop_all_active_connections(self) -> None:
        for tr in list(self._active_transports):
            try:
                tr.close()
            except Exception:
                pass

    def stop(self) -> None:
        self._running = False
        try:
            self._sock.close()
        except Exception:
            pass
        self.drop_all_active_connections()

    def remote_file_path(self, remote_path: str) -> Path:
        norm = remote_path.lstrip("/")
        return self.root_dir / Path(*norm.split("/"))


@pytest.fixture
def sftp_server(tmp_path: Path):
    remote_root = tmp_path / "remote_linux_root"
    srv = EmbeddedSftpTestServer(remote_root)
    yield srv
    srv.stop()
