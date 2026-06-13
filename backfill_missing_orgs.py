"""Bổ sung các org có trong notices.investor_code nhưng thiếu ở mọi bảng profile.

Cách làm (Phương án B): tra cứu trực tiếp từng mã qua endpoint lookup-orgInfo
(orgCode.contains, bỏ roleType) — trả về org kèm tất cả vai trò — rồi upsert
mỗi bản ghi vào bảng tương ứng theo parentName.

An toàn để chạy lại: mỗi lần chạy tự lấy lại danh sách mã CÒN thiếu, nên nếu bị
ngắt giữa chừng chỉ cần chạy lại.

Chạy:  python backfill_missing_orgs.py
"""
from __future__ import annotations
import json, sys, time
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from playwright.sync_api import sync_playwright
from sqlalchemy import text

from crawler import config as C
from crawler.db import ProfileDB
from crawler.scraper import ProfileScraper, _get_records

# parentName (vai trò) -> nguồn/bảng profile
ROLE_TO_SOURCE = {
    "NT": "contractors", "NTPER": "contractors",
    "BMT": "bid_solicitors",
    "CDT": "investors_v2",
    "NDT": "investors",
}

DELAY = 0.25   # giây nghỉ giữa các lượt tra cứu


def missing_codes(db: ProfileDB) -> list[str]:
    union = ("SELECT lower(org_code) oc FROM contractors "
             "UNION SELECT lower(org_code) FROM investors "
             "UNION SELECT lower(org_code) FROM investors_v2 "
             "UNION SELECT lower(org_code) FROM bid_solicitors")
    with db.engine.connect() as c:
        rows = c.execute(text(
            f"SELECT n.oc FROM (SELECT DISTINCT lower(investor_code) oc "
            f"FROM notices WHERE investor_code IS NOT NULL AND investor_code<>'') n "
            f"WHERE n.oc NOT IN ({union})")).fetchall()
    return [r[0] for r in rows]


def main():
    db = ProfileDB()
    codes = missing_codes(db)
    print(f"Còn thiếu {len(codes)} org cần bổ sung.", flush=True)
    if not codes:
        db.close(); return

    stats = {"found": 0, "not_found": 0, "rows_new": 0, "rows_updated": 0,
             "rows_unchanged": 0, "errors": 0}
    per_table = {}
    not_found = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=C.USER_AGENT, locale="vi-VN",
                                  viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        # endpoint lookup-orgInfo của bid_solicitors trả bản ghi đầy đủ trường
        sc = ProfileScraper("bid_solicitors", C.SOURCES["bid_solicitors"]["url"],
                            page, verbose=False)
        api_url, body = sc._setup()
        if not api_url:
            print("Không thiết lập được endpoint lookup-orgInfo.", flush=True)
            ctx.close(); browser.close(); db.close(); return
        base = json.loads(body)

        for i, code in enumerate(codes, 1):
            tax = code[2:] if code.startswith("vn") else code
            try:
                b = json.loads(json.dumps(base))   # copy sâu
                b["pageNumber"] = 0
                b["pageSize"] = 20
                b["queryParams"]["orgCode"] = {"contains": tax}
                b["queryParams"]["roleType"] = {}
                data = sc._call_api(api_url, json.dumps(b, ensure_ascii=False))
                recs = [r for r in (_get_records(data) if data else [])
                        if str(r.get("orgCode", "")).lower() == code]
                if not recs:
                    stats["not_found"] += 1
                    not_found.append(code)
                else:
                    stats["found"] += 1
                    for r in recs:
                        src = ROLE_TO_SOURCE.get(r.get("parentName"))
                        if not src:
                            continue
                        r["_record_key"] = r.get("orgCode")
                        res = db.upsert_record(src, r)
                        stats[f"rows_{res}"] = stats.get(f"rows_{res}", 0) + 1
                        per_table[src] = per_table.get(src, 0) + (1 if res != "unchanged" else 0)
            except Exception as e:
                stats["errors"] += 1
                if stats["errors"] <= 10:
                    print(f"  lỗi tại {code}: {e}", flush=True)
            if i % 50 == 0:
                print(f"  [{i}/{len(codes)}] found={stats['found']} "
                      f"not_found={stats['not_found']} "
                      f"new={stats.get('rows_new',0)} upd={stats.get('rows_updated',0)} "
                      f"err={stats['errors']}", flush=True)
            time.sleep(DELAY)

        ctx.close(); browser.close()

    print("\n===== KẾT QUẢ =====", flush=True)
    print(f"  Org tìm thấy:     {stats['found']}", flush=True)
    print(f"  Org KHÔNG thấy:   {stats['not_found']}", flush=True)
    print(f"  Bản ghi mới:      {stats.get('rows_new',0)}", flush=True)
    print(f"  Bản ghi cập nhật: {stats.get('rows_updated',0)}", flush=True)
    print(f"  Không đổi:        {stats.get('rows_unchanged',0)}", flush=True)
    print(f"  Lỗi:              {stats['errors']}", flush=True)
    print(f"  Theo bảng:        {per_table}", flush=True)
    if not_found:
        print(f"  Mẫu mã không thấy: {not_found[:20]}", flush=True)

    # kiểm tra lại độ phủ join
    remain = missing_codes(db)
    print(f"\n  CÒN THIẾU sau bổ sung: {len(remain)} (trước: {len(codes)})", flush=True)
    db.close()


if __name__ == "__main__":
    main()
