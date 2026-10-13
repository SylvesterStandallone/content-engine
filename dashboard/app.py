import calendar as calendar_mod
import os
import subprocess
from datetime import date, datetime, timedelta

from flask import Flask, render_template, send_from_directory, request, jsonify, redirect, url_for
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

# Holds both the generated PNGs and the mp4s rendered from them.
IMAGE_CACHE_DIR = os.path.join(os.path.dirname(__file__), "image_cache")

# Video render settings. TikTok wants 9:16; generated images are square, so the
# frame is a blurred cover of the source with the sharp square centred on it —
# that keeps the whole image visible instead of cropping ~44% away.
VIDEO_W, VIDEO_H = 1080, 1920
VIDEO_SECONDS = 6
VIDEO_FPS = 30
VIDEO_ZOOM_MAX = 1.25
# zoompan quantises its pan offsets to whole input pixels, so panning a
# 1080-wide source visibly steps. Supersampling first and letting s= scale back
# down is the standard fix; it is also most of the render cost, so it is the
# first dial to turn if renders are too slow.
VIDEO_SUPERSAMPLE = 2
# Must stay under the 120s n8n node timeout so a stuck render reports a real
# error instead of the caller giving up first.
FFMPEG_TIMEOUT_SECONDS = 100

MONTH_NAMES = (
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
)
WEEKDAY_NAMES = ("L", "M", "X", "J", "V", "S", "D")


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


# One definition of "a piece of content": the item plus whichever variant is
# currently active, classified by media kind and anchored to a single date.
# anchor_at is when it went out (first publication) or, if it never did, when it
# was created — so everything lands somewhere on the calendar.
CONTENT_SQL = """
    SELECT i.id AS item_id, i.topic, i.status AS item_status, i.created_at,
           i.updated_at,
           v.id AS variant_id, v.attempt_no, v.copy_text,
           v.image_path, v.video_path, v.image_source, v.text_source, v.decision,
           CASE WHEN v.video_path IS NOT NULL THEN 'video' ELSE 'image' END
               AS media_kind,
           COALESCE(
               (SELECT min(p.created_at) FROM publications p
                 WHERE p.variant_id = v.id),
               i.created_at
           ) AS anchor_at
      FROM items i
      LEFT JOIN variants v ON v.id = i.active_variant_id
"""

MEDIA_KINDS = ("image", "video")


def _fetch_publications(cur, rows):
    """Map variant_id -> its publication rows, for the cards to render."""
    variant_ids = [r["variant_id"] for r in rows if r["variant_id"]]
    pub_map = {}
    if variant_ids:
        cur.execute(
            "SELECT * FROM publications WHERE variant_id = ANY(%s) ORDER BY network",
            (variant_ids,),
        )
        for pub in cur.fetchall():
            pub_map.setdefault(pub["variant_id"], []).append(pub)
    return pub_map


@app.context_processor
def inject_pending_count():
    """Badge on the Cola nav link. Cheap enough for a single-user tool."""
    try:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM items WHERE status = 'awaiting_review'"
                )
                return {"pending_review": cur.fetchone()[0]}
        finally:
            conn.close()
    except Exception:
        return {"pending_review": 0}


def _requested_kind():
    kind = (request.args.get("kind") or "").lower()
    return kind if kind in MEDIA_KINDS else None


@app.route("/")
@auth.login_required
def index():
    today = date.today()
    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
        first = date(year, month, 1)
    except ValueError:
        return redirect(url_for("index"))

    kind = _requested_kind()
    # Month bounds, plus the leading/trailing days the grid shows from
    # neighbouring months so their counts aren't silently blank.
    weeks = calendar_mod.Calendar(firstweekday=0).monthdatescalendar(year, month)
    span_start, span_end = weeks[0][0], weeks[-1][-1] + timedelta(days=1)

    counts = {}
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sql = f"""
                SELECT anchor_at::date AS day, media_kind, count(*) AS n
                  FROM ({CONTENT_SQL}) c
                 WHERE anchor_at >= %s AND anchor_at < %s
                 GROUP BY 1, 2
            """
            cur.execute(sql, (span_start, span_end))
            for row in cur.fetchall():
                bucket = counts.setdefault(row["day"], {"image": 0, "video": 0})
                bucket[row["media_kind"]] = row["n"]

            cur.execute(
                f"SELECT count(*) AS n FROM ({CONTENT_SQL}) c"
                " WHERE item_status = 'awaiting_review'"
            )
            pending_review = cur.fetchone()["n"]
    finally:
        conn.close()

    prev_month = first - timedelta(days=1)
    next_month = (first + timedelta(days=32)).replace(day=1)

    return render_template(
        "calendar.html",
        weeks=weeks,
        counts=counts,
        weekday_names=WEEKDAY_NAMES,
        month_label=f"{MONTH_NAMES[month - 1]} {year}",
        this_month=first,
        prev_month=prev_month,
        next_month=next_month,
        today=today,
        kind=kind,
        pending_review=pending_review,
    )


