from flask import Flask, render_template, request, redirect, session, Response
import sqlite3
import os
from werkzeug.utils import secure_filename
import qrcode
from datetime import datetime
import tensorflow as tf
import numpy as np
import cv2
import logging
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False
app.secret_key = '123'

UPLOAD_FOLDER = os.path.join("static", "uploads", "tablets")
QR_FOLDER = os.path.join("static", "uploads", "qrcodes")
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['QR_FOLDER'] = QR_FOLDER

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['QR_FOLDER'], exist_ok=True)


MODEL_PATH = os.path.join("model", "counterfeit_model.h5")

try:
    model = tf.keras.models.load_model(MODEL_PATH)
    print("AI model loaded successfully")
except Exception as e:
    print("Error loading model:", e)
    model = None

def verify_image(image_path):
    if model is None:
        return "Model Not Loaded"

    try:
        img = cv2.imread(image_path)
        img = cv2.resize(img, (224, 224))
        img = img / 255.0
        img = np.reshape(img, (1, 224, 224, 3))

        prediction = model.predict(img)[0][0]

        return "Genuine" if prediction < 0.5 else "Fake"

    except Exception as e:
        print("Verification error:", e)
        return "Unknown"

def init_db():
    conn = sqlite3.connect('db.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sellers(
        id INTEGER PRIMARY KEY, username TEXT, email TEXT UNIQUE, 
        password TEXT, pan TEXT, aadhar TEXT, 
        verified INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id INTEGER, 
        batch_id TEXT UNIQUE, manufacturer TEXT, expiry_date TEXT, 
        tablet_image BLOB, qr_image BLOB, ai_result TEXT, 
        blockchain_status TEXT, drug_status TEXT DEFAULT 'ACTIVE', created_at TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS admin(
        id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS customers(
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, email TEXT UNIQUE, password TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS verification_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, qr_data TEXT, 
        ai_result TEXT, blockchain_status TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    
    c.execute("INSERT OR IGNORE INTO admin(id,username,password) VALUES(1,'admin','admin123')")
    conn.commit()
    conn.close()

init_db()

def get_db():
    return sqlite3.connect('db.db')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.route('/seller/register', methods=['GET','POST'])
def seller_register():
    if request.method=='POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        pan_file = request.files['pan']
        aadhar_file = request.files['aadhar']
        
        pan_filename = secure_filename(pan_file.filename)
        aadhar_filename = secure_filename(aadhar_file.filename)
        
        pan_file.save(os.path.join(app.config['UPLOAD_FOLDER'], pan_filename))
        aadhar_file.save(os.path.join(app.config['UPLOAD_FOLDER'], aadhar_filename))
        
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO sellers(username,email,password,pan,aadhar) VALUES(?,?,?,?,?)",
                  (username,email,password,pan_filename,aadhar_filename))
        conn.commit()
        conn.close()
        return render_template('index.html', msg="Registered! Wait for Admin Approval.")
    return render_template('seller_register.html')

@app.route('/seller/login', methods=['GET','POST'])
def seller_login():
    if request.method=='POST':
        email = request.form['email']
        password = request.form['password']
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM sellers WHERE email=? AND password=?", (email,password))
        seller = c.fetchone()
        conn.close()
        if seller:
            if seller[7] == 1: return "Account Blocked."
            if seller[6] == 1:
                session['seller_id'] = seller[0]
                session['seller_username'] = seller[1]
                return redirect('/seller/dashboard')
            return "Account pending verification."
        return "Invalid credentials."
    return render_template('seller_login.html')

@app.route('/seller/dashboard', methods=['GET', 'POST'])
def seller_dashboard():
    if 'seller_id' not in session: return redirect('/seller/login')
    
    conn = get_db()
    c = conn.cursor()
    if request.method == 'POST':
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        batch_id = request.form['batch_id']
        manufacturer = request.form['manufacturer']
        expiry_date = request.form['expiry_date']
        image = request.files['tablet_image']
        tablet_bytes = image.read()

        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], "temp.jpg")
        with open(temp_path, "wb") as f: f.write(tablet_bytes)
        ai_result = verify_image(temp_path)
        os.remove(temp_path)

        qr_data = f"{batch_id}|{manufacturer}|{expiry_date}"
        qr = qrcode.make(qr_data)
        qr_temp_path = os.path.join(app.config['QR_FOLDER'], "temp_qr.png")
        qr.save(qr_temp_path)
        with open(qr_temp_path, "rb") as f: qr_bytes = f.read()
        os.remove(qr_temp_path)

        try:
            c.execute("""INSERT INTO products (seller_id, batch_id, manufacturer, expiry_date, 
                      tablet_image, qr_image, ai_result, blockchain_status, created_at) 
                      VALUES (?,?,?,?,?,?,?,?,?)""", 
                      (session['seller_id'], batch_id, manufacturer, expiry_date, 
                       sqlite3.Binary(tablet_bytes), sqlite3.Binary(qr_bytes), ai_result, "REGISTERED", created_at))
            conn.commit()
        except sqlite3.IntegrityError: return "Batch ID exists."

    c.execute("SELECT * FROM products WHERE seller_id=?", (session['seller_id'],))
    products = c.fetchall()
    conn.close()
    return render_template('seller_dashboard.html', products=products)

