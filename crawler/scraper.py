"""Crawl 1 trang danh sách hồ sơ từ muasamcong bằng Playwright.

Cơ chế:
  1. Mở trang, lắng nghe tất cả request POST JSON đến /services/ hoặc /api/
  2. Sau networkidle, thử từng endpoint bắt được: gọi lại in-page với page 0,
     kiểm tra có trả danh sách phân trang không.
  3. Nếu tìm được endpoint phù hợp → phân trang hết, trả list dict.
  4. Fallback: cào HTML table + click "next page" từng trang.

Xử lý reCAPTCHA tự động: thử không token trước; nếu HTTP 400 và trang có
grecaptcha.execute → mint token rồi gọi lại.
"""
from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Iterator

from playwright.sync_api import Page, TimeoutError as PWTimeout

from . import config as C

# ---- JavaScript helpers ----

# Gọi API POST JSON trong trang, không token
_FETCH_JS = r"""
async ({url, bodyStr}) => {
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
      body: bodyStr,
    });
    return {status: r.status, text: await r.text()};
  } catch(e) {
    return {status: 0, text: String(e)};
  }
}
"""

# Mint reCAPTCHA token (nếu trang đã load grecaptcha) rồi gọi API
_FETCH_RECAPTCHA_JS = r"""
async ({url, bodyStr, sitekey}) => {
  let token = null;
  try {
    token = await Promise.race([
      new Promise((res, rej) => {
        try {
          window.grecaptcha.ready(() => {
            window.grecaptcha.execute(sitekey, {action: 'search'}).then(res).catch(e => rej(String(e)));
          });
        } catch(e) { rej(String(e)); }
      }),
      new Promise((_, rej) => setTimeout(() => rej('timeout'), 12000)),
    ]);
  } catch(e) {
    token = null;
  }
  const fullUrl = token ? url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token) : url;
  try {
    const r = await fetch(fullUrl, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
      body: bodyStr,
    });
    return {status: r.status, text: await r.text(), used_token: !!token};
  } catch(e) {
    return {status: 0, text: String(e)};
  }
}
"""

# Đọc reCAPTCHA site key từ trang
_READ_SITEKEY_JS = r"""() => {
  for (const s of document.querySelectorAll('script[src]')) {
    const m = (s.src || '').match(/[?&]render=([0-9A-Za-z_\-]{30,})/);
    if (m && m[1] !== 'explicit') return m[1];
  }
  try {
    const c = window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients;
    for (const id in (c||{})) for (const k in c[id]) {
      const v = c[id][k];
      if (v && typeof v === 'object' && v.sitekey) return v.sitekey;
    }
  } catch(e) {}
  return null;
}"""


# ---- Tiện ích ----

def _strip_token(url: str) -> str:
    """Bỏ ?token=... hoặc &token=... khỏi URL."""
    url = re.sub(r'[?&]token=[^&]*', '', url)
    return url.rstrip('?').rstrip('&')


def _is_static(url: str) -> bool:
    ext = url.split('?')[0].rsplit('.', 1)[-1].lower()
    return ext in ('js', 'css', 'png', 'jpg', 'gif', 'ico', 'svg',
                   'woff', 'woff2', 'ttf', 'map', 'webp')


def _is_api_url(url: str) -> bool:
    return (('/services/' in url or '/api/' in url)
            and 'google' not in url and 'recaptcha' not in url
            and 'analytics' not in url)


# Endpoints phụ (không phải data) — bỏ qua khi tìm data endpoint
_SKIP_PATTERNS = frozenset([
    'cat-areas', 'categories-by-cat-type', 'role-status',
    'cat/areas', 'cat/categories',
])


def _get_total_pages(data) -> tuple[int, int]:
    """Trả về (total_elements, total_pages) từ response JSON."""
    if isinstance(data, dict):
        # Nested: {"ebidOrgInfos": {"content":[], "totalElements":N, ...}}
        for nested_key in ('ebidOrgInfos', 'ebidOrg', 'orgInfos', 'bidderInfos'):
            nested = data.get(nested_key)
            if isinstance(nested, dict) and 'totalElements' in nested:
                return int(nested['totalElements'] or 0), int(nested.get('totalPages') or 0)
        # Flat: {content:[], totalElements:N, totalPages:N}
        if 'totalElements' in data:
            return int(data['totalElements'] or 0), int(data.get('totalPages') or 0)
        if isinstance(data.get('page'), dict):
            pg = data['page']
            if 'totalElements' in pg:
                return int(pg['totalElements'] or 0), int(pg.get('totalPages') or 0)
        for key in ('data', 'items', 'list', 'records', 'result'):
            if isinstance(data.get(key), list) and 'total' in data:
                total = int(data['total'] or 0)
                ps = max(len(data[key]), 1)
                return total, (total + ps - 1) // ps
    elif isinstance(data, list):
        return len(data), 1
    return 0, 0


