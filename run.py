"""CLI để chạy tay crawler Profile muasamcong.

Ví dụ:
    python run.py                           # crawl tất cả 5 nguồn
    python run.py --source contractors      # chỉ crawl nhà thầu
    python run.py --headed                  # hiện cửa sổ trình duyệt
    python run.py --list-sources            # liệt kê nguồn
"""
from __future__ import annotations

import argparse
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main():
    from crawler import config as C

    ap = argparse.ArgumentParser(
        description="Crawl danh sách hồ sơ năng lực từ muasamcong.mpi.gov.vn",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--source",
        help=f"Crawl chỉ một nguồn: {', '.join(C.SOURCES)}",
    )
    ap.add_argument("--headed", action="store_true", help="Hiện cửa sổ trình duyệt")
    ap.add_argument("--full", action="store_true",
                    help="Quét HẾT mọi trang (tắt dừng sớm) — lấy đầy đủ về DB, chạy lâu")
    ap.add_argument("--quiet", action="store_true", help="Ít log hơn")
    ap.add_argument("--list-sources", action="store_true", help="Liệt kê các nguồn rồi thoát")
    args = ap.parse_args()

    if args.list_sources:
        print("Nguồn khả dụng:")
        for k, v in C.SOURCES.items():
            print(f"  {k:25s}  {v['name']}")
            print(f"    {v['url']}")
        return

    C.ensure_dirs()
    headless = not args.headed
    verbose  = not args.quiet

    from crawler.crawl import crawl_all, crawl_source

    if args.source:
        r = crawl_source(args.source, headless=headless, verbose=verbose,
                         full=args.full)
        rc = 0 if r["status"] == "done" else 1
    else:
        results = crawl_all(headless=headless, verbose=verbose, full=args.full)
        errors = [r for r in results if r["status"] != "done"]
        print(f"\n{'='*60}")
        print(f"Tổng: {len(results)} nguồn, {len(errors)} lỗi")
        for r in results:
            tag = "✓" if r["status"] == "done" else "✗"
            early = " [dừng sớm]" if r.get("stopped_early") else ""
            print(f"  {tag} {r['source']:25s} {r['n_total']:6d} bản ghi "
                  f"(+{r['n_new']} mới, ~{r['n_updated']} cập nhật, "
                  f"{r.get('n_unchanged',0)} không đổi){early}"
                  + (f"  [{r['error']}]" if r.get("error") else ""))
        rc = 0 if not errors else 1

    sys.exit(rc)


if __name__ == "__main__":
    main()
