import os
import sqlite3
import uuid
from datetime import datetime

from flask import Flask, jsonify, render_template, request, url_for
import qrcode

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_ROOT, "ledger.db")
QR_DIR = os.path.join(APP_ROOT, "static", "qrcodes")
DEFAULT_PRODUCT = {
    "id": "6001007334581",
    "name": "AuthentiSip Demo Bottle",
    "batch_id": "BATCH-001",
    "status": "authentic",
}

app = Flask(__name__)


def ensure_dirs():
    os.makedirs(QR_DIR, exist_ok=True)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS scans (
            id TEXT PRIMARY KEY,
            product_id TEXT,
            ip TEXT NOT NULL,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL,
            ip TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    cur.execute("PRAGMA table_info(products)")
    columns = [row[1] for row in cur.fetchall()]
    if "status" not in columns:
        cur.execute("ALTER TABLE products ADD COLUMN status TEXT NOT NULL DEFAULT 'authentic'")
        conn.commit()
    conn.close()


def seed_default_product():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM products")
    has_products = cur.fetchone()[0] > 0
    if not has_products:
        cur.execute(
            "INSERT INTO products (id, name, batch_id, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                DEFAULT_PRODUCT["id"],
                DEFAULT_PRODUCT["name"],
                DEFAULT_PRODUCT["batch_id"],
                DEFAULT_PRODUCT["status"],
                now_iso(),
            ),
        )
        conn.commit()
    conn.close()


def bootstrap_app():
    ensure_dirs()
    init_db()
    seed_default_product()


def now_iso():
    return datetime.utcnow().isoformat() + "Z"


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def parse_payload():
    data = request.get_json(silent=True)
    if not data:
        data = request.form.to_dict()
    return data or {}


bootstrap_app()


@app.route("/")
def scan_page():
    return render_template("scan.html")


@app.route("/admin")
def admin_page():
    return render_template("index.html")


@app.route("/admin/register", methods=["POST"])
def register_product():
    data = parse_payload()
    name = (data.get("name") or "").strip()
    batch_id = (data.get("batch_id") or "").strip()
    product_id = (data.get("product_id") or "").strip()

    if not name:
        name = "Test Product"
    if not batch_id:
        batch_id = "BATCH-001"

    if not product_id:
        return jsonify({"error": "product_id (barcode) is required"}), 400

    is_update = False
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE id = ?", (product_id,))
    row = cur.fetchone()
    if row:
        is_update = True
        cur.execute(
            "UPDATE products SET name = ?, batch_id = ?, status = ? WHERE id = ?",
            (name, batch_id, "authentic", product_id),
        )
        conn.commit()
    else:
        created_at = now_iso()
        cur.execute(
            "INSERT INTO products (id, name, batch_id, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (product_id, name, batch_id, "authentic", created_at),
        )
        conn.commit()
    conn.close()

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE id = ?", (product_id,))
    row = cur.fetchone()
    conn.close()

    base_url = request.host_url.rstrip("/")
    qr_payload = f"{base_url}/?product_id={product_id}"
    qr_img = qrcode.make(qr_payload)
    qr_filename = f"{product_id}.png"
    qr_path = os.path.join(QR_DIR, qr_filename)
    qr_img.save(qr_path)

    qr_url = url_for("static", filename=f"qrcodes/{qr_filename}")

    return jsonify(
        {
            "product": {
                "id": row["id"] if row else product_id,
                "name": row["name"] if row else name,
                "batch_id": row["batch_id"] if row else batch_id,
                "created_at": row["created_at"] if row else now_iso(),
            },
            "qr_url": qr_url,
            "qr_payload": qr_payload,
            "updated": is_update,
        }
    )


@app.route("/verify_product", methods=["POST"])
def verify_product():
    data = parse_payload()
    product_id = (data.get("product_id") or "").strip()

    if not product_id:
        return jsonify({"error": "product_id is required"}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE id = ?", (product_id,))
    row = cur.fetchone()

    status = "Authentic" if row else "Fake"
    scan_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO scans (id, product_id, ip, result, created_at) VALUES (?, ?, ?, ?, ?)",
        (scan_id, product_id, client_ip(), status, now_iso()),
    )
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM scans WHERE result = 'Fake'")
    fake_count = cur.fetchone()[0]
    cur.execute("SELECT created_at FROM scans WHERE result = 'Fake' ORDER BY created_at DESC LIMIT 1")
    last_fake = cur.fetchone()
    conn.close()

    if row:
        return jsonify(
            {
                "status": status,
                "product": {
                    "id": row["id"],
                    "name": row["name"],
                    "batch_id": row["batch_id"],
                    "created_at": row["created_at"],
                    "status": row["status"],
                },
                "alerts": {
                    "fake_count": fake_count,
                    "last_fake_at": last_fake[0] if last_fake else None,
                },
            }
        )

    return jsonify(
        {
            "status": status,
            "product_id": product_id,
            "alerts": {
                "fake_count": fake_count,
                "last_fake_at": last_fake[0] if last_fake else None,
            },
        }
    )


@app.route("/stats", methods=["GET"])
def stats():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM scans WHERE result = 'Fake'")
    fake_count = cur.fetchone()[0]
    cur.execute("SELECT created_at FROM scans WHERE result = 'Fake' ORDER BY created_at DESC LIMIT 1")
    last_fake = cur.fetchone()
    conn.close()
    return jsonify(
        {
            "fake_count": fake_count,
            "last_fake_at": last_fake[0] if last_fake else None,
        }
    )


@app.route("/report_product", methods=["POST"])
def report_product():
    data = parse_payload()
    product_id = (data.get("product_id") or "").strip()
    note = (data.get("note") or "").strip()

    if not product_id:
        return jsonify({"error": "product_id is required"}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO reports (id, product_id, ip, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), product_id, client_ip(), note or None, now_iso()),
    )
    conn.commit()
    conn.close()

    return jsonify({"status": "reported"})


if __name__ == "__main__":
    app.run(debug=True)
