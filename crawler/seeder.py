"""Seed bảng tra cứu tỉnh/thành phố và quận/huyện.

Provinces: hardcode 63 tỉnh/thành theo mã Bộ Nội vụ (officePro từ API).
Districts: thử fetch cat-areas trong browser session khi crawl; bỏ qua nếu fail.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page
    from .db import ProfileDB

# 63 tỉnh/thành phố — mã chuẩn Bộ Nội vụ, khớp officePro trong API
_PROVINCES = [
    ("01", "Hà Nội"),
    ("02", "Hà Giang"),
    ("04", "Cao Bằng"),
    ("06", "Bắc Kạn"),
    ("08", "Tuyên Quang"),
    ("10", "Lào Cai"),
    ("11", "Điện Biên"),
    ("12", "Lai Châu"),
    ("14", "Sơn La"),
    ("15", "Yên Bái"),
    ("17", "Hòa Bình"),
    ("19", "Thái Nguyên"),
    ("20", "Lạng Sơn"),
    ("22", "Quảng Ninh"),
    ("24", "Bắc Giang"),
    ("25", "Phú Thọ"),
    ("26", "Vĩnh Phúc"),
    ("27", "Bắc Ninh"),
    ("30", "Hải Dương"),
    ("31", "Hải Phòng"),
    ("33", "Hưng Yên"),
    ("34", "Thái Bình"),
    ("35", "Hà Nam"),
    ("36", "Nam Định"),
    ("37", "Ninh Bình"),
    ("38", "Thanh Hóa"),
    ("40", "Nghệ An"),
    ("42", "Hà Tĩnh"),
    ("44", "Quảng Bình"),
    ("45", "Quảng Trị"),
    ("46", "Thừa Thiên Huế"),
    ("48", "Đà Nẵng"),
    ("49", "Quảng Nam"),
    ("51", "Quảng Ngãi"),
    ("52", "Bình Định"),
    ("54", "Phú Yên"),
    ("56", "Khánh Hòa"),
    ("58", "Ninh Thuận"),
    ("60", "Bình Thuận"),
    ("62", "Kon Tum"),
    ("64", "Gia Lai"),
    ("66", "Đắk Lắk"),
    ("67", "Đắk Nông"),
    ("68", "Lâm Đồng"),
    ("70", "Bình Phước"),
    ("72", "Tây Ninh"),
    ("74", "Bình Dương"),
    ("75", "Đồng Nai"),
    ("77", "Bà Rịa - Vũng Tàu"),
    ("79", "Thành phố Hồ Chí Minh"),
    ("80", "Long An"),
    ("82", "Tiền Giang"),
    ("83", "Bến Tre"),
    ("84", "Trà Vinh"),
    ("86", "Vĩnh Long"),
    ("87", "Đồng Tháp"),
    ("89", "An Giang"),
    ("91", "Kiên Giang"),
    ("92", "Cần Thơ"),
    ("93", "Hậu Giang"),
    ("94", "Sóc Trăng"),
    ("95", "Bạc Liêu"),
    ("96", "Cà Mau"),
]

_FETCH_JS = r"""
async ({url, body}) => {
    try {
        const r = await fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
            body: body,
        });
        return {status: r.status, text: await r.text()};
    } catch(e) { return {status: 0, text: String(e)}; }
}
"""

_CAT_AREAS_URLS = [
    "https://muasamcong.mpi.gov.vn/o/egp-portal-contractors-approved/services/cat-areas",
    "https://muasamcong.mpi.gov.vn/o/egp-portal-investors-approved/services/cat-areas",
]


def _parse_districts(data) -> list[dict]:
    """Trích districts từ cat-areas response (nhiều format khác nhau)."""
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ('content', 'data', 'items', 'list', 'result'):
            if isinstance(data.get(key), list):
                items = data[key]
                break

    districts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pcode = str(
            item.get('code') or item.get('areaCode') or
            item.get('provinceCode') or item.get('id') or ''
        )
        children = (item.get('children') or item.get('districts') or
                    item.get('subAreas') or [])
        for child in (children or []):
            if not isinstance(child, dict):
                continue
            dcode = str(
                child.get('code') or child.get('areaCode') or
                child.get('districtCode') or child.get('id') or ''
            )
            dname = str(
                child.get('name') or child.get('areaName') or
                child.get('districtName') or ''
            )
            if dcode and dname and pcode:
                districts.append({'code': dcode, 'name': dname, 'province_code': pcode})
    return districts


def seed_provinces(db: "ProfileDB", verbose: bool = True) -> int:
    """Insert 63 tỉnh/thành nếu bảng còn trống. Trả về số dòng đã có."""
    from sqlalchemy import text
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    with db.engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM provinces")).scalar()

    if n and n >= 63:
        return n

    rows = [{"code": c, "name": nm} for c, nm in _PROVINCES]
    with db.engine.begin() as conn:
        conn.execute(
            pg_insert(db._tables["provinces"])
            .values(rows)
            .on_conflict_do_nothing(index_elements=["code"])
        )
    if verbose:
        print(f"  ✓ Seeded {len(rows)} tỉnh/thành phố", flush=True)
    return len(rows)


def seed_districts(page: "Page", db: "ProfileDB", verbose: bool = True) -> int:
    """Thử fetch cat-areas từ browser session để lấy quận/huyện.

    Trả về số district đã insert (0 nếu không lấy được).
    """
    from sqlalchemy import text
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    with db.engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM districts")).scalar()
    if n and n > 0:
        return n

    data = None
    for url in _CAT_AREAS_URLS:
        try:
            res = page.evaluate(_FETCH_JS, {"url": url, "body": "{}"})
            if res.get("status") == 200 and res.get("text"):
                data = json.loads(res["text"])
                if isinstance(data, (list, dict)):
                    break
        except Exception:
            continue

    if not data:
        if verbose:
            print("  ⚠ Không lấy được cat-areas — bỏ qua districts", flush=True)
        return 0

    districts = _parse_districts(data)
    if not districts:
        if verbose:
            print(f"  ⚠ cat-areas trả về data nhưng parse được 0 district "
                  f"(type={type(data).__name__}, preview={str(data)[:120]})", flush=True)
        return 0

    with db.engine.begin() as conn:
        conn.execute(
            pg_insert(db._tables["districts"])
            .values(districts)
            .on_conflict_do_nothing(index_elements=["code"])
        )
    if verbose:
        print(f"  ✓ Seeded {len(districts)} quận/huyện", flush=True)
    return len(districts)
