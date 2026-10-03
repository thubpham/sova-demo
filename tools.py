from datetime import date

from langchain_core.tools import tool

import erp

PO_STATUSES = {"nhap", "da_duyet", "tu_choi"}


def _one(sql: str, params: tuple) -> dict | None:
    rows = erp.query(sql, params)
    return rows[0] if rows else None


def _not_found(kind: str, key: str) -> dict:
    return {"loi": f"Không tìm thấy {kind} {key}"}


@tool
def lookup_customer(customer_id: str) -> dict:
    """Tra cứu hồ sơ khách hàng: tên, mã số thuế, hạn mức công nợ, công nợ hiện tại, điều khoản thanh toán, trạng thái."""
    customer = _one("SELECT * FROM customers WHERE customer_id = ?", (customer_id,))
    return customer or _not_found("khách hàng", customer_id)


@tool
def check_order_terms(customer_id: str, sku: str, quantity: int) -> dict:
    """Kiểm tra đơn hàng có nằm trong hạn mức công nợ còn lại của khách hàng không; giá bán lấy từ hệ thống."""
    customer = _one("SELECT * FROM customers WHERE customer_id = ?", (customer_id,))
    if not customer:
        return _not_found("khách hàng", customer_id)
    product = _one("SELECT * FROM products WHERE sku = ?", (sku,))
    if not product:
        return _not_found("sản phẩm", sku)
    if quantity <= 0:
        return {"loi": "Số lượng phải lớn hơn 0"}

    gia_tri = quantity * product["gia_ban_vnd"]
    con_lai = customer["han_muc_cong_no_vnd"] - customer["cong_no_hien_tai_vnd"]
    if customer["trang_thai"] != "hoat_dong":
        hop_le, ly_do = False, f"Khách hàng đang ở trạng thái {customer['trang_thai']}"
    elif gia_tri > con_lai:
        hop_le, ly_do = False, f"Giá trị đơn {gia_tri:,} VND vượt hạn mức công nợ còn lại {con_lai:,} VND"
    else:
        hop_le, ly_do = True, f"Giá trị đơn {gia_tri:,} VND nằm trong hạn mức công nợ còn lại {con_lai:,} VND"
    return {
        "hop_le": hop_le,
        "gia_tri_don_hang_vnd": gia_tri,
        "cong_no_con_lai_vnd": con_lai,
        "dieu_khoan_thanh_toan": customer["dieu_khoan_thanh_toan"],
        "ly_do": ly_do,
    }


@tool
def check_stock(sku: str) -> dict:
    """Tra cứu số lượng tồn kho thực tế và kho chứa của một SKU."""
    row = _one(
        "SELECT i.sku, p.ten_san_pham, p.don_vi, i.ton_kho, i.kho "
        "FROM inventory i JOIN products p USING (sku) WHERE i.sku = ?",
        (sku,),
    )
    return row or _not_found("SKU", sku)


@tool
def get_reorder_point(sku: str) -> dict:
    """Tra cứu điểm đặt hàng lại của một SKU và cho biết tồn kho đã xuống dưới điểm này chưa."""
    row = _one("SELECT sku, ton_kho, diem_dat_hang_lai FROM inventory WHERE sku = ?", (sku,))
    if not row:
        return _not_found("SKU", sku)
    return {**row, "duoi_diem_dat_hang_lai": row["ton_kho"] < row["diem_dat_hang_lai"]}


@tool
def lookup_vendor_price(sku: str) -> list[dict] | dict:
    """Liệt kê các nhà cung cấp bán SKU này kèm giá, thời gian giao, số lượng tối thiểu, hóa đơn GTGT và đánh giá giao hàng."""
    rows = erp.query(
        "SELECT vp.vendor_id, v.ten_ncc, vp.gia_vnd, vp.thoi_gian_giao_ngay, vp.so_luong_toi_thieu, "
        "v.co_hoa_don_gtgt, v.danh_gia_giao_hang, v.dieu_khoan_cong_no "
        "FROM vendor_prices vp JOIN vendors v USING (vendor_id) WHERE vp.sku = ? ORDER BY vp.gia_vnd",
        (sku,),
    )
    if not rows:
        return {"loi": f"Không có nhà cung cấp nào bán SKU {sku}"}
    return [{**r, "co_hoa_don_gtgt": bool(r["co_hoa_don_gtgt"])} for r in rows]


@tool
def create_purchase_order(vendor_id: str, sku: str, qty: int) -> dict:
    """Tạo đơn mua hàng nháp với nhà cung cấp; đơn giá lấy từ bảng giá, đơn chưa có hiệu lực cho đến khi được duyệt."""
    with erp.transaction() as conn:
        price = conn.execute(
            "SELECT * FROM vendor_prices WHERE vendor_id = ? AND sku = ?", (vendor_id, sku)
        ).fetchone()
        if not price:
            return {"loi": f"Nhà cung cấp {vendor_id} không bán SKU {sku}"}
        if qty < price["so_luong_toi_thieu"]:
            return {"loi": f"Số lượng {qty} thấp hơn mức tối thiểu {price['so_luong_toi_thieu']} của {vendor_id}"}

        today = date.today().isoformat()
        seq = conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE ngay_tao = ?", (today,)).fetchone()[0] + 1
        po = {
            "po_id": f"PO-{today.replace('-', '')}-{seq:03d}",
            "vendor_id": vendor_id,
            "sku": sku,
            "so_luong": qty,
            "don_gia_vnd": price["gia_vnd"],
            "thanh_tien_vnd": qty * price["gia_vnd"],
            "thoi_gian_giao_ngay": price["thoi_gian_giao_ngay"],
            "trang_thai": "nhap",
            "ngay_tao": today,
        }
        conn.execute(
            f"INSERT INTO purchase_orders ({', '.join(po)}) VALUES ({', '.join(':' + k for k in po)})", po
        )
    return po


def set_po_status(po_id: str, trang_thai: str) -> dict:
    if trang_thai not in PO_STATUSES:
        raise ValueError(f"Trạng thái không hợp lệ: {trang_thai}")
    with erp.transaction() as conn:
        updated = conn.execute(
            "UPDATE purchase_orders SET trang_thai = ? WHERE po_id = ?", (trang_thai, po_id)
        ).rowcount
        if not updated:
            raise KeyError(f"Không tìm thấy đơn mua hàng {po_id}")
        return dict(conn.execute("SELECT * FROM purchase_orders WHERE po_id = ?", (po_id,)).fetchone())


TOOLS = {
    t.name: t
    for t in [
        lookup_customer,
        check_order_terms,
        check_stock,
        get_reorder_point,
        lookup_vendor_price,
        create_purchase_order,
    ]
}