@app.route('/seller/delete/<int:product_id>', methods=['POST', 'GET'])
def delete_product(product_id):
    if 'seller_id' not in session:
        return redirect('/seller/login')
    
    seller_id = session['seller_id']
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT id FROM products WHERE id=? AND seller_id=?", (product_id, seller_id))
    product = c.fetchone()
    
    if product:
        c.execute("DELETE FROM products WHERE id=?", (product_id,))
        conn.commit()
        print(f"Product {product_id} successfully purged from ledger.")
    
    conn.close()
    
    return redirect('/seller/dashboard')

@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    if request.method=='POST':
        u, p = request.form['username'], request.form['password']
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT * FROM admin WHERE username=? AND password=?", (u,p))
        if c.fetchone():
            session['admin'] = True
            return redirect('/admin/dashboard')
    return render_template('admin_login.html')

@app.route('/admin/dashboard')
def admin_dashboard():
    if 'admin' not in session: return redirect('/admin/login')
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id, username, email, verified, blocked FROM sellers")
    sellers = c.fetchall()
    c.execute("""SELECT p.id, p.batch_id, p.manufacturer, p.expiry_date, p.ai_result, 
              p.blockchain_status, p.drug_status, s.username 
              FROM products p JOIN sellers s ON p.seller_id = s.id""")
    products = c.fetchall()
    conn.close()
    return render_template('admin_dashboard.html', sellers=sellers, products=products)

@app.route('/admin/manage/sellers')
def admin_manage_sellers():
    if 'admin' not in session: return redirect('/admin/login')
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id, username, email, verified, blocked FROM sellers")
    sellers = c.fetchall()
    conn.close()
    return render_template('admin_sellers.html', sellers=sellers)

@app.route('/admin/verify_details/<int:seller_id>')
def admin_verify_details(seller_id):
    if 'admin' not in session: return redirect('/admin/login')
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id, username, email, pan, aadhar FROM sellers WHERE id=?", (seller_id,))
    seller = c.fetchone()
    conn.close()
    if seller:
        return render_template('verify_details.html', seller_id=seller[0], seller_name=seller[1], 
                               pan_filename=seller[3], aadhar_filename=seller[4])
    return "Not Found", 404

@app.route('/admin/verify/<int:seller_id>')
def verify_seller(seller_id):
    if 'admin' not in session: return redirect('/admin/login')
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE sellers SET verified=1 WHERE id=?", (seller_id,))
    conn.commit(); conn.close()
    return redirect('/admin/manage/sellers')

@app.route('/admin/block/seller/<int:seller_id>')
def block_seller(seller_id):
    if 'admin' not in session: return redirect('/admin/login')
    conn = get_db(); c = conn.cursor()
    print('block')
    c.execute("UPDATE sellers SET blocked=1 WHERE id=?", (seller_id,))
    conn.commit(); conn.close()
    return redirect('/admin/manage/sellers')

@app.route('/admin/manage/products')
def admin_manage_products():
    if 'admin' not in session: return redirect('/admin/login')
    conn = get_db(); c = conn.cursor()
    c.execute("""SELECT p.id, p.batch_id, p.manufacturer, p.ai_result, p.blockchain_status, 
              p.drug_status, s.username FROM products p JOIN sellers s ON p.seller_id = s.id""")
    products = c.fetchall()
    conn.close()
    return render_template('admin_products.html', products=products)

@app.route('/admin/flag/drug/<int:product_id>')
def flag_drug(product_id):
    if 'admin' not in session: return redirect('/admin/login')
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE products SET drug_status='SUSPICIOUS' WHERE id=?", (product_id,))
    conn.commit(); conn.close()
    return redirect('/admin/manage/products')

@app.route('/admin/remove/drug/<int:product_id>')
def remove_drug(product_id):
    if 'admin' not in session: return redirect('/admin/login')
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE products SET drug_status='REMOVED' WHERE id=?", (product_id,))
    conn.commit(); conn.close()
    return redirect('/admin/manage/products')

