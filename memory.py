import json
import sqlite3
import sys
from functools import cache
from pathlib import Path

from langgraph.store.base import BaseStore
from langgraph.store.sqlite import SqliteStore

ROOT = Path(__file__).parent
DB_PATH = ROOT / "sova_demo.sqlite"
IDENTITIES_PATH = ROOT / "identities.json"

SCHEMAS = {
    "nha_cung_cap": {"ten_ncc", "do_tin_cay", "uu_tien", "lan_chon_gan_nhat", "ghi_chu"},
    "khach_hang": {"uu_tien_giao_hang", "san_pham_hay_mua", "ghi_chu"},
    "quyet_dinh": {"customer_id", "nha_cung_cap_da_chon", "sku", "so_tien_vnd", "nguoi_duyet", "ly_do", "thoi_diem"},
}

SEED = {
    "nha_cung_cap": {
        "NCC-001": {
            "ten_ncc": "Công ty TNHH Cơ điện Việt Tiến",
            "do_tin_cay": 0.85,
            "uu_tien": 2,
            "lan_chon_gan_nhat": "2026-05-10",
            "ghi_chu": "Hóa đơn GTGT đầy đủ, giao đúng hạn; thời gian giao dài hơn mặt bằng chung.",
        },
        "NCC-002": {
            "ten_ncc": "Cơ sở Thiết bị Phúc Long",
            "do_tin_cay": 0.5,
            "uu_tien": 3,
            "lan_chon_gan_nhat": "2026-03-20",
            "ghi_chu": "Giá rẻ nhưng không xuất được hóa đơn GTGT; chỉ dùng khi khẩn cấp.",
        },
        "NCC-003": {
            "ten_ncc": "Công ty Cổ phần Thiết bị Công nghiệp Sài Gòn",
            "do_tin_cay": 0.85,
            "uu_tien": 2,
            "lan_chon_gan_nhat": "2026-07-22",
            "ghi_chu": "Giao nhanh; từng trễ 2 đơn vào mùa cao điểm.",
        },
    },
    "khach_hang": {
        "KH-001": {
            "uu_tien_giao_hang": "Giao trong giờ hành chính, ưu tiên giao đủ một lần",
            "san_pham_hay_mua": ["SP-123", "SP-456"],
            "ghi_chu": "Thanh toán đúng hạn; chấp nhận chờ thêm vài ngày để nhận hàng có hóa đơn GTGT.",
        },
    },
    "quyet_dinh": {
        "DH-2026-0042": {
            "customer_id": "KH-002",
            "nha_cung_cap_da_chon": "NCC-003",
            "sku": "SP-789",
            "so_tien_vnd": 13000000,
            "nguoi_duyet": "truongphong.thumua@besgroup.vn",
            "ly_do": "Nhà cung cấp duy nhất bán SP-789; có hóa đơn GTGT.",
            "thoi_diem": "2026-07-22T10:30:00+07:00",
        },
    },
}


@cache
def _permissions() -> dict[str, dict[str, set[str]]]:
    identities = json.loads(IDENTITIES_PATH.read_text(encoding="utf-8"))
    return {
        a["id"]: {mode: set(ns) for mode, ns in a["memory_namespaces"].items()}
        for a in identities["agents"]
    }


def _check(agent_id: str, mode: str, namespace: str) -> None:
    if namespace not in _permissions().get(agent_id, {}).get(mode, set()):
        raise PermissionError(f"{agent_id} không có quyền {mode} namespace {namespace}")


def get_store() -> SqliteStore:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    store = SqliteStore(conn)
    store.setup()
    return store


def read(store: BaseStore, agent_id: str, namespace: str, key: str | None = None) -> dict:
    _check(agent_id, "read", namespace)
    if key is not None:
        item = store.get((namespace,), key)
        return item.value if item else {}
    return {item.key: item.value for item in store.search((namespace,), limit=1000)}


def read_for_agent(store: BaseStore, agent_id: str) -> dict[str, dict]:
    return {ns: read(store, agent_id, ns) for ns in sorted(_permissions()[agent_id]["read"])}


def write(store: BaseStore, agent_id: str, namespace: str, key: str, value: dict) -> None:
    _check(agent_id, "write", namespace)
    missing = SCHEMAS.get(namespace, set()) - value.keys()
    if missing:
        raise ValueError(f"Thiếu trường {sorted(missing)} cho namespace {namespace}")
    store.put((namespace,), key, value, index=False)


def seed(store: BaseStore, reset: bool = False) -> bool:
    if reset:
        for namespace in SCHEMAS:
            for item in store.search((namespace,), limit=1000):
                store.delete((namespace,), item.key)
    elif any(store.search((namespace,), limit=1) for namespace in SCHEMAS):
        return False
    for namespace, rows in SEED.items():
        for key, value in rows.items():
            store.put((namespace,), key, value, index=False)
    return True


if __name__ == "__main__":
    store = get_store()
    seeded = seed(store, reset="--reset" in sys.argv)
    counts = {ns: len(store.search((ns,), limit=1000)) for ns in SCHEMAS}
    print(DB_PATH.name, "seeded" if seeded else "unchanged", counts)