def _get_records(data) -> list[dict]:
    """Trích list[dict] records từ response."""
    if isinstance(data, dict):
        # Nested: {"ebidOrgInfos": {"content": [...]}}
        for nested_key in ('ebidOrgInfos', 'ebidOrg', 'orgInfos', 'bidderInfos'):
            nested = data.get(nested_key)
            if isinstance(nested, dict):
                v = nested.get('content', [])
                if isinstance(v, list) and v:
                    return [r for r in v if isinstance(r, dict)]
        for key in ('content', 'data', 'items', 'list', 'records', 'result'):
            v = data.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        if isinstance(data.get('page'), dict):
            v = data['page'].get('content', [])
            return [r for r in v if isinstance(r, dict)]
    elif isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def _set_page_num(body_str: str, page_num: int, page_size: int | None = None) -> str:
    """Đặt lại pageNumber trong body JSON; nếu có page_size thì ép luôn pageSize.

    API muasamcong giới hạn cứng pageSize tối đa = 20, nên dùng 20 để giảm
    số trang (mặc định body của trang web chỉ là 10).
    """
    def _apply_size(obj):
        if page_size is not None:
            for sk in ('pageSize', 'pagesize', 'size', 'limit'):
                if sk in obj:
                    obj[sk] = page_size
                    break
    try:
        data = json.loads(body_str)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            obj = data[0]
            if 'pageNumber' in obj:
                obj['pageNumber'] = page_num
            _apply_size(obj)
            return json.dumps(data, ensure_ascii=False)
        if isinstance(data, dict):
            for k in ('pageNumber', 'pageNum', 'page', 'currentPage', 'pageNo', 'from'):
                if k in data:
                    data[k] = page_num
                    break
            _apply_size(data)
            return json.dumps(data, ensure_ascii=False)
    except Exception:
        pass
    return body_str


def _add_keys(records: list[dict], source: str) -> None:
    """Thêm _record_key vào mỗi record in-place."""
    for r in records:
        if '_record_key' not in r:
            r['_record_key'] = _extract_key(r, source)


def _extract_key(record: dict, source: str) -> str:
    """Trích khóa duy nhất từ một record để làm record_key khi upsert."""
    for field in (
        'id', 'Id', 'ID',
        'orgCode',
        'maDoanhNghiep', 'maNhaThai', 'maToChuc', 'maId',
        'contractorCode', 'investorCode', 'bidSolicitorCode',
        'maSoThue', 'taxCode', 'msdn', 'mst', 'tin', 'code',
        'Code', 'no', 'No', 'Ma', 'ma',
    ):
        val = record.get(field)
        if val and str(val).strip():
            return str(val).strip()
    # fallback: hash toàn bộ record
    import hashlib
    h = hashlib.md5(
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]
    return f"hash_{h}"


# ---- Lớp Scraper chính ----