@app.route('/admin/blockchain')
def admin_blockchain():
    if 'admin' not in session: return redirect('/admin/login')
    
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT products.id, products.batch_id, sellers.username, products.created_at, products.ai_result 
                 FROM products 
                 JOIN sellers ON products.seller_id = sellers.id 
                 ORDER BY products.created_at DESC""")
    all_logs = c.fetchall()
    conn.close()
    
    return render_template('admin_blockchain.html', logs=all_logs)

@app.route('/customer/register', methods=['GET','POST'])
def customer_register():
    if request.method == 'POST':
        u, e, p = request.form['username'], request.form['email'], request.form['password']
        conn = get_db(); c = conn.cursor()
        c.execute("INSERT INTO customers(username,email,password) VALUES(?,?,?)", (u,e,p))
        conn.commit(); conn.close()
        return redirect('/customer/login')
    return render_template('customer_register.html')

@app.route('/customer/login', methods=['GET','POST'])
def customer_login():
    if request.method == 'POST':
        e, p = request.form['email'], request.form['password']
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT * FROM customers WHERE email=? AND password=?", (e,p))
        user = c.fetchone()
        conn.close()
        if user:
            session['customer_id'], session['customer_name'] = user[0], user[1]
            return redirect('/customer/home')
    return render_template('customer_login.html')

from pyzbar.pyzbar import decode
from PIL import Image
import base64

@app.route('/customer/home')
def customer_home():
    if 'customer_id' not in session:
        return redirect('/customer/login')
    
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM verification_logs WHERE customer_id=?", (session['customer_id'],))
    total_scans = c.fetchone()[0]
    
    c.execute("SELECT ai_result FROM verification_logs WHERE customer_id=? ORDER BY timestamp DESC LIMIT 1", (session['customer_id'],))
    last_result = c.fetchone()
    last_status = last_result[0] if last_result else "No Scans Yet"
    
    conn.close()
    
    return render_template('customer_home.html', 
                           name=session.get('customer_name', 'User'),
                           total_scans=total_scans,
                           last_status=last_status)

@app.route('/customer/verify', methods=['GET', 'POST'])
def customer_verify():
    if 'customer_id' not in session: 
        return redirect('/customer/login')

    if request.method == 'POST':
        qr_file = request.files.get('qr_image')
        if not qr_file:
            return "Error: No scan data received."

        try:
            img = Image.open(qr_file)
            decoded_objects = decode(img)
            
            if not decoded_objects:
                return "Error: QR Code is blurry or unreadable. Please try again."
            
            qr_payload = decoded_objects[0].data.decode('utf-8')
            unique_id = qr_payload.split("|")[0].strip()
            
            conn = get_db()
            c = conn.cursor()
            c.execute("""SELECT batch_id, manufacturer, expiry_date, drug_status, 
                                tablet_image, qr_image, ai_result 
                         FROM products WHERE batch_id=?""", (unique_id,))
            drug = c.fetchone()
            conn.close()

            if not drug:
                return render_template('verify_result.html', 
                                       blockchain_status=False,
                                       ai_result="Fake",
                                       suggestions=["CRITICAL: This Batch ID is not registered on the MediGuard Blockchain."])

            tablet_base64 = base64.b64encode(drug[4]).decode('utf-8') if drug[4] else None
            qr_base64 = base64.b64encode(drug[5]).decode('utf-8') if drug[5] else None

            blockchain_status = True
            db_ai_result = drug[6] # The result saved by the seller
            print(db_ai_result)
            
            is_valid = False
            if db_ai_result == "Genuine" and drug[3] == "ACTIVE":
                is_valid = True

            recommendations = []
            if db_ai_result == "Fake":
                recommendations.append("AI ALERT: Manufacturer's original scan flagged this batch as having physical anomalies.")
            
            if drug[3] != 'ACTIVE':
                recommendations.append(f"COMPLIANCE ALERT: The ledger shows this product status is '{drug[3]}'. Do not consume.")
            
            if is_valid:
                recommendations.append("SUCCESS: Digital signature and physical parameters match the master ledger.")

            
            conn = get_db()
            c = conn.cursor()
                
            c.execute("""INSERT INTO verification_logs 
                             (customer_id, qr_data, ai_result, timestamp) 
                             VALUES (?, ?, ?, CURRENT_TIMESTAMP)""", 
                          (session['customer_id'], unique_id, db_ai_result))
                
            conn.commit()
            conn.close()
            
            return render_template('verify_result.html', 
                                   ai_result=db_ai_result, 
                                   blockchain_status=blockchain_status, 
                                   drug_data=drug, 
                                   tablet_img=tablet_base64, 
                                   qr_img=qr_base64,         
                                   is_valid=is_valid,
                                   suggestions=recommendations)

        except Exception as e:
            return f"System Integrity Error: {str(e)}"

    return render_template('customer_verify.html')


@app.route('/customer/history')
def customer_history():
    if 'customer_id' not in session: return redirect('/customer/login')
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT qr_data, ai_result, blockchain_status, timestamp FROM verification_logs WHERE customer_id=? ORDER BY timestamp DESC", (session['customer_id'],))
    logs = c.fetchall(); conn.close()
    return render_template('customer_history.html', logs=logs)

@app.route('/seller/logs')
def seller_logs():
    if 'seller_id' not in session: return redirect('/seller/login')
    
    conn = get_db()
    c = conn.cursor()

    c.execute("""SELECT batch_id, created_at, blockchain_status, ai_result 
                 FROM products WHERE seller_id=? 
                 ORDER BY created_at DESC""", (session['seller_id'],))
    
    logs = c.fetchall()
    conn.close()
    
    return render_template('seller_logs.html', logs=logs)


@app.route('/seller/analytics')
def seller_analytics():
    if 'seller_id' not in session: 
        return redirect('/seller/login')
    
    conn = get_db()
    c = conn.cursor()
    sid = session['seller_id']

    c.execute("SELECT COUNT(*) FROM products WHERE seller_id=?", (sid,))
    total_batches = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM products WHERE seller_id=? AND ai_result='Fake'", (sid,))
    fake_count = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM products WHERE seller_id=? AND ai_result='Genuine'", (sid,))
    genuine_count = c.fetchone()[0]

    rate = 100.0
    if (fake_count + genuine_count) > 0:
        rate = (genuine_count / (fake_count + genuine_count)) * 100

    c.execute("SELECT id, ai_result FROM products WHERE seller_id=? AND ai_result='Fake' ORDER BY id DESC LIMIT 5", (sid,))
    threat_list = c.fetchall()
    conn.close()
    
    recommendations = []
    if rate < 100:
        recommendations.append({
            "title": "Protocol Violation Detected",
            "desc": f"Found {fake_count} anomalies. Quarantine affected batches immediately.",
            "type": "critical"
        })
    
    if rate < 80:
        recommendations.append({
            "title": "Supply Chain Breach",
            "desc": "Authenticity dropped below 80%. Resetting API credentials recommended.",
            "type": "critical"
        })
    elif rate >= 95 and total_batches > 0:
        recommendations.append({
            "title": "System Nominal",
            "desc": "Integrity levels are high. Continue standard monitoring.",
            "type": "optimal"
        })

    conn.close()
    
    return render_template('seller_analytics.html', 
                           total=total_batches, 
                           fakes=fake_count, 
                           rate=round(rate, 2),
                           threats=threat_list,
                           recs=recommendations) 

@app.route('/seller/settings')
def seller_settings():
    if 'seller_id' not in session: return redirect('/seller/login')
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT username, email, verified FROM sellers WHERE id=?", (session['seller_id'],))
    seller_info = c.fetchone()
    conn.close()
    
    return render_template('seller_settings.html', seller=seller_info)
@app.route('/product/image/<int:product_id>')
def product_image(product_id):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT tablet_image FROM products WHERE id=?", (product_id,))
    row = c.fetchone(); conn.close()
    return Response(row[0], mimetype='image/jpeg') if row else ("404", 404)

@app.route('/product/qr/<int:product_id>')
def product_qr(product_id):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT qr_image FROM products WHERE id=?", (product_id,))
    row = c.fetchone(); conn.close()
    return Response(row[0], mimetype='image/png') if row else ("404", 404)

@app.route('/customer/report/<int:log_id>')
def customer_report(log_id):
    if 'customer_id' not in session: return redirect('/customer/login')
    
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT l.qr_data, l.ai_result, l.blockchain_status, l.timestamp, p.manufacturer, p.drug_status
        FROM verification_logs l
        LEFT JOIN products p ON l.qr_data LIKE p.batch_id || '%'
        WHERE l.id = ? AND l.customer_id = ?
    """, (log_id, session['customer_id']))
    report = c.fetchone()
    conn.close()

    if not report: return "Report not found", 404

    counterfactuals = []
    if report[2] == "NOT REGISTERED":
        counterfactuals.append("The product would be Genuine if the QR ID matched a certified manufacturer entry in the ledger.")
    if report[5] == "REMOVED":
        counterfactuals.append("This product would be safe if it had not been recalled for safety violations.")

    return render_template('customer_report_detail.html', report=report, suggestions=counterfactuals)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
