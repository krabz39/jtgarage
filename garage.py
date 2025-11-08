# garage.py — J&T Garage WebApp (Orders + Stock + Payments + Reconciliation)
# Flask + SQLite + Admin CRUD + M-Pesa + 3D Builder
# pip install flask python-dotenv requests werkzeug pillow

import os, base64, datetime, json, sqlite3, secrets, requests, shutil, time
from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, session, flash
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from PIL import Image
from functools import wraps

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["SECRET_KEY"] = os.getenv("APP_SECRET", secrets.token_hex(16))
app.config["UPLOAD_FOLDER"] = os.path.join(app.static_folder, "uploads")
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

DB_PATH = "jtgarage.db"

# ---------- Brand ----------
BRAND = {
    "name": "J&T Garage",
    "tagline": "Landrover specialist & Automotive builders",
    "accent": "#1C6B3C"
}

# ---------- DB ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db(); cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        price_kes REAL,
        category TEXT,
        description TEXT,
        models TEXT,
        img TEXT,
        stock INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password_hash TEXT,
        role TEXT DEFAULT 'admin'
    );

    -- Orders header
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT,
        ship_to TEXT,
        total_kes REAL,
        status TEXT DEFAULT 'pending',   -- pending, paid, shipped, complete, cancelled
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    -- Order items
    CREATE TABLE IF NOT EXISTS order_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        product_id INTEGER,
        qty INTEGER,
        price_kes REAL,
        FOREIGN KEY(order_id) REFERENCES orders(id)
    );

    -- Payments
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        method TEXT,      -- mpesa, cash, bank
        ref TEXT,
        amount REAL,
        status TEXT,      -- initiated, confirmed, failed
        payload TEXT,     -- raw API response if any
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    -- Stock movements
    CREATE TABLE IF NOT EXISTS stock_movements(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER,
        change INTEGER,         -- + restock, - sale
        reason TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    # Ensure columns exist if migrating from older versions
    cols = [r[1] for r in cur.execute("PRAGMA table_info(products)").fetchall()]
    if "description" not in cols:
        cur.execute("ALTER TABLE products ADD COLUMN description TEXT DEFAULT ''")
        print("[migrate] Added 'description' column to products table")

    # Seed default admin
    if not cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        cur.execute("INSERT INTO users(username,password_hash) VALUES(?,?)",
                    (os.getenv("ADMIN_USER", "admin"),
                     generate_password_hash(os.getenv("ADMIN_PASS", "admin123"))))
        print("[seed] Admin account created (see .env)")
    con.commit(); con.close()

init_db()

# ---------- Auth ----------
def login_required(fn):
    @wraps(fn)
    def wrap(*a, **kw):
        if "user_id" not in session:
            return redirect(url_for("admin_page"))
        return fn(*a, **kw)
    return wrap

# ---------- Helpers ----------
def to_num(v, default=0):
    try:
        return float(v)
    except Exception:
        return float(default)

def save_image(fs):
    fn = secure_filename(fs.filename)
    dest = os.path.join(app.config["UPLOAD_FOLDER"], fn)
    fs.save(dest)
    try:
        im = Image.open(dest); im.thumbnail((1600, 1600)); im.save(dest, optimize=True, quality=85)
    except: 
        pass
    return f"uploads/{fn}"

# ---------- M-Pesa ----------
BASE = "https://sandbox.safaricom.co.ke" if os.getenv("DARAJA_ENV", "sandbox") == "sandbox" else "https://api.safaricom.co.ke"

def mpesa_configured():
    keys = ["MPESA_CONSUMER_KEY","MPESA_CONSUMER_SECRET","MPESA_SHORTCODE","MPESA_PASSKEY","CALLBACK_BASE"]
    return all(os.getenv(k) for k in keys)

def daraja_token():
    r = requests.get(f"{BASE}/oauth/v1/generate?grant_type=client_credentials",
        auth=(os.getenv("MPESA_CONSUMER_KEY",""), os.getenv("MPESA_CONSUMER_SECRET","")))
    try:
        return r.json().get("access_token", "")
    except:
        return ""

def stk_push(phone, amount):
    # If not configured, simulate success so your flow works in dev
    if not mpesa_configured():
        return 200, {
            "Mock": True,
            "MerchantRequestID":"mock-req",
            "CheckoutRequestID":"mock-checkout",
            "ResponseCode":"0",
            "CustomerMessage":"[MOCK] STK push simulated",
            "amount": int(amount),
            "phone": phone
        }

    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    pw = base64.b64encode(
        f"{os.getenv('MPESA_SHORTCODE')}{os.getenv('MPESA_PASSKEY')}{timestamp}".encode()
    ).decode()
    payload = {
        "BusinessShortCode": os.getenv("MPESA_SHORTCODE"),
        "Password": pw, "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(amount),
        "PartyA": phone, "PartyB": os.getenv("MPESA_SHORTCODE"),
        "PhoneNumber": phone,
        "CallBackURL": os.getenv("CALLBACK_BASE") + "/mpesa/callback",
        "AccountReference": "JT-GARAGE", "TransactionDesc": "Defender parts"
    }
    h = {"Authorization": f"Bearer {daraja_token()}", "Content-Type": "application/json"}
    r = requests.post(f"{BASE}/mpesa/stkpush/v1/processrequest", headers=h, json=payload)
    return r.status_code, r.json()

# ---------- Public ----------
@app.route("/")
def home():
    return render_template("home.html", brand=BRAND)

@app.route("/builder")
def builder():
    return render_template("builder.html", brand=BRAND)

@app.route("/api/categories")
def api_categories():
    con = db()
    rows = con.execute("SELECT DISTINCT category FROM products WHERE IFNULL(category,'')<>'' ORDER BY 1").fetchall()
    con.close()
    return jsonify([r["category"] for r in rows])

@app.route("/api/products")
def api_products():
    con = db(); cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='products'")
    if not cur.fetchone():
        con.close()
        return jsonify({"error": "products table missing; visit /admin/repair-db to fix"}), 500

    # Optional filtering/sorting for grid
    q = (request.args.get("q") or "").strip().lower()
    category = (request.args.get("category") or "").strip()
    sort = request.args.get("sort") or "newest"

    sql = "SELECT * FROM products"
    params = []
    where = []
    if q:
        where.append("(LOWER(name) LIKE ? OR LOWER(category) LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if category:
        where.append("category LIKE ?")
        params += [f"%{category}%"]
    if where:
        sql += " WHERE " + " AND ".join(where)
    if sort == "price_asc":
        sql += " ORDER BY price_kes ASC"
    elif sort == "price_desc":
        sql += " ORDER BY price_kes DESC"
    else:
        sql += " ORDER BY datetime(created_at) DESC"

    rows = cur.execute(sql, params).fetchall()
    con.close()
    return jsonify([
        {**dict(r), "models": json.loads(r["models"] or "[]")}
        for r in rows
    ])

# ---------- Checkout & M-Pesa ----------
@app.route("/checkout", methods=["POST"])
def checkout():
    data = request.get_json(force=True)
    phone = (data or {}).get("phone")
    ship_to = (data or {}).get("ship_to", "")
    items = (data or {}).get("items", [])

    if not phone or not items:
        return jsonify({"ok": False, "error": "Missing phone or items"}), 400

    # Items can come with price_kes or price; be tolerant
    clean_items = []
    for it in items:
        pid = int(it.get("id"))
        qty = int(it.get("qty", 1))
        price = to_num(it.get("price_kes", it.get("price", 0)))
        name = it.get("name","Item")
        img = it.get("img","")
        clean_items.append({"id": pid, "qty": qty, "price_kes": price, "name": name, "img": img})

    total = sum(i["price_kes"] * i["qty"] for i in clean_items)

    # 1) Create Order
    con = db(); cur = con.cursor()
    cur.execute("INSERT INTO orders(phone, ship_to, total_kes, status) VALUES (?,?,?,?)",
                (phone, ship_to, total, "pending"))
    order_id = cur.lastrowid

    # 2) Items + stock decrement + stock movement
    for i in clean_items:
        cur.execute("INSERT INTO order_items(order_id,product_id,qty,price_kes) VALUES (?,?,?,?)",
                    (order_id, i["id"], i["qty"], i["price_kes"]))
        # Decrement stock safely
        cur.execute("UPDATE products SET stock = IFNULL(stock,0) - ? WHERE id=?", (i["qty"], i["id"]))
        cur.execute("INSERT INTO stock_movements(product_id, change, reason) VALUES (?,?,?)",
                    (i["id"], -i["qty"], f"Sale Order #{order_id}"))

    con.commit()

    # 3) Initiate payment (STK push or mock)
    status_code, resp = stk_push(phone, total)
    cur.execute("INSERT INTO payments(order_id, method, ref, amount, status, payload) VALUES (?,?,?,?,?,?)",
                (order_id, "mpesa", (resp.get("CheckoutRequestID") if isinstance(resp, dict) else None),
                 total, "initiated" if status_code == 200 else "failed", json.dumps(resp)))
    con.commit(); con.close()

    return jsonify({"ok": status_code == 200, "order_id": order_id, "amount": total, "mpesa": resp})

@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    # Record callback & mark payment confirmed if successful
    payload = request.json or {}
    try:
        # Extract fields defensively (supports sandbox format)
        checkout_id = payload.get("Body",{}).get("stkCallback",{}).get("CheckoutRequestID")
        result_code = payload.get("Body",{}).get("stkCallback",{}).get("ResultCode")
        amount = None
        meta = payload.get("Body",{}).get("stkCallback",{}).get("CallbackMetadata",{}).get("Item",[])
        for m in meta:
            if m.get("Name") in ("Amount","amount"):
                amount = to_num(m.get("Value",0))
        con = db(); cur = con.cursor()

        # Find payment by ref
        pay = cur.execute("SELECT * FROM payments WHERE ref=?", (checkout_id,)).fetchone()
        if pay:
            new_status = "confirmed" if result_code == 0 else "failed"
            cur.execute("UPDATE payments SET status=?, amount=?, payload=? WHERE id=?",
                        (new_status, amount if amount else pay["amount"], json.dumps(payload), pay["id"]))
            # if confirmed, mark order paid
            if new_status == "confirmed":
                cur.execute("UPDATE orders SET status='paid' WHERE id=?", (pay["order_id"],))
            con.commit()
        con.close()
    except Exception as e:
        print("[mpesa callback error]", e)
    return jsonify({"ResultCode": 0, "ResultDesc": "Received"})

# ---------- Admin (pages) ----------
@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    if request.method == "POST":  # login
        u = request.form.get("username"); p = request.form.get("password")
        con = db(); row = con.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone(); con.close()
        if row and check_password_hash(row["password_hash"], p):
            session["user_id"] = row["id"]
            return redirect(url_for("admin_page"))
        flash("Invalid credentials", "err")
    return render_template("admin.html", brand=BRAND)

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_page"))

@app.route("/admin/status")
def admin_status():
    logged = bool(session.get("user_id"))
    return jsonify({"logged_in": logged, "username": "admin" if logged else None})

# ---------- Admin Products ----------
@app.route("/admin/products/new", methods=["POST"])
@login_required
def admin_new():
    f = request.files.get("img"); img = ""
    if f and f.filename: img = save_image(f)
    models_field = request.form.getlist("models") or []
    con = db()
    con.execute("""INSERT INTO products(name, price_kes, category, description, models, img, stock)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (request.form["name"], to_num(request.form.get("price_kes",0)),
                 request.form.get("category", ""), request.form.get("description", ""),
                 json.dumps(models_field), img, int(request.form.get("stock", 0) or 0)))
    con.commit(); con.close()
    return redirect(url_for("admin_page"))

@app.route("/admin/products/<int:pid>/edit", methods=["POST"])
@login_required
def admin_edit(pid):
    f = request.files.get("img"); img = None
    if f and f.filename: img = save_image(f)
    con = db()
    prod = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not prod:
        con.close(); return "Not found", 404
    models_field = request.form.getlist("models") or []
    con.execute("""UPDATE products
                   SET name=?, price_kes=?, category=?, description=?, models=?, img=?, stock=?
                   WHERE id=?""",
                (request.form["name"], to_num(request.form.get("price_kes",0)),
                 request.form.get("category", ""), request.form.get("description", ""),
                 json.dumps(models_field),
                 img or prod["img"], int(request.form.get("stock", 0) or 0), pid))
    con.commit(); con.close()
    return redirect(url_for("admin_page"))

@app.route("/admin/products/<int:pid>/delete", methods=["POST"])
@login_required
def admin_delete(pid):
    con = db(); con.execute("DELETE FROM products WHERE id=?", (pid,)); con.commit(); con.close()
    return redirect(url_for("admin_page"))

# ---------- Admin Orders / Payments / Stock APIs ----------
@app.route("/api/orders")
@login_required
def api_orders():
    """ List orders with paid amount and item count """
    con = db(); cur = con.cursor()
    rows = cur.execute("""
        SELECT
          o.id, o.phone, o.ship_to, o.total_kes, o.status, o.created_at,
          IFNULL((SELECT SUM(amount) FROM payments WHERE order_id=o.id AND status='confirmed'), 0) AS paid_kes,
          (SELECT COUNT(*) FROM order_items WHERE order_id=o.id) AS items_count
        FROM orders o
        ORDER BY datetime(o.created_at) DESC
    """).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/orders/<int:oid>")
@login_required
def api_order_detail(oid):
    con = db(); cur = con.cursor()
    order = cur.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not order:
        con.close(); return jsonify({"error":"not found"}), 404
    items = cur.execute("""
        SELECT oi.*, p.name, p.img
        FROM order_items oi
        LEFT JOIN products p ON p.id=oi.product_id
        WHERE oi.order_id=?
    """, (oid,)).fetchall()
    pays = cur.execute("SELECT * FROM payments WHERE order_id=? ORDER BY datetime(created_at) DESC", (oid,)).fetchall()
    con.close()
    return jsonify({
        "order": dict(order),
        "items": [dict(i) for i in items],
        "payments": [dict(p) for p in pays]
    })

@app.route("/admin/orders/update_status", methods=["POST"])
@login_required
def admin_order_update_status():
    data = request.get_json(force=True)
    oid = int(data.get("order_id"))
    status = data.get("status")
    if status not in ("pending","paid","shipped","complete","cancelled"):
        return jsonify({"ok":False,"error":"bad status"}), 400
    con = db(); con.execute("UPDATE orders SET status=? WHERE id=?", (status, oid)); con.commit(); con.close()
    return jsonify({"ok": True})

@app.route("/admin/payments/confirm", methods=["POST"])
@login_required
def admin_payment_confirm():
    """
    Manual confirmation (cash/bank or override)
    body: {order_id, amount, method, ref}
    """
    data = request.get_json(force=True)
    oid = int(data.get("order_id"))
    amount = to_num(data.get("amount",0))
    method = data.get("method","cash")
    ref = data.get("ref","manual")
    con = db(); cur = con.cursor()
    cur.execute("INSERT INTO payments(order_id, method, ref, amount, status, payload) VALUES (?,?,?,?,?,?)",
                (oid, method, ref, amount, "confirmed", json.dumps({"manual":True})))
    # Mark order as paid if total covered
    cur.execute("SELECT total_kes FROM orders WHERE id=?", (oid,))
    tot = to_num(cur.fetchone()[0])
    cur.execute("SELECT IFNULL(SUM(amount),0) FROM payments WHERE order_id=? AND status='confirmed'", (oid,))
    paid = to_num(cur.fetchone()[0])
    if paid >= tot:
        cur.execute("UPDATE orders SET status='paid' WHERE id=?", (oid,))
    con.commit(); con.close()
    return jsonify({"ok": True, "paid": paid, "total": tot})

@app.route("/admin/stock/move", methods=["POST"])
@login_required
def admin_stock_move():
    """
    Manual stock adjustment
    body: {product_id, change, reason}
    """
    data = request.get_json(force=True)
    pid = int(data.get("product_id"))
    change = int(data.get("change",0))
    reason = data.get("reason","manual adjust")
    con = db(); cur = con.cursor()
    cur.execute("UPDATE products SET stock = IFNULL(stock,0) + ? WHERE id=?", (change, pid))
    cur.execute("INSERT INTO stock_movements(product_id, change, reason) VALUES (?,?,?)", (pid, change, reason))
    con.commit(); con.close()
    return jsonify({"ok": True})

@app.route("/api/report/reconciliation")
@login_required
def api_report_recon():
    """ Basic reconciliation: order vs paid vs balance """
    con = db(); cur = con.cursor()
    rows = cur.execute("""
        SELECT
          o.id AS order_id,
          o.phone,
          o.total_kes,
          IFNULL(SUM(CASE WHEN p.status='confirmed' THEN p.amount ELSE 0 END),0) AS paid_kes,
          (o.total_kes - IFNULL(SUM(CASE WHEN p.status='confirmed' THEN p.amount ELSE 0 END),0)) AS balance_kes,
          o.status,
          o.created_at
        FROM orders o
        LEFT JOIN payments p ON p.order_id=o.id
        GROUP BY o.id
        ORDER BY datetime(o.created_at) DESC
    """).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

# ---------- DB HEALTH CHECK ----------
@app.route("/admin/repair-db")
def repair_db():
    con = db(); cur = con.cursor()
    required = {
        "products": """CREATE TABLE products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, price_kes REAL, category TEXT,
            description TEXT, models TEXT, img TEXT,
            stock INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
        "users": """CREATE TABLE users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE, password_hash TEXT, role TEXT DEFAULT 'admin')""",
        "orders": """CREATE TABLE orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT, ship_to TEXT, total_kes REAL,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
        "order_items": """CREATE TABLE order_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER, product_id INTEGER, qty INTEGER, price_kes REAL,
            FOREIGN KEY(order_id) REFERENCES orders(id))""",
        "payments": """CREATE TABLE payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER, method TEXT, ref TEXT, amount REAL,
            status TEXT, payload TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
        "stock_movements": """CREATE TABLE stock_movements(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER, change INTEGER, reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)"""
    }
    repaired = []
    for t, ddl in required.items():
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,))
        if not cur.fetchone():
            print(f"[repair] Recreating missing table: {t}")
            cur.execute(ddl)
            repaired.append(t)

    # ensure admin exists
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO users(username,password_hash) VALUES(?,?)",
                    (os.getenv("ADMIN_USER", "admin"),
                     generate_password_hash(os.getenv("ADMIN_PASS", "admin123"))))
        repaired.append("admin_user_seeded")

    con.commit(); con.close()
    return jsonify({"ok": True, "repaired": repaired or "All tables healthy"})

# ---------- AUTO BACKUP ----------
def backup_db():
    src = DB_PATH
    dest = f"backups/jtgarage_{time.strftime('%Y%m%d_%H%M%S')}.db"
    os.makedirs("backups", exist_ok=True)
    shutil.copy(src, dest)
    print("[backup] Database saved to", dest)

@app.before_request
def auto_backup():
    if not hasattr(app, "_last_backup") or (time.time() - app._last_backup > 1800):
        try:
            backup_db(); app._last_backup = time.time()
        except Exception as e:
            print("[backup] error:", e)

if __name__ == "__main__":
    print(f" {BRAND['name']} running on http://127.0.0.1:5000/admin")
    app.run(host="0.0.0.0", port=5000, debug=True)
