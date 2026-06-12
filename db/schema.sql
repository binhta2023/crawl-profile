-- =====================================================================
-- Profile Crawl Schema — PostgreSQL
-- Lưu danh sách hồ sơ năng lực từ muasamcong.mpi.gov.vn
-- Tên cột từ API thực tế (đã research 400+ records mỗi nguồn).
--
-- An toàn: chỉ CREATE ... IF NOT EXISTS — không đụng bảng hiện có.
-- =====================================================================

-- ─── Nhà thầu được phê duyệt ──────────────────────────────────────────
-- URL: /web/guest/approved-contractors-list
-- API: egp-portal-contractors-approved/services/get-list
-- Fields cố định: orgCode, orgFullname, taxCode, officeAdd, parentName, status, effRoleDate
CREATE TABLE IF NOT EXISTS contractors (
    id            BIGSERIAL PRIMARY KEY,
    org_code      TEXT UNIQUE NOT NULL,         -- orgCode (API) — mã tổ chức
    ten           TEXT,                         -- orgFullname
    ma_so_thue    TEXT,                         -- taxCode
    dia_chi       TEXT,                         -- officeAdd
    loai_hinh     TEXT,                         -- parentName: NT | NTPER | ...
    trang_thai    TEXT,                         -- status: 1 = active
    ngay_hieu_luc TIMESTAMPTZ,                  -- effRoleDate [y,m,d,h,min,sec]
    extra_data    JSONB NOT NULL DEFAULT '{}',  -- trường còn lại
    data_hash     TEXT,
    crawled_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contractors_ten       ON contractors(ten);
CREATE INDEX IF NOT EXISTS idx_contractors_mst       ON contractors(ma_so_thue);
CREATE INDEX IF NOT EXISTS idx_contractors_loai      ON contractors(loai_hinh);
CREATE INDEX IF NOT EXISTS idx_contractors_hl        ON contractors(ngay_hieu_luc DESC);
CREATE INDEX IF NOT EXISTS idx_contractors_extra     ON contractors USING GIN (extra_data);

-- ─── Nhà đầu tư được phê duyệt ────────────────────────────────────────
-- URL: /web/guest/investor-approved-list
-- API: egp-portal-investors-approved/services/get-list-approve-bidder
-- Fields cố định: orgCode, orgFullname, taxCode, officeAdd, officePro, officeDis, status, effRoleDate
CREATE TABLE IF NOT EXISTS investors (
    id            BIGSERIAL PRIMARY KEY,
    org_code      TEXT UNIQUE NOT NULL,
    ten           TEXT,
    ma_so_thue    TEXT,
    dia_chi       TEXT,
    tinh_thanh    TEXT,                         -- officePro (mã tỉnh)
    quan_huyen    TEXT,                         -- officeDis (mã huyện)
    trang_thai    TEXT,
    ngay_hieu_luc TIMESTAMPTZ,
    extra_data    JSONB NOT NULL DEFAULT '{}',
    data_hash     TEXT,
    crawled_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_investors_ten    ON investors(ten);
CREATE INDEX IF NOT EXISTS idx_investors_mst    ON investors(ma_so_thue);
CREATE INDEX IF NOT EXISTS idx_investors_tinh   ON investors(tinh_thanh);
CREATE INDEX IF NOT EXISTS idx_investors_hl     ON investors(ngay_hieu_luc DESC);
CREATE INDEX IF NOT EXISTS idx_investors_extra  ON investors USING GIN (extra_data);

-- ─── Nhà thầu nước ngoài thắng thầu tại VN ───────────────────────────
-- URL: /web/guest/foreign-contractor-winning-bid-vn
-- API: egp-portal-international-contractors/services/po-winning-contractors
-- Key field: id (UUID)
CREATE TABLE IF NOT EXISTS foreign_contractors (
    id              BIGSERIAL PRIMARY KEY,
    nttnn_id        TEXT UNIQUE NOT NULL,       -- id (UUID từ API)
    ten             TEXT,                       -- contractorName
    ten_hop_dong    TEXT,                       -- contractName
    thong_tin_chung TEXT,                       -- generalInfo
    dia_chi_nn      TEXT,                       -- addressForeign
    dia_chi_nn_ct   TEXT,                       -- addressForeignDetail (JSON string)
    dia_chi_vn      TEXT,                       -- addressVn
    dia_chi_vn_ct   TEXT,                       -- addressVnDetail (JSON string)
    ngay_bat_dau    TIMESTAMPTZ,                -- excuteFromDate
    ngay_ket_thuc   TIMESTAMPTZ,                -- excuteToDate
    ngay_cong_bo    TIMESTAMPTZ,                -- publicDate
    trang_thai      TEXT,                       -- status: "01" = active
    nguoi_tao       TEXT,                       -- createdBy
    ngay_tao        TIMESTAMPTZ,                -- createdDate
    thong_tin_khac  TEXT,                       -- otherInfo (JSON string)
    extra_data      JSONB NOT NULL DEFAULT '{}',
    data_hash       TEXT,
    crawled_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fctors_ten       ON foreign_contractors(ten);
CREATE INDEX IF NOT EXISTS idx_fctors_cbo       ON foreign_contractors(ngay_cong_bo DESC);
CREATE INDEX IF NOT EXISTS idx_fctors_extra     ON foreign_contractors USING GIN (extra_data);

-- ─── Nhà đầu tư được phê duyệt (v2) ─────────────────────────────────
-- URL: /web/guest/investors-approval-v2
-- API: egp-portal-investor-approved-v2/services/um/lookup-orgInfo
-- Response: {"ebidOrgInfos": {"content": [...], "totalElements": N}}
-- parentName = "CDT"
CREATE TABLE IF NOT EXISTS investors_v2 (
    id                  BIGSERIAL PRIMARY KEY,
    org_code            TEXT UNIQUE NOT NULL,
    ten                 TEXT,                   -- orgFullname
    ma_so_thue          TEXT,                   -- taxCode
    dia_chi             TEXT,                   -- officeAdd
    tinh_thanh          TEXT,                   -- officePro
    quan_huyen          TEXT,                   -- officeDis
    dien_thoai          TEXT,                   -- officePhone
    email               TEXT,                   -- recEmail
    nguoi_dai_dien      TEXT,                   -- repFullname
    loai_hinh           TEXT,                   -- parentName: CDT
    loai_dn             TEXT,                   -- businessType: NON_BUSINESS_UNIT | LLC2 | SC | ...
    trang_thai          TEXT,                   -- status
    trang_thai_to_chuc  TEXT,                   -- statusOrg
    ngay_hieu_luc       TIMESTAMPTZ,            -- effRoleDate [y,m,d,h,min,sec]
    ngay_het_han        TIMESTAMPTZ,            -- expTime [y,m,d,h,min,sec]
    quoc_gia_thue       TEXT,                   -- taxNation: VN | ...
    extra_data          JSONB NOT NULL DEFAULT '{}',
    data_hash           TEXT,
    crawled_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_inv2_ten     ON investors_v2(ten);
CREATE INDEX IF NOT EXISTS idx_inv2_mst     ON investors_v2(ma_so_thue);
CREATE INDEX IF NOT EXISTS idx_inv2_tinh    ON investors_v2(tinh_thanh);
CREATE INDEX IF NOT EXISTS idx_inv2_hl      ON investors_v2(ngay_hieu_luc DESC);
CREATE INDEX IF NOT EXISTS idx_inv2_extra   ON investors_v2 USING GIN (extra_data);

-- ─── Bên mời thầu được phê duyệt ─────────────────────────────────────
-- URL: /web/guest/bid-solicitor-approval
-- API: egp-portal-bid-solicitor-approved/services/um/lookup-orgInfo
-- Response: {"ebidOrgInfos": {"content": [...], "totalElements": N}}
-- parentName = "BMT"
CREATE TABLE IF NOT EXISTS bid_solicitors (
    id                  BIGSERIAL PRIMARY KEY,
    org_code            TEXT UNIQUE NOT NULL,
    ten                 TEXT,
    ma_so_thue          TEXT,
    dia_chi             TEXT,
    tinh_thanh          TEXT,
    quan_huyen          TEXT,
    dien_thoai          TEXT,
    email               TEXT,
    nguoi_dai_dien      TEXT,
    loai_hinh           TEXT,                   -- parentName: BMT
    loai_dn             TEXT,
    trang_thai          TEXT,
    trang_thai_to_chuc  TEXT,
    ngay_hieu_luc       TIMESTAMPTZ,
    ngay_het_han        TIMESTAMPTZ,
    quoc_gia_thue       TEXT,
    extra_data          JSONB NOT NULL DEFAULT '{}',
    data_hash           TEXT,
    crawled_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bsol_ten     ON bid_solicitors(ten);
CREATE INDEX IF NOT EXISTS idx_bsol_mst     ON bid_solicitors(ma_so_thue);
CREATE INDEX IF NOT EXISTS idx_bsol_tinh    ON bid_solicitors(tinh_thanh);
CREATE INDEX IF NOT EXISTS idx_bsol_hl      ON bid_solicitors(ngay_hieu_luc DESC);
CREATE INDEX IF NOT EXISTS idx_bsol_extra   ON bid_solicitors USING GIN (extra_data);

-- ─── Tra cứu tỉnh/thành phố ─────────────────────────────────────────
-- Seed từ crawler/seeder.py khi khởi động lần đầu
CREATE TABLE IF NOT EXISTS provinces (
    code    TEXT PRIMARY KEY,   -- officePro từ API, ví dụ "79"
    name    TEXT NOT NULL       -- "Thành phố Hồ Chí Minh"
);

-- ─── Tra cứu quận/huyện ───────────────────────────────────────────────
-- Seed từ cat-areas API (nếu lấy được); để trống nếu không lấy được
CREATE TABLE IF NOT EXISTS districts (
    code          TEXT PRIMARY KEY,   -- officeDis từ API, ví dụ "30589"
    name          TEXT NOT NULL,
    province_code TEXT REFERENCES provinces(code)
);
CREATE INDEX IF NOT EXISTS idx_districts_prov ON districts(province_code);

-- ─── Log mỗi lần crawl ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS profile_crawl_log (
    id            BIGSERIAL PRIMARY KEY,
    source        TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    n_total       INTEGER,
    n_new         INTEGER,
    n_updated     INTEGER,
    n_unchanged   INTEGER,
    n_error       INTEGER NOT NULL DEFAULT 0,
    stopped_early BOOLEAN NOT NULL DEFAULT FALSE,
    status        TEXT NOT NULL DEFAULT 'running',
    error_msg     TEXT
);
CREATE INDEX IF NOT EXISTS idx_pcl_source   ON profile_crawl_log(source);
CREATE INDEX IF NOT EXISTS idx_pcl_started  ON profile_crawl_log(started_at DESC);
