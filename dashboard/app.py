import os

from flask import Flask, render_template, send_from_directory, request, jsonify
from flask_httpauth import HTTPBasicAuth
import psycopg2
import psycopg2.extras
import requests as http_requests

app = Flask(__name__)
auth = HTTPBasicAuth()

DATABASE_URL = os.environ["DATABASE_URL"]
N8N_WEBHOOK_BASE = os.environ["N8N_WEBHOOK_BASE_URL"]
PIPELINE_TOKEN = os.environ["PIPELINE_SHARED_TOKEN"]
DASH_USER = os.environ["DASHBOARD_USER"]
DASH_PASS = os.environ["DASHBOARD_PASS"]

IMAGE_CACHE_DIR = os.path.join(os.path.dirname(__file__), "image_cache")


@auth.verify_password
def verify(username, password):
    if username == DASH_USER and password == DASH_PASS:
        return username


def pipeline_token_valid():
    return request.headers.get("X-Pipeline-Token") == PIPELINE_TOKEN


def internal_or_dashboard_auth_ok():
    if pipeline_token_valid():
        return True
    creds = request.authorization
    return bool(creds and creds.username == DASH_USER and creds.password == DASH_PASS)


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


@app.route("/")
@auth.login_required
def index():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT i.id AS item_id, i.topic, i.status AS item_status,
                       v.id AS variant_id, v.attempt_no, v.copy_text,
                       v.image_path, v.image_source, v.text_source, v.decision
                FROM items i
                LEFT JOIN variants v ON v.id = i.active_variant_id
                ORDER BY
                    CASE i.status
                        WHEN 'awaiting_review' THEN 0
                        WHEN 'generating' THEN 1
                        WHEN 'partial' THEN 2
                        WHEN 'publishing' THEN 3
                        WHEN 'published' THEN 4
                        WHEN 'failed' THEN 5
                        ELSE 6
                    END,
                    i.updated_at DESC
            """)
            items = cur.fetchall()

            pub_map = {}
            if items:
                variant_ids = [r["variant_id"] for r in items if r["variant_id"]]
                if variant_ids:
                    cur.execute(
                        "SELECT * FROM publications WHERE variant_id = ANY(%s)",
                        (variant_ids,),
                    )
                    for pub in cur.fetchall():
                        pub_map.setdefault(pub["variant_id"], []).append(pub)
    finally:
        conn.close()

    return render_template("review.html", items=items, pub_map=pub_map)


@app.route("/images/<path:filename>")
def serve_image(filename):
    if not internal_or_dashboard_auth_ok():
        return jsonify({"message": "Unauthorized"}), 401
    return send_from_directory(IMAGE_CACHE_DIR, filename)


@app.route("/internal/store-image", methods=["POST"])
def store_image():
    if not pipeline_token_valid():
        return jsonify({"message": "Unauthorized"}), 401
    uploaded = request.files.get("file")
    filename = request.form.get("filename")
    if not uploaded or not filename:
        return jsonify({"message": "file and filename are required"}), 400
    safe_filename = os.path.basename(filename)
    uploaded.save(os.path.join(IMAGE_CACHE_DIR, safe_filename))
    return jsonify({"status": "ok", "filename": safe_filename})


@app.route("/api/approve", methods=["POST"])
@auth.login_required
def approve():
    data = request.get_json()
    resp = http_requests.post(
        f"{N8N_WEBHOOK_BASE}/webhook/approve",
        json={"item_id": data["item_id"], "variant_id": data["variant_id"]},
        headers={"X-Pipeline-Token": PIPELINE_TOKEN},
        timeout=120,
    )
    return jsonify(resp.json()), resp.status_code


@app.route("/api/reject", methods=["POST"])
@auth.login_required
def reject():
    data = request.get_json()
    resp = http_requests.post(
        f"{N8N_WEBHOOK_BASE}/webhook/reject",
        json={"item_id": data["item_id"], "variant_id": data["variant_id"]},
        headers={"X-Pipeline-Token": PIPELINE_TOKEN},
        timeout=120,
    )
    return jsonify(resp.json()), resp.status_code
