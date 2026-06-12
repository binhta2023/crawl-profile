"""Tầng DB: ghi dữ liệu hồ sơ vào bảng riêng theo từng nguồn.

Mỗi nguồn có bảng riêng (contractors, investors, ...) với cột cố định
để project khác dễ query, cộng extra_data JSONB cho trường không cố định.

Incremental crawl: dùng data_hash (MD5 của JSON record) để phát hiện
bản ghi đã có và chưa thay đổi, nhờ đó dừng sớm mà không cần đọc toàn bộ data.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import MetaData, Table, create_engine, func, select, text

from . import config as C

_DDL_FILE = C.BASE_DIR / "db" / "schema.sql"

# Map source_key → (table_name, record_key_column)
_TABLE_MAP: dict[str, tuple[str, str]] = {
    "contractors":         ("contractors",         "org_code"),
    "investors":           ("investors",           "org_code"),
    "foreign_contractors": ("foreign_contractors", "nttnn_id"),
    "investors_v2":        ("investors_v2",        "org_code"),
    "bid_solicitors":      ("bid_solicitors",      "org_code"),
}

# Map: api_field_name → db_column_name (per source)
_FIELD_MAP: dict[str, dict[str, str]] = {
    "contractors": {
        "orgCode":     "org_code",
        "orgFullname": "ten",
        "taxCode":     "ma_so_thue",
        "officeAdd":   "dia_chi",
        "parentName":  "loai_hinh",
        "status":      "trang_thai",
        "effRoleDate": "ngay_hieu_luc",
    },
    "investors": {
        "orgCode":     "org_code",
        "orgFullname": "ten",
        "taxCode":     "ma_so_thue",
        "officeAdd":   "dia_chi",
        "officePro":   "tinh_thanh",
        "officeDis":   "quan_huyen",
        "status":      "trang_thai",
        "effRoleDate": "ngay_hieu_luc",
    },
    "foreign_contractors": {
        "id":                   "nttnn_id",
        "contractorName":       "ten",
        "contractName":         "ten_hop_dong",
        "generalInfo":          "thong_tin_chung",
        "addressForeign":       "dia_chi_nn",
        "addressForeignDetail": "dia_chi_nn_ct",
        "addressVn":            "dia_chi_vn",
        "addressVnDetail":      "dia_chi_vn_ct",
        "excuteFromDate":       "ngay_bat_dau",
        "excuteToDate":         "ngay_ket_thuc",
        "publicDate":           "ngay_cong_bo",
        "status":               "trang_thai",
        "createdBy":            "nguoi_tao",
        "createdDate":          "ngay_tao",
        "otherInfo":            "thong_tin_khac",
    },
    "investors_v2": {
        "orgCode":      "org_code",
        "orgFullname":  "ten",
        "taxCode":      "ma_so_thue",
        "officeAdd":    "dia_chi",
        "officePro":    "tinh_thanh",
        "officeDis":    "quan_huyen",
        "officePhone":  "dien_thoai",
        "recEmail":     "email",
        "repFullname":  "nguoi_dai_dien",
        "parentName":   "loai_hinh",
        "businessType": "loai_dn",
        "status":       "trang_thai",
        "statusOrg":    "trang_thai_to_chuc",
        "effRoleDate":  "ngay_hieu_luc",
        "expTime":      "ngay_het_han",
        "taxNation":    "quoc_gia_thue",
    },
    "bid_solicitors": {
        "orgCode":      "org_code",
        "orgFullname":  "ten",
        "taxCode":      "ma_so_thue",
        "officeAdd":    "dia_chi",
        "officePro":    "tinh_thanh",
        "officeDis":    "quan_huyen",
        "officePhone":  "dien_thoai",
        "recEmail":     "email",
        "repFullname":  "nguoi_dai_dien",
        "parentName":   "loai_hinh",
        "businessType": "loai_dn",
        "status":       "trang_thai",
        "statusOrg":    "trang_thai_to_chuc",
        "effRoleDate":  "ngay_hieu_luc",
        "expTime":      "ngay_het_han",
        "taxNation":    "quoc_gia_thue",
    },
}

# Cột cố định của từng bảng (không tính serial id, data_hash, crawled_at, updated_at)
_FIXED_COLS: dict[str, list[str]] = {
    "contractors":         ["org_code", "ten", "ma_so_thue", "dia_chi",
                            "loai_hinh", "trang_thai", "ngay_hieu_luc"],
    "investors":           ["org_code", "ten", "ma_so_thue", "dia_chi",
                            "tinh_thanh", "quan_huyen", "trang_thai", "ngay_hieu_luc"],
    "foreign_contractors": ["nttnn_id", "ten", "ten_hop_dong", "thong_tin_chung",
                            "dia_chi_nn", "dia_chi_nn_ct", "dia_chi_vn", "dia_chi_vn_ct",
                            "ngay_bat_dau", "ngay_ket_thuc", "ngay_cong_bo",
                            "trang_thai", "nguoi_tao", "ngay_tao", "thong_tin_khac"],
    "investors_v2":        ["org_code", "ten", "ma_so_thue", "dia_chi",
                            "tinh_thanh", "quan_huyen", "dien_thoai", "email",
                            "nguoi_dai_dien", "loai_hinh", "loai_dn",
                            "trang_thai", "trang_thai_to_chuc",
                            "ngay_hieu_luc", "ngay_het_han", "quoc_gia_thue"],
    "bid_solicitors":      ["org_code", "ten", "ma_so_thue", "dia_chi",
                            "tinh_thanh", "quan_huyen", "dien_thoai", "email",
                            "nguoi_dai_dien", "loai_hinh", "loai_dn",
                            "trang_thai", "trang_thai_to_chuc",
                            "ngay_hieu_luc", "ngay_het_han", "quoc_gia_thue"],
}

# API fields có giá trị là array [year, month, day, hour, min, sec] → TIMESTAMPTZ
_DATE_ARRAY_FIELDS = frozenset({'effRoleDate', 'expTime'})


def _now():
    return datetime.now(timezone.utc)


def _arr_to_dt(v):
    """Convert [year, month, day, hour, min, sec] array → datetime UTC."""
    if isinstance(v, list) and len(v) >= 3:
        try:
            return datetime(v[0], v[1], v[2],
                            v[3] if len(v) > 3 else 0,
                            v[4] if len(v) > 4 else 0,
                            v[5] if len(v) > 5 else 0,
                            tzinfo=timezone.utc)
        except Exception:
            return None
    return v


def _hash(record: dict) -> str:
    """MD5 của JSON record (bỏ _record_key helper field)."""
    clean = {k: v for k, v in record.items() if k != '_record_key'}
    s = json.dumps(clean, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(s.encode()).hexdigest()


def _map_record(source_key: str, raw: dict) -> tuple[dict, dict]:
    """Tách record thành (cột_cố_định, extra_data).

    - Các field có trong _FIELD_MAP[source_key] → cột tương ứng
    - Còn lại → extra_data JSONB
    Khi _FIELD_MAP chưa được điền (placeholder) → tất cả vào extra_data.
    """
    field_map = _FIELD_MAP.get(source_key, {})
    fixed_cols = _FIXED_COLS.get(source_key, [])
    record_key_col = _TABLE_MAP[source_key][1]

    fixed: dict = {}
    extra: dict = {}

    for api_field, value in raw.items():
        if api_field == '_record_key':
            continue
        if api_field in _DATE_ARRAY_FIELDS and isinstance(value, list):
            value = _arr_to_dt(value)
        db_col = field_map.get(api_field)
        if db_col and db_col in fixed_cols:
            fixed[db_col] = value
        else:
            extra[api_field] = value

    # record_key được scraper gán vào _record_key, map sang cột PK của bảng
    rk = raw.get('_record_key', '')
    if record_key_col not in fixed:
        fixed[record_key_col] = rk

    return fixed, extra


class ProfileDB:
    """Kết nối PostgreSQL và ghi/đọc dữ liệu profile."""

    def __init__(self, url: str | None = None):
        self.url = url or C.db_url()
        if not self.url:
            raise RuntimeError(
                "Chưa cấu hình DB. Kiểm tra secrets.local.txt có PG* hoặc SSH_HOST."
            )
        kw: dict = {"pool_pre_ping": True}
        if self.url.startswith("sqlite"):
            kw["connect_args"] = {"timeout": 60}
        self.engine = create_engine(self.url, **kw)
        self._ensure_schema()
        md = MetaData()
        self._tables: dict[str, Table] = {}
        all_tables = set(t for t, _ in _TABLE_MAP.values()) | {
            "provinces", "districts", "profile_crawl_log",
        }
        for tbl_name in all_tables:
            self._tables[tbl_name] = Table(tbl_name, md, autoload_with=self.engine)
        self.logs = self._tables["profile_crawl_log"]

    def _ensure_schema(self):
        if self.engine.dialect.name != "postgresql":
            return
        if not _DDL_FILE.exists():
            return
        ddl = _DDL_FILE.read_text(encoding="utf-8")
        with self.engine.begin() as conn:
            conn.exec_driver_sql(ddl)

    def close(self):
        self.engine.dispose()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ── Log ──────────────────────────────────────────────────────────────

    def log_start(self, source: str) -> int:
        with self.engine.begin() as conn:
            return conn.execute(
                self.logs.insert().values(source=source, started_at=_now())
                .returning(self.logs.c.id)
            ).scalar()

    def log_finish(self, log_id: int, *, n_total: int, n_new: int,
                   n_updated: int, n_unchanged: int, n_error: int,
                   stopped_early: bool, status: str,
                   error_msg: str | None = None):
        with self.engine.begin() as conn:
            conn.execute(
                self.logs.update().where(self.logs.c.id == log_id).values(
                    finished_at=_now(), n_total=n_total, n_new=n_new,
                    n_updated=n_updated, n_unchanged=n_unchanged, n_error=n_error,
                    stopped_early=stopped_early, status=status, error_msg=error_msg,
                )
            )

    def last_log(self, source: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                self.logs.select()
                .where(self.logs.c.source == source)
                .order_by(self.logs.c.started_at.desc())
                .limit(1)
            ).mappings().first()
        return dict(row) if row else None

    # ── Hash lookup (incremental crawl) ──────────────────────────────────

    def get_hash(self, source: str, record_key: str) -> str | None:
        """Trả về data_hash đang lưu cho record này; None nếu chưa có."""
        tbl_name, key_col = _TABLE_MAP[source]
        tbl = self._tables[tbl_name]
        with self.engine.connect() as conn:
            row = conn.execute(
                select(tbl.c.data_hash)
                .where(tbl.c[key_col] == record_key)
            ).first()
        return row[0] if row else None

    def get_hashes_for_page(self, source: str,
                            record_keys: list[str]) -> dict[str, str]:
        """Lấy hash của 1 tập record_keys cùng lúc (1 query thay vì N)."""
        if not record_keys:
            return {}
        tbl_name, key_col = _TABLE_MAP[source]
        tbl = self._tables[tbl_name]
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(tbl.c[key_col], tbl.c.data_hash)
                .where(tbl.c[key_col].in_(record_keys))
            ).fetchall()
        return {r[0]: r[1] for r in rows if r[1]}

    # ── Upsert ──────────────────────────────────────────────────────────

    def upsert_record(self, source: str, raw: dict) -> str:
        """Insert hoặc update 1 record. Trả về 'new'|'updated'|'unchanged'."""
        record_key = raw.get('_record_key', '')
        new_hash = _hash(raw)
        existing_hash = self.get_hash(source, record_key)

        if existing_hash == new_hash:
            return 'unchanged'

        fixed, extra = _map_record(source, raw)
        tbl_name, key_col = _TABLE_MAP[source]
        tbl = self._tables[tbl_name]

        values = {**fixed, 'extra_data': extra, 'data_hash': new_hash,
                  'updated_at': _now()}

        with self.engine.begin() as conn:
            if existing_hash is None:
                values['crawled_at'] = _now()
                conn.execute(tbl.insert().values(**values))
                return 'new'
            else:
                conn.execute(
                    tbl.update()
                    .where(tbl.c[key_col] == record_key)
                    .values(**values)
                )
                return 'updated'

    def upsert_page(self, source: str,
                    page_records: list[dict]) -> tuple[int, int, int]:
        """Upsert 1 trang records. Trả về (n_new, n_updated, n_unchanged).

        Dùng bulk hash lookup (1 query) trước để quyết định mỗi record.
        """
        if not page_records:
            return 0, 0, 0

        keys = [r.get('_record_key', '') for r in page_records]
        existing = self.get_hashes_for_page(source, keys)

        n_new = n_updated = n_unchanged = 0
        tbl_name, key_col = _TABLE_MAP[source]
        tbl = self._tables[tbl_name]

        with self.engine.begin() as conn:
            for raw in page_records:
                key = raw.get('_record_key', '')
                new_hash = _hash(raw)
                ex_hash = existing.get(key)

                if ex_hash == new_hash:
                    n_unchanged += 1
                    continue

                fixed, extra = _map_record(source, raw)
                vals = {**fixed, 'extra_data': extra,
                        'data_hash': new_hash, 'updated_at': _now()}

                if ex_hash is None:
                    vals['crawled_at'] = _now()
                    conn.execute(tbl.insert().values(**vals))
                    n_new += 1
                else:
                    conn.execute(
                        tbl.update()
                        .where(tbl.c[key_col] == key)
                        .values(**vals)
                    )
                    n_updated += 1

        return n_new, n_updated, n_unchanged

    # ── Thống kê ─────────────────────────────────────────────────────────

    def count(self, source: str) -> int:
        tbl_name = _TABLE_MAP[source][0]
        tbl = self._tables[tbl_name]
        with self.engine.connect() as conn:
            row = conn.execute(select(func.count()).select_from(tbl)).first()
        return int(row[0]) if row else 0
