import os
from datetime import datetime, timedelta

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session, jsonify
)
import psycopg2
from psycopg2.extras import RealDictCursor

# ---------------- App & Config ----------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key")

# Render / local PostgreSQL URL
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. "
        "On Render, add it in the Environment tab. "
        "Locally, set it before running the app."
    )


# ---------------- Database helpers (PostgreSQL) ----------------
def get_db():
    """
    Open a new PostgreSQL connection.
    Caller must conn.close() when done.
    """
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    conn.autocommit = False  # we will commit manually
    return conn


def init_db():
    """Create tables if they do not exist (idempotent)."""
    conn = get_db()
    cur = conn.cursor()

    # rooms
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS rooms (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL
        );
        """
    )

    # active bookings
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id SERIAL PRIMARY KEY,
            user_name TEXT NOT NULL,
            room TEXT NOT NULL,
            date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            people INTEGER,
            remark TEXT,
            created_at TEXT NOT NULL
        );
        """
    )

    # archived bookings
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings_history (
            id INTEGER PRIMARY KEY,
            user_name TEXT NOT NULL,
            room TEXT NOT NULL,
            date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            people INTEGER,
            remark TEXT,
            created_at TEXT NOT NULL,
            archived_at TEXT NOT NULL
        );
        """
    )

    # seed rooms if empty
    cur.execute("SELECT COUNT(*) AS c FROM rooms;")
    if cur.fetchone()["c"] == 0:
        cur.executemany(
            "INSERT INTO rooms (name) VALUES (%s);",
            [("Room A",), ("Room B",), ("Room C",)],
        )

    conn.commit()
    cur.close()
    conn.close()


# ---------------- Helpers ----------------
def generate_time_options():
    """Full list at 30-min steps for validation (09:00..17:00 inclusive)."""
    start = datetime.strptime("09:00", "%H:%M")
    end = datetime.strptime("17:00", "%H:%M")
    step = timedelta(minutes=30)
    times = []
    cur = start
    while cur <= end:
        times.append(cur.strftime("%H:%M"))
        cur += step
    return times


ALL_TIMES = generate_time_options()
ALLOWED_SET = set(ALL_TIMES)


def get_rooms():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name FROM rooms ORDER BY name;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r["name"] for r in rows]


def is_admin():
    return session.get("is_admin", False)


@app.context_processor
def inject_globals():
    return dict(rooms=get_rooms(), times=ALL_TIMES, is_admin=is_admin())


def combine_datetime(date_str, time_str):
    return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")


def archive_past_bookings():
    """Move completed bookings to history so active lists stay clean."""
    now = datetime.now()
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM bookings;")
    rows = cur.fetchall()

    for r in rows:
        end_dt = combine_datetime(r["date"], r["end_time"])
        if end_dt < now:
            # insert into history; ignore if already archived
            cur.execute(
                """
                INSERT INTO bookings_history
                (id, user_name, room, date, start_time, end_time,
                 people, remark, created_at, archived_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING;
                """,
                (
                    r["id"],
                    r["user_name"],
                    r["room"],
                    r["date"],
                    r["start_time"],
                    r["end_time"],
                    r["people"],
                    r["remark"],
                    r["created_at"],
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            # delete from active
            cur.execute("DELETE FROM bookings WHERE id = %s;", (r["id"],))

    conn.commit()
    cur.close()
    conn.close()


# ---------- time utilities for /api/available_times ----------
def t2min(tstr):
    h, m = map(int, tstr.split(":"))
    return h * 60 + m


def min2t(mn):
    return f"{mn // 60:02d}:{mn % 60:02d}"


def generate_halfhour_slots(start="09:00", end="17:00"):
    s, e = t2min(start), t2min(end)
    return [min2t(x) for x in range(s, e, 30)]


def load_booked_intervals(room, date_str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT start_time, end_time
        FROM bookings
        WHERE room = %s AND date = %s
        ORDER BY start_time;
        """,
        (room, date_str),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [(t2min(r["start_time"]), t2min(r["end_time"])) for r in rows]


def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


# ---------------- Public Routes ----------------
@app.route("/")
def index():
    archive_past_bookings()
    selected_date = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM bookings
        WHERE date >= %s
        ORDER BY date, start_time;
        """,
        (datetime.now().strftime("%Y-%m-%d"),),
    )
    bookings = cur.fetchall()
    cur.close()
    conn.close()

    return render_template(
        "index.html", bookings=bookings, selected_date=selected_date
    )


@app.route("/book", methods=["POST"])
def book():
    user_name = (request.form.get("user_name") or "").strip()
    room = request.form.get("room")
    date = request.form.get("date")
    start_time = request.form.get("start_time")
    end_time = request.form.get("end_time")
    people = request.form.get("people")
    remark = (request.form.get("remark") or "").strip()

    if not user_name or not room or not date or not start_time or not end_time:
        flash("Please fill in all required fields.", "danger")
        return redirect(url_for("index"))
    if start_time not in ALLOWED_SET or end_time not in ALLOWED_SET:
        flash(
            "Times must be in 30-minute increments between 09:00 and 17:00.",
            "warning",
        )
        return redirect(url_for("index"))
    if end_time <= start_time:
        flash("End time must be later than start time.", "warning")
        return redirect(url_for("index"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT start_time, end_time
        FROM bookings
        WHERE date = %s AND room = %s;
        """,
        (date, room),
    )
    rows = cur.fetchall()
    for r in rows:
        if start_time < r["end_time"] and end_time > r["start_time"]:
            flash("That time slot overlaps with an existing booking.", "warning")
            cur.close()
            conn.close()
            return redirect(url_for("index"))

    cur.execute(
        """
        INSERT INTO bookings
        (user_name, room, date, start_time, end_time, people, remark, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s);
        """,
        (
            user_name,
            room,
            date,
            start_time,
            end_time,
            int(people) if people else None,
            remark,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    cur.close()
    conn.close()

    flash("Booking created successfully!", "success")
    return redirect(url_for("index"))


# ---------------- Admin Auth & Pages ----------------
ADMIN_USER = "admin"
ADMIN_PASS = "admin123"


def admin_required(f):
    from functools import wraps

    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("is_admin"):
            flash("Please log in as admin.", "warning")
            return redirect(url_for("admin_login"))
        return f(*a, **kw)

    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        if username == ADMIN_USER and password == ADMIN_PASS:
            session["is_admin"] = True
            flash("Logged in as admin.", "success")
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.", "danger")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    archive_past_bookings()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bookings ORDER BY date DESC, start_time;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return render_template("admin_dashboard.html", bookings=rows)


@app.route("/admin/history")
@admin_required
def admin_history():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bookings_history ORDER BY archived_at DESC;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return render_template("admin_history.html", bookings=rows)


@app.route("/admin/history/delete/<int:history_id>", methods=["POST"])
@admin_required
def admin_delete_history(history_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM bookings_history WHERE id = %s;", (history_id,))
    conn.commit()
    cur.close()
    conn.close()
    flash("History record deleted.", "info")
    return redirect(url_for("admin_history"))


# ----- Admin: add/edit/delete booking -----
@app.route("/admin/add", methods=["GET", "POST"])
@admin_required
def admin_add():
    if request.method == "POST":
        user_name = (request.form.get("user_name") or "").strip()
        room = request.form.get("room")
        date = request.form.get("date")
        start_time = request.form.get("start_time")
        end_time = request.form.get("end_time")
        people = request.form.get("people")
        remark = (request.form.get("remark") or "").strip()

        if not user_name or not room or not date or not start_time or not end_time:
            flash("Please fill in all required fields.", "danger")
            return redirect(url_for("admin_add"))
        if end_time <= start_time:
            flash("End time must be later than start time.", "warning")
            return redirect(url_for("admin_add"))

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT start_time, end_time
            FROM bookings
            WHERE date = %s AND room = %s;
            """,
            (date, room),
        )
        overlaps_rows = cur.fetchall()
        for r in overlaps_rows:
            if start_time < r["end_time"] and end_time > r["start_time"]:
                flash("That time slot overlaps.", "warning")
                cur.close()
                conn.close()
                return redirect(url_for("admin_add"))

        cur.execute(
            """
            INSERT INTO bookings
            (user_name, room, date, start_time, end_time, people, remark, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s);
            """,
            (
                user_name,
                room,
                date,
                start_time,
                end_time,
                int(people) if people else None,
                remark,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        cur.close()
        conn.close()
        flash("Booking added successfully.", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("admin_add.html")


@app.route("/admin/edit/<int:booking_id>", methods=["GET", "POST"])
@admin_required
def admin_edit(booking_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bookings WHERE id = %s;", (booking_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        flash("Booking not found.", "danger")
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        user_name = (request.form.get("user_name") or "").strip()
        room = request.form.get("room")
        date = request.form.get("date")
        start_time = request.form.get("start_time")
        end_time = request.form.get("end_time")
        people = request.form.get("people")
        remark = (request.form.get("remark") or "").strip()

        if end_time <= start_time:
            flash("End time must be later than start time.", "warning")
            return redirect(url_for("admin_edit", booking_id=booking_id))

        cur.execute(
            """
            SELECT start_time, end_time
            FROM bookings
            WHERE date = %s AND room = %s AND id <> %s;
            """,
            (date, room, booking_id),
        )
        overlaps_rows = cur.fetchall()
        for r in overlaps_rows:
            if start_time < r["end_time"] and end_time > r["start_time"]:
                flash("That time slot overlaps.", "warning")
                return redirect(url_for("admin_edit", booking_id=booking_id))

        cur.execute(
            """
            UPDATE bookings
            SET user_name = %s, room = %s, date = %s,
                start_time = %s, end_time = %s,
                people = %s, remark = %s
            WHERE id = %s;
            """,
            (
                user_name,
                room,
                date,
                start_time,
                end_time,
                int(people) if people else None,
                remark,
                booking_id,
            ),
        )
        conn.commit()
        cur.close()
        conn.close()
        flash("Booking updated.", "success")
        return redirect(url_for("admin_dashboard"))

    cur.close()
    conn.close()
    return render_template("admin_edit.html", booking=row)


@app.route("/admin/delete/<int:booking_id>", methods=["POST"])
@admin_required
def admin_delete(booking_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM bookings WHERE id = %s;", (booking_id,))
    conn.commit()
    cur.close()
    conn.close()
    flash("Booking deleted.", "info")
    return redirect(url_for("admin_dashboard"))


# ----- Room Management -----
@app.route("/admin/rooms")
@admin_required
def admin_rooms():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rooms ORDER BY id;")
    rooms = cur.fetchall()
    cur.close()
    conn.close()
    return render_template("admin_rooms.html", rooms=rooms)


@app.route("/admin/rooms/add", methods=["POST"])
@admin_required
def admin_add_room():
    name = (request.form.get("name") or "").strip()
    if name:
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO rooms (name) VALUES (%s);", (name,))
            conn.commit()
            flash("Room added.", "success")
        except Exception:
            conn.rollback()
            flash("Room already exists.", "danger")
        cur.close()
        conn.close()
    return redirect(url_for("admin_rooms"))


@app.route("/admin/rooms/delete/<int:room_id>", methods=["POST"])
@admin_required
def admin_delete_room(room_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM rooms WHERE id = %s;", (room_id,))
    conn.commit()
    cur.close()
    conn.close()
    flash("Room deleted.", "info")
    return redirect(url_for("admin_rooms"))


@app.route("/admin/rooms/edit/<int:room_id>", methods=["POST"])
@admin_required
def admin_edit_room(room_id):
    new_name = (request.form.get("name") or "").strip()
    if new_name:
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE rooms SET name = %s WHERE id = %s;", (new_name, room_id)
            )
            conn.commit()
            flash("Room updated.", "success")
        except Exception:
            conn.rollback()
            flash("Room name already exists.", "danger")
        cur.close()
        conn.close()
    return redirect(url_for("admin_rooms"))


# ---------------- JSON APIs ----------------
@app.route("/api/admin/alerts")
def api_admin_alerts():
    """Return meetings starting/ending within 15 minutes for admin toast."""
    if not session.get("is_admin"):
        resp = jsonify({"ok": False, "alerts": []})
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp, 200

    now = datetime.now()
    horizon = now + timedelta(minutes=15)
    today_local = now.strftime("%Y-%m-%d")

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, date, start_time, end_time, room, user_name
        FROM bookings
        WHERE date = %s
        ORDER BY start_time;
        """,
        (today_local,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    alerts = []

    def _combine_dt(d, t):
        try:
            return datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M")
        except Exception:
            return None

    for r in rows:
        start_dt = _combine_dt(r["date"], r["start_time"])
        end_dt = _combine_dt(r["date"], r["end_time"])
        if start_dt and now <= start_dt <= horizon:
            alerts.append(
                {
                    "id": f"start-{r['id']}",
                    "type": "start",
                    "when": r["start_time"],
                    "room": r["room"],
                    "user": r["user_name"],
                    "msg": f"Meeting in {r['room']} starts at {r['start_time']} "
                    f"(Booked by {r['user_name']}).",
                }
            )
        if end_dt and now <= end_dt <= horizon:
            alerts.append(
                {
                    "id": f"end-{r['id']}",
                    "type": "end",
                    "when": r["end_time"],
                    "room": r["room"],
                    "user": r["user_name"],
                    "msg": f"Meeting in {r['room']} ends at {r['end_time']} "
                    f"(Booked by {r['user_name']}).",
                }
            )

    resp = jsonify({"ok": True, "alerts": alerts})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp, 200


@app.route("/api/available_times")
def api_available_times():
    """
    Returns available start slots (30-min) for a room+date,
    and valid end slots after a chosen start. Booked starts are excluded.
    """
    room = request.args.get("room")
    date_str = request.args.get("date")
    start_sel = request.args.get("start")

    if not room or not date_str:
        return jsonify({"ok": False, "error": "room and date are required"}), 400

    booked = load_booked_intervals(room, date_str)

    day_start, day_end = "09:00", "17:00"
    starts_all = generate_halfhour_slots(day_start, day_end)

    starts_available = []
    for s in starts_all:
        s_m = t2min(s)
        e_m = s_m + 30
        if any(overlaps(s_m, e_m, b0, b1) for (b0, b1) in booked):
            continue
        starts_available.append(s)

    ends_available = []
    if start_sel:
        s_m = t2min(start_sel)
        ends_all = [min2t(x) for x in range(s_m + 30, t2min(day_end) + 1, 30)]
        block_after = t2min(day_end)
        for (b0, b1) in booked:
            if b0 >= s_m and b0 < block_after:
                block_after = b0
        for e in ends_all:
            e_m = t2min(e)
            if e_m > block_after:
                break
            if any(overlaps(s_m, e_m, b0, b1) for (b0, b1) in booked):
                break
            ends_available.append(e)

    return jsonify({"ok": True, "starts": starts_available, "ends": ends_available})


# ---------------- Main ----------------
if __name__ == "__main__":
    # init_db already safe to call; tables will be created if missing
    init_db()
    app.run(debug=True)