class ProfileScraper:
    """Crawl một trang danh sách hồ sơ từ muasamcong.

    Dùng: scraper = ProfileScraper("contractors", url, page); records = scraper.crawl()
    """

    PAGE_SIZE = 20      # API muasamcong giới hạn cứng pageSize tối đa = 20
    MAX_PAGES = 20000   # trần an toàn (~400.000 records ở pageSize=20)

    def __init__(self, source_key: str, url: str, page: Page, verbose: bool = True):
        self.source_key = source_key
        self.url = url
        self._page = page
        self.verbose = verbose
        self._captured: list[dict] = []   # {url, method, post_data}
        self._sitekey: str | None = None
        self._capturing = True

    def _log(self, *a):
        if self.verbose:
            ts = C.now_vn().strftime('%H:%M:%S')
            print(f"[{ts}][{self.source_key}]", *a, flush=True)

    def _on_request(self, req):
        if not self._capturing:
            return
        url = req.url
        if _is_static(url) or not _is_api_url(url):
            return
        if req.method != 'POST':
            return
        post_data = req.post_data
        if not post_data:
            return
        try:
            json.loads(post_data)
        except Exception:
            return
        stripped = _strip_token(url)
        if not any(c['url'] == stripped for c in self._captured):
            self._captured.append({'url': stripped, 'post_data': post_data})

    def _read_sitekey(self) -> str | None:
        try:
            return self._page.evaluate(_READ_SITEKEY_JS)
        except Exception:
            return None

    def _call_api(self, url: str, body_str: str) -> dict | list | None:
        """Gọi API in-page; thử không token trước, nếu 400 thử với reCAPTCHA."""
        # Lần 1: không token
        try:
            res = self._page.evaluate(_FETCH_JS, {'url': url, 'bodyStr': body_str})
            if res['status'] == 200:
                return json.loads(res['text'])
            need_token = res['status'] in (400, 401, 403)
        except Exception:
            need_token = True

        if not need_token:
            return None

        # Lần 2: với reCAPTCHA
        if self._sitekey is None:
            self._sitekey = self._read_sitekey()
        if not self._sitekey:
            return None
        try:
            res = self._page.evaluate(_FETCH_RECAPTCHA_JS, {
                'url': url, 'bodyStr': body_str, 'sitekey': self._sitekey,
            })
            if res.get('status') == 200:
                return json.loads(res['text'])
        except Exception:
            pass
        return None


    # ---- HTML fallback ----

    def _extract_table_rows(self) -> list[dict]:
        try:
            headers = self._page.evaluate(r"""() => {
                const t = document.querySelector('table');
                if (!t) return [];
                const hrow = t.querySelector('thead tr, tr');
                if (!hrow) return [];
                return Array.from(hrow.querySelectorAll('th, td')).map(e => e.innerText.trim());
            }""")
            if not headers:
                return []
            rows = self._page.evaluate(r"""() => {
                const t = document.querySelector('table');
                if (!t) return [];
                const body = t.querySelector('tbody') || t;
                return Array.from(body.querySelectorAll('tr'))
                    .map(tr => Array.from(tr.querySelectorAll('td')).map(c => c.innerText.trim()))
                    .filter(r => r.length > 0 && r.some(c => c.length > 0));
            }""")
            result = []
            for row in rows:
                rec = {headers[i] if i < len(headers) else f'col_{i}': cell
                       for i, cell in enumerate(row)}
                result.append(rec)
            return result
        except Exception as e:
            self._log(f"  Lỗi đọc bảng HTML: {e}")
            return []

    def _find_next_btn(self):
        for sel in [
            'li.next:not(.disabled) a',
            '.pagination .next:not(.disabled) a',
            'a[aria-label="Next"]',
            'button:has-text("›"), button:has-text(">")',
            '.page-link:last-child:not(.disabled)',
        ]:
            try:
                btn = self._page.query_selector(sel)
                if btn and btn.is_visible() and btn.is_enabled():
                    return btn
            except Exception:
                pass
        return None

    def _crawl_html(self) -> list[dict]:
        all_rows: list[dict] = []
        page_num = 0
        while True:
            rows = self._extract_table_rows()
            if not rows:
                break
            all_rows.extend(rows)
            page_num += 1
            self._log(f"  HTML trang {page_num}: {len(rows)} dòng (tổng {len(all_rows)})")
            btn = self._find_next_btn()
            if not btn or page_num >= self.MAX_PAGES:
                break
            try:
                btn.click()
                self._page.wait_for_load_state('networkidle', timeout=30000)
                self._page.wait_for_timeout(800)
            except Exception as e:
                self._log(f"  Không chuyển trang được: {e}")
                break
        return all_rows

    # ── Thiết lập trang ─────────────────────────────────────────────────

    def _trigger_filter_search(self) -> bool:
        """Click 'Bộ lọc' → đợi modal → click 'Áp dụng' để trigger data API."""
        clicked = self._page.evaluate(r"""() => {
            const all = Array.from(document.querySelectorAll('button, [role="button"]'));
            for (const b of all) {
                const txt = (b.innerText || b.textContent || '').trim().toLowerCase();
                const cls = b.className || '';
                if ((txt.includes('bộ lọc') || cls.includes('searchbar'))
                        && b.offsetParent !== null) {
                    b.click(); return true;
                }
            }
            return false;
        }""")
        if not clicked:
            return False
        self._page.wait_for_timeout(2500)
        self._page.evaluate(r"""() => {
            const kws = ['áp dụng', 'tìm kiếm', 'tìm',
                         'search', 'apply', 'lọc'];
            const all = Array.from(document.querySelectorAll(
                'button, input[type="submit"]'));
            for (const kw of kws) {
                for (const b of all) {
                    const txt = (b.innerText || b.value || b.textContent || '')
                        .trim().toLowerCase();
                    if ((txt === kw || txt.startsWith(kw)) && b.offsetParent !== null) {
                        b.click(); return true;
                    }
                }
            }
            const subs = Array.from(document.querySelectorAll('.ant-btn-primary'));
            for (const s of subs) {
                if (s.offsetParent !== null && !s.disabled) {
                    const txt = (s.innerText || '').trim().toLowerCase();
                    if (!txt.includes('đăng') && !txt.includes('login')) {
                        s.click(); return true;
                    }
                }
            }
            return false;
        }""")
        self._page.wait_for_timeout(2000)
        return True

    def _fetch_districts(self) -> None:
        """Gọi cat-areas areaType=2 để lấy toàn bộ quận/huyện (dùng session đang mở)."""
        cat_url = next(
            (c['url'] for c in self._captured if 'cat-areas' in c['url']), None
        )
        if not cat_url:
            return
        try:
            data = self._call_api(cat_url, '{"areaType":"2"}')
            if isinstance(data, list) and data:
                self.cat_areas_data = data
                self._log(f"  ✓ cat-areas: {len(data)} quận/huyện")
        except Exception:
            pass

    def _setup(self) -> tuple[str | None, str | None]:
        """Mở trang, bắt requests, trả về (api_url, original_body) hoặc (None, None)."""
        self.cat_areas_data = None
        self._page.on('request', self._on_request)
        try:
            self._page.goto(self.url, wait_until='networkidle',
                            timeout=C.NAV_TIMEOUT_MS)
        except PWTimeout:
            self._log("⚠ Timeout tải trang, tiếp tục...")
        self._page.wait_for_timeout(2000)

        # Click "Bộ lọc → Áp dụng" để trigger data API (4/5 nguồn cần thao tác này)
        self._trigger_filter_search()
        self._capturing = False

        if not self._captured:
            return None, None

        self._log(f"Phát hiện {len(self._captured)} API endpoint")

        # Bỏ qua endpoints phụ (cat-areas, categories-by-cat-type, ...)
        candidates = [c for c in self._captured
                      if not any(pat in c['url'] for pat in _SKIP_PATTERNS)]
        if not candidates:
            candidates = self._captured

        for cap in candidates:
            body0 = _set_page_num(cap['post_data'], 0, self.PAGE_SIZE)
            data = self._call_api(cap['url'], body0)
            if data and _get_records(data):
                total_elem, total_pages = _get_total_pages(data)
                self._log(f"  ✓ Endpoint: .../{cap['url'].split('/')[-1]} "
                          f"— {total_elem} bản ghi, {total_pages} trang")
                self._page0_data = data
                self._fetch_districts()
                return cap['url'], cap['post_data']
        return None, None

    # ── Generator yield từng trang ────────────────────────────────────

    def iter_pages(self) -> "Iterator[list[dict]]":
        """Yield từng trang records (list[dict]). Cho phép caller dừng sớm.

        Mỗi record đã có trường _record_key.
        """
        self._page0_data = None
        api_url, original_body = self._setup()

        if api_url:
            yield from self._iter_api_pages(api_url, original_body)
        else:
            self._log("⚠ Không phát hiện API, cào HTML...")
            yield from self._iter_html_pages()

    def _iter_api_pages(self, api_url: str,
                        original_body: str) -> "Iterator[list[dict]]":
        # Trang 0 đã fetch trong _setup
        data0 = self._page0_data
        if data0 is None:
            body0 = _set_page_num(original_body, 0, self.PAGE_SIZE)
            data0 = self._call_api(api_url, body0)
            if not data0:
                return

        total_elem, total_pages = _get_total_pages(data0)
        if total_pages == 0 and total_elem > 0:
            total_pages = (total_elem + self.PAGE_SIZE - 1) // self.PAGE_SIZE
        total_pages = max(1, min(total_pages or 1, self.MAX_PAGES))

        recs0 = _get_records(data0)
        _add_keys(recs0, self.source_key)
        yield recs0

        for pn in range(1, total_pages):
            body_n = _set_page_num(original_body, pn, self.PAGE_SIZE)
            data_n = self._call_api(api_url, body_n)
            if data_n is None:
                self._log(f"  ✗ Trang {pn + 1} lỗi, dừng")
                return
            recs = _get_records(data_n)
            if not recs:
                return
            _add_keys(recs, self.source_key)
            self._log(f"  Trang {pn + 1}/{total_pages}...")
            yield recs
            time.sleep(C.REQUEST_DELAY)

    def _iter_html_pages(self) -> "Iterator[list[dict]]":
        page_num = 0
        while True:
            rows = self._extract_table_rows()
            if not rows:
                break
            _add_keys(rows, self.source_key)
            page_num += 1
            self._log(f"  HTML trang {page_num}: {len(rows)} dòng")
            yield rows
            btn = self._find_next_btn()
            if not btn or page_num >= self.MAX_PAGES:
                break
            try:
                btn.click()
                self._page.wait_for_load_state('networkidle', timeout=30000)
                self._page.wait_for_timeout(800)
            except Exception as e:
                self._log(f"  Không chuyển trang được: {e}")
                break

    # ── Crawl toàn bộ (không dừng sớm) ──────────────────────────────────

    def crawl_all(self) -> list[dict]:
        """Lấy tất cả records (không check DB). Chủ yếu dùng để test."""
        all_recs: list[dict] = []
        for page_recs in self.iter_pages():
            all_recs.extend(page_recs)
        self._log(f"Tổng: {len(all_recs)} bản ghi")
        return all_recs