@app.route("/day/<day>")
@auth.login_required
def day_view(day):
    try:
        the_day = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return redirect(url_for("index"))

    kind = _requested_kind()
    params = [the_day]
    where = "WHERE anchor_at::date = %s"
    if kind:
        where += " AND media_kind = %s"
        params.append(kind)

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM ({CONTENT_SQL}) c {where} ORDER BY anchor_at DESC",
                params,
            )
            items = cur.fetchall()
            pub_map = _fetch_publications(cur, items)
    finally:
        conn.close()

    return render_template(
        "day.html",
        items=items,
        pub_map=pub_map,
        the_day=the_day,
        kind=kind,
        prev_day=the_day - timedelta(days=1),
        next_day=the_day + timedelta(days=1),
        day_label=f"{the_day.day} de {MONTH_NAMES[the_day.month - 1].lower()} de {the_day.year}",
    )


@app.route("/queue")
@auth.login_required
def queue():
    kind = _requested_kind()
    where = ""
    params = []
    if kind:
        where = "WHERE media_kind = %s"
        params.append(kind)

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT * FROM ({CONTENT_SQL}) c
                {where}
                ORDER BY
                    CASE item_status
                        WHEN 'awaiting_review' THEN 0
                        WHEN 'generating' THEN 1
                        WHEN 'partial' THEN 2
                        WHEN 'publishing' THEN 3
                        WHEN 'published' THEN 4
                        WHEN 'failed' THEN 5
                        ELSE 6
                    END,
                    updated_at DESC
                """,
                params,
            )
            items = cur.fetchall()
            pub_map = _fetch_publications(cur, items)
    finally:
        conn.close()

    return render_template("review.html", items=items, pub_map=pub_map, kind=kind)


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


def _build_ffmpeg_command(src_path, out_path):
    frames = VIDEO_SECONDS * VIDEO_FPS
    zoom_step = (VIDEO_ZOOM_MAX - 1) / frames
    ss_w, ss_h = VIDEO_W * VIDEO_SUPERSAMPLE, VIDEO_H * VIDEO_SUPERSAMPLE
    filter_complex = (
        f"[0:v]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_W}:{VIDEO_H},boxblur=40:2[bg];"
        f"[0:v]scale={VIDEO_W}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,scale={ss_w}:{ss_h},"
        f"zoompan=z='min(zoom+{zoom_step:.6f},{VIDEO_ZOOM_MAX})':d={frames}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={VIDEO_W}x{VIDEO_H}:fps={VIDEO_FPS},setsar=1[v]"
    )
    return [
        "ffmpeg", "-y",
        "-loop", "1", "-i", src_path,
        # Silent track: audioless files are a common source of ingest rejections.
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
        "-r", str(VIDEO_FPS), "-t", str(VIDEO_SECONDS),
        "-c:a", "aac", "-shortest", "-movflags", "+faststart",
        out_path,
    ]


@app.route("/internal/render-video", methods=["POST"])
def render_video():
    if not pipeline_token_valid():
        return jsonify({"message": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    image_filename = data.get("image_filename") or request.form.get("image_filename")
    if not image_filename:
        return jsonify({"message": "image_filename is required"}), 400

    src_name = os.path.basename(image_filename)
    src_path = os.path.join(IMAGE_CACHE_DIR, src_name)
    if not os.path.isfile(src_path):
        return jsonify({"message": f"no such image: {src_name}"}), 404

    out_name = os.path.splitext(src_name)[0] + ".mp4"
    out_path = os.path.join(IMAGE_CACHE_DIR, out_name)

    # Idempotent: retry-network re-approves the same variant and should not pay
    # for the render twice.
    if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        return jsonify({"video_filename": out_name, "cached": True})

    # Render to a temp file so a crash can't leave a truncated mp4 that the
    # cache check above would then treat as valid.
    tmp_path = os.path.join(IMAGE_CACHE_DIR, f".{out_name}.{os.getpid()}.partial.mp4")
    try:
        result = subprocess.run(
            _build_ffmpeg_command(src_path, tmp_path),
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", "replace")[-800:]
            return jsonify({"message": "ffmpeg failed", "stderr": stderr_tail}), 500
        os.replace(tmp_path, out_path)
    except subprocess.TimeoutExpired:
        return jsonify({"message": f"render exceeded {FFMPEG_TIMEOUT_SECONDS}s"}), 504
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return jsonify({"video_filename": out_name, "cached": False})


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
