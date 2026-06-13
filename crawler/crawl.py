"""Điều phối crawl với thuật toán incremental: dừng sớm khi gặp records cũ.

Thuật toán dừng sớm (incremental crawl):
  - Crawl từng trang một (generator).
  - Sau mỗi trang, bulk-check hash của tất cả records trong trang với DB.
  - Nếu TOÀN BỘ records trong trang đều UNCHANGED (hash khớp) → dừng.
  - Nếu có ít nhất 1 record mới/thay đổi → tiếp tục trang kế.

Lý do dừng sau 1 trang unchanged (không phải 1 record):
  - Danh sách có thể không sort theo ngày → cần 1 trang đầy đủ để chắc.
  - 1 record unchanged ngẫu nhiên không có nghĩa phần còn lại cũng vậy.
  - 1 trang unchanged (~50 records) = tín hiệu đủ mạnh để dừng an toàn.
"""
from __future__ import annotations

import traceback

from playwright.sync_api import sync_playwright

from . import config as C
from .scraper import ProfileScraper
from .db import ProfileDB
from .seeder import seed_provinces, seed_districts


def _crawl_incremental(source_key: str, scraper: ProfileScraper,
                       db: ProfileDB, verbose: bool, full: bool = False) -> dict:
    """Crawl 1 nguồn. full=True: quét HẾT mọi trang (tắt dừng sớm) để backfill.

    Trả về stats dict.
    """
    stats = {"n_total": 0, "n_new": 0, "n_updated": 0,
             "n_unchanged": 0, "stopped_early": False}

    for page_records in scraper.iter_pages():
        if not page_records:
            continue

        n_new, n_updated, n_unchanged = db.upsert_page(source_key, page_records)
        page_total = len(page_records)

        stats["n_total"]    += page_total
        stats["n_new"]      += n_new
        stats["n_updated"]  += n_updated
        stats["n_unchanged"]+= n_unchanged

        if verbose:
            print(f"  Trang {stats['n_total']//max(page_total,1)}: "
                  f"+{n_new} mới, ~{n_updated} cập nhật, "
                  f"{n_unchanged} không đổi / {page_total} records",
                  flush=True)

        # Dừng sớm: toàn trang không có gì mới (bỏ qua khi full=True)
        if not full and n_new == 0 and n_updated == 0 and n_unchanged == page_total:
            if verbose:
                print(f"  → Dừng sớm: trang vừa rồi toàn records đã có "
                      f"({n_unchanged}/{page_total} unchanged)", flush=True)
            stats["stopped_early"] = True
            break

    return stats


def _run_source(source_key: str, headless: bool, verbose: bool,
                db: ProfileDB, full: bool = False) -> dict:
    """Crawl 1 nguồn, ghi DB, trả về dict kết quả."""
    info = C.SOURCES[source_key]
    log_id = db.log_start(source_key)
    result = {"source": source_key, "n_total": 0, "n_new": 0,
              "n_updated": 0, "n_unchanged": 0, "n_error": 0,
              "stopped_early": False, "status": "error", "error": None}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            ctx = browser.new_context(
                user_agent=C.USER_AGENT,
                locale="vi-VN",
                viewport={"width": 1440, "height": 900},
            )
            page = ctx.new_page()
            try:
                scraper = ProfileScraper(source_key, info["url"],
                                         page, verbose=verbose)
                stats = _crawl_incremental(source_key, scraper, db, verbose, full)
                # Seed districts từ cat-areas data bắt được lúc load trang
                seed_districts(scraper.cat_areas_data, db, verbose=verbose)
                result.update(stats)
                result["status"] = "done"
                if verbose:
                    print(f"[{source_key}] ✓ Xong: {stats['n_total']} records "
                          f"(+{stats['n_new']} mới, ~{stats['n_updated']} cập nhật"
                          + (", dừng sớm" if stats['stopped_early'] else "") + ")",
                          flush=True)
            finally:
                ctx.close()
                browser.close()
    except Exception as e:
        result["error"] = str(e)
        result["status"] = "error"
        if verbose:
            print(f"[{source_key}] ✗ LỖI: {e}", flush=True)
            traceback.print_exc()
    finally:
        db.log_finish(
            log_id,
            n_total=result["n_total"],
            n_new=result["n_new"],
            n_updated=result["n_updated"],
            n_unchanged=result["n_unchanged"],
            n_error=result["n_error"],
            stopped_early=result["stopped_early"],
            status=result["status"],
            error_msg=result["error"],
        )
    return result


def crawl_source(source_key: str, headless: bool = True,
                 verbose: bool = True, full: bool = False) -> dict:
    if source_key not in C.SOURCES:
        raise ValueError(f"Nguồn không hợp lệ: {source_key!r}. "
                         f"Chọn: {list(C.SOURCES)}")
    C.ensure_dirs()
    with ProfileDB() as db:
        seed_provinces(db, verbose=verbose)
        return _run_source(source_key, headless, verbose, db, full)


def crawl_all(headless: bool = True, verbose: bool = True,
              sources: list[str] | None = None,
              progress_cb=None, full: bool = False) -> list[dict]:
    """Crawl tất cả nguồn tuần tự. full=True: quét hết mọi trang (backfill toàn bộ).

    progress_cb(source_key, result): callback sau mỗi nguồn (dùng cho GUI).
    """
    C.ensure_dirs()
    keys = sources or list(C.SOURCES)
    results = []
    with ProfileDB() as db:
        seed_provinces(db, verbose=verbose)
        for key in keys:
            if verbose:
                name = C.SOURCES[key]["name"]
                mode = " [FULL]" if full else ""
                print(f"\n{'='*60}\n== Crawl: {name}{mode}\n{'='*60}", flush=True)
            r = _run_source(key, headless, verbose, db, full)
            results.append(r)
            if progress_cb:
                try:
                    progress_cb(key, r)
                except Exception:
                    pass
    return results
