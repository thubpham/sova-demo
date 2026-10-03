import json
import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path

ROOT = Path(__file__).parent
SEED_PATH = ROOT / "mock_data.json"
DB_PATH = ROOT / "erp.sqlite"

TABLES = ["customers", "products", "inventory", "vendors", "vendor_prices", "purchase_orders"]

SCHEMA = """
CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY,
    ten_cong_ty TEXT NOT NULL,
    ma_so_thue TEXT NOT NULL,
    han_muc_cong_no_vnd INTEGER NOT NULL,
    cong_no_hien_tai_vnd INTEGER NOT NULL,
    dieu_khoan_thanh_toan TEXT NOT NULL,
    trang_thai TEXT NOT NULL
);
CREATE TABLE products (
    sku TEXT PRIMARY KEY,
    ten_san_pham TEXT NOT NULL,
    don_vi TEXT NOT NULL,
    gia_ban_vnd INTEGER NOT NULL
);
CREATE TABLE inventory (
    sku TEXT PRIMARY KEY REFERENCES products(sku),
    ton_kho INTEGER NOT NULL,
    diem_dat_hang_lai INTEGER NOT NULL,
    kho TEXT NOT NULL
);
CREATE TABLE vendors (
    vendor_id TEXT PRIMARY KEY,
    ten_ncc TEXT NOT NULL,
    ma_so_thue TEXT NOT NULL,
    co_hoa_don_gtgt INTEGER NOT NULL,
    danh_gia_giao_hang REAL NOT NULL,
    dieu_khoan_cong_no TEXT NOT NULL
);
CREATE TABLE vendor_prices (
    vendor_id TEXT NOT NULL REFERENCES vendors(vendor_id),
    sku TEXT NOT NULL REFERENCES products(sku),
    gia_vnd INTEGER NOT NULL,
    thoi_gian_giao_ngay INTEGER NOT NULL,
    so_luong_toi_thieu INTEGER NOT NULL,
    PRIMARY KEY (vendor_id, sku)
);
CREATE TABLE purchase_orders (
    po_id TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL REFERENCES vendors(vendor_id),
    sku TEXT NOT NULL REFERENCES products(sku),
    so_luong INTEGER NOT NULL,
    don_gia_vnd INTEGER NOT NULL,
    thanh_tien_vnd INTEGER NOT NULL,
    thoi_gian_giao_ngay INTEGER NOT NULL,
    trang_thai TEXT NOT NULL,
    ngay_tao TEXT NOT NULL
);
"""


def build(reset: bool = False) -> Path:
    if reset:
        DB_PATH.unlink(missing_ok=True)
    if DB_PATH.exists():
        return DB_PATH
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    staging = DB_PATH.with_name(DB_PATH.name + "-build")
    staging.unlink(missing_ok=True)
    with closing(sqlite3.connect(staging)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        for table in TABLES:
            for row in seed.get(table, []):
                cols = ", ".join(row)
                marks = ", ".join(f":{k}" for k in row)
                conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", row)
    staging.replace(DB_PATH)
    return DB_PATH


def connect() -> sqlite3.Connection:
    build()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def query(sql: str, params=()) -> list[dict]:
    with closing(connect()) as conn:
        return [dict(row) for row in conn.execute(sql, params)]


@contextmanager
def transaction():
    with closing(connect()) as conn, conn:
        yield conn


if __name__ == "__main__":
    path = build(reset="--reset" in sys.argv)
    counts = {t: query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in TABLES}
    print(path.name, counts)
