"""Cấu hình crawler Profile muasamcong — kết nối DB, danh sách nguồn."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

HOST = "https://muasamcong.mpi.gov.vn"

# 5 nguồn cần crawl mỗi ngày
SOURCES: dict[str, dict] = {
    "contractors": {
        "name": "Nhà thầu được phê duyệt",
        "url": f"{HOST}/web/guest/approved-contractors-list",
    },
    "investors": {
        "name": "Nhà đầu tư được phê duyệt",
        "url": f"{HOST}/web/guest/investor-approved-list",
    },
    "foreign_contractors": {
        "name": "Nhà thầu nước ngoài thắng thầu",
        "url": f"{HOST}/web/guest/foreign-contractor-winning-bid-vn",
    },
    "investors_v2": {
        "name": "Nhà đầu tư được phê duyệt (v2)",
        "url": f"{HOST}/web/guest/investors-approval-v2",
    },
    "bid_solicitors": {
        "name": "Bên mời thầu được phê duyệt",
        "url": f"{HOST}/web/guest/bid-solicitor-approval",
    },
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

NAV_TIMEOUT_MS = 90_000
REQUEST_DELAY  = 0.5     # giây nghỉ giữa các trang

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PROFILE_DATA_DIR", BASE_DIR / "data"))

# --- Bí mật / kết nối ---
# Tìm file secrets: trong thư mục project trước, rồi thư mục cha (C:\Bidpro)
def _find_secrets() -> Path:
    for p in [
        BASE_DIR / "secrets.local.txt",
        BASE_DIR.parent / "secrets.local.txt",
    ]:
        if p.exists():
            return p
    return BASE_DIR / "secrets.local.txt"

SECRETS_FILE = _find_secrets()

# --- SSH tunnel + PostgreSQL (giống Crawl project) ---

_TUNNEL = None
_TUNNEL_LOCK = threading.Lock()


class _SSHTunnel:
    """Forwarder cục bộ qua SSH (paramiko) + tự kết nối lại khi rớt."""

    def __init__(self, ssh_host, ssh_port, user, password, remote_host, remote_port):
        import socket
        self._cfg = (ssh_host, int(ssh_port), user, password)
        self._remote = (remote_host, int(remote_port))
        self._t = None
        self._tlock = threading.Lock()
        self._stop = False
        self._connect()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(64)
        self.local_bind_port = self._srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()
        threading.Thread(target=self._monitor, daemon=True).start()

    def _connect(self):
        import paramiko
        with self._tlock:
            if self._t is not None and self._t.is_active():
                return self._t
            try:
                if self._t is not None:
                    self._t.close()
            except Exception:
                pass
            host, port, user, pw = self._cfg
            t = paramiko.Transport((host, port))
            t.set_keepalive(20)
            t.connect(username=user, password=pw)
            self._t = t
            return t

    def _ensure(self):
        t = self._t
        if t is not None and t.is_active():
            return t
        return self._connect()

    def _monitor(self):
        import time
        while not self._stop:
            time.sleep(20)
            try:
                if self._t is None or not self._t.is_active():
                    self._connect()
            except Exception:
                pass

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(target=self._pipe, args=(conn,), daemon=True).start()

    def _open_channel(self, conn):
        for attempt in (1, 2):
            try:
                return self._ensure().open_channel("direct-tcpip", self._remote,
                                                   conn.getsockname())
            except Exception:
                if attempt == 2:
                    return None
                try:
                    self._connect()
                except Exception:
                    return None
        return None

    def _pipe(self, conn):
        import select
        chan = self._open_channel(conn)
        if chan is None:
            conn.close()
            return
        try:
            while True:
                r, _, _ = select.select([conn, chan], [], [], 60)
                if conn in r:
                    d = conn.recv(8192)
                    if not d:
                        break
                    chan.sendall(d)
                if chan in r:
                    d = chan.recv(8192)
                    if not d:
                        break
                    conn.sendall(d)
        except Exception:
            pass
        finally:
            try:
                chan.close()
            except Exception:
                pass
            conn.close()

    @property
    def is_active(self):
        return (not self._stop) and (self._srv is not None)


def _load_secrets() -> dict:
    d: dict[str, str] = {}
    try:
        if SECRETS_FILE.exists():
            for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    except Exception:
        pass
    return d


def _ssh_tunnel_local():
    """Mở SSH tunnel tới Postgres — singleton cả tiến trình."""
    global _TUNNEL
    s = _load_secrets()

    def pick(k):
        return os.environ.get(k) or s.get(k)

    sh = pick("SSH_HOST")
    if not sh:
        return None
    with _TUNNEL_LOCK:
        if _TUNNEL is not None and _TUNNEL.is_active:
            return ("127.0.0.1", _TUNNEL.local_bind_port)
        import logging
        logging.getLogger("paramiko").setLevel(logging.WARNING)
        _TUNNEL = _SSHTunnel(
            sh, pick("SSH_PORT") or 22, pick("SSH_USER"), pick("SSH_PASSWORD"),
            pick("PGHOST") or "127.0.0.1", pick("PGPORT") or 5432,
        )
        return ("127.0.0.1", _TUNNEL.local_bind_port)


def db_url() -> str | None:
    """SQLAlchemy URL tới PostgreSQL qua SSH tunnel. None nếu chưa cấu hình."""
    from urllib.parse import quote_plus
    s = _load_secrets()

    def pick(key):
        return os.environ.get(key) or s.get(key)

    url = pick("PROFILE_DB_URL") or pick("HSMT_DB_URL")
    if url:
        return url
    db = pick("PGDATABASE")
    if not db:
        return None
    user = pick("PGUSER") or "postgres"
    pw   = pick("PGPASSWORD") or ""
    tun  = _ssh_tunnel_local()
    if tun:
        host, port = tun
    else:
        host = pick("PGHOST")
        port = pick("PGPORT") or "5432"
        if not host:
            return None
    return (f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(pw)}"
            f"@{host}:{port}/{db}")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
