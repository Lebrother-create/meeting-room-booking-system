from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
import sqlite3
from datetime import datetime, timedelta
import os

# ---------------- App & Config ----------------
app = Flask(__name__)

# Secret key: use env var in production, fallback for local
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')

# SQLite path (works locally + on Render)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'booking.db')


# ---------------- Database ----------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # bookings (active)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_name TEXT NOT NULL,
            room TEXT NOT NULL,
            date TEXT NOT NULL,           -- YYYY-MM-DD
            start_time TEXT NOT NULL,     -- HH:MM
            end_time TEXT NOT NULL,       -- HH:MM
            people INTEGER,
            remark TEXT,
            created_at TEXT NOT NULL
        );
    """)

    # rooms
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );
    """)

    # seed rooms once
    if cur.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0:
        cur.executemany("INSERT INTO rooms (name) VALUES (?)",
                        [('Room A',), ('Room B',), ('Room C',)])

    # bookings history (auto-archived past items)
    cur.execute("""
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
    """)

    conn.commit()
    conn.close()


# run init_db automatically on first request (for Render)
@app.before_first_request
def _init_on_first_request():
    init_db()


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
ALLOWED_SET = set(ALL_TIMES)  # for server-side validation


def get_rooms():
    conn = get_db()
    rows = conn.execute("SELECT name FROM rooms ORDER BY name").fetchall()
    conn.close()
    return [r['name'] for r in rows]


def is_admin():
    return session.get('is_admin', False)


@app.context_processor
def inject_globals():
    # Expose common variables to all templates
    return dict(rooms=get_rooms(), times=ALL_TIMES, is_admin=is_admin())


def combine_datetime(date_str, time_str):
    return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")


def archive_past_bookings():
    """Move completed bookings to history so active lists stay clean."""
    now = datetime.now()
    conn = get_db()
    cur = conn.cursor()
    for r in cur.execute("SELECT * FROM bookings").fetchall():
        end_dt = combine_datetime(r['date'], r['end_time'])
        if end_dt < now:
            cur.execute("""
                INSERT OR REPLACE INTO bookings_history
                (id, user_name, room, date, start_time, end_time, people, remark, created_at, archived_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                r['id'], r['user_name'], r['room'], r['date'], r['start_time'], r['end_time'],
                r['people'], r['remark'], r['created_at'],
                datetime.now().isoformat(timespec="seconds")
            ))
            cur.execute("DELETE FROM bookings WHERE id=?", (r['id'],))
    conn.commit()
    conn.close()


# ---------------- Time utilities for availability API ----------------
def t2min(tstr):  # "HH:MM" -> minutes
    h, m = map(int, tstr.split(":"))
    return h * 60 + m


def min2t(mn):   # minutes -> "HH:MM"
    return f"{mn // 60:02d}:{mn % 60:02d}"


def generate_halfhour_slots(start="09:00", end="17:00"):
    s, e = t2min(start), t2min(end)
    return [min2t(x) for x in range(s, e, 30)]  # returns 09:00..16:30 start slots


def load_booked_intervals(db, room, date_str):
    cur = db.execute("""
        SELECT start_time, end_time
        FROM bookings
        WHERE room = ? AND date = ?
        ORDER BY start_time
    """, (room, date_str))
    rows = cur.fetchall()
    cur.close()
    return [(t2min(r["start_time"]), t2min(r["end_time"])) for r in rows]


def overlaps(a_start, a_end, b_start, b_end):
    # half-open intervals [start, end)
    return a_start < b_end and b_start < a_end


# ---------------- Public Routes ----------------
@app.route('/')
def index():
    archive_past_bookings()
    selected_date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    conn = get_db()
    bookings = conn.execute(
        "SELECT * FROM bookings WHERE date >= ? ORDER BY date, start_time",
        (datetime.now().strftime("%Y-%m-%d"),)
    ).fetchall()
    conn.close()
    return render_template('index.html', bookings=bookings, selected_date=selected_date)


@app.route('/book', methods=['POST'])
def book():
    user_name = (request.form.get('user_name') or '').strip()
    room = request.form.get('room')
    date = request.form.get('date')
    start_time = request.form.get('start_time')
    end_time = request.form.get('end_time')
    people = request.form.get('people')
    remark = (request.form.get('remark') or '').strip()

    # Basic validations
    if not user_name or not room or not date or not start_time or not end_time:
        flash('Please fill in all required fields.', 'danger')
        return redirect(url_for('index'))
    if start_time not in ALLOWED_SET or end_time not in ALLOWED_SET:
        flash('Times must be in 30-minute increments between 09:00 and 17:00.', 'warning')
        return redirect(url_for('index'))
    if end_time <= start_time:
        flash('End time must be later than start time.', 'warning')
        return redirect(url_for('index'))

    # Overlap check (same room + date)
    conn = get_db()
    rows = conn.execute(
        "SELECT start_time, end_time FROM bookings WHERE date=? AND room=?",
        (date, room)
    ).fetchall()
    for r in rows:
        if start_time < r['end_time'] and end_time > r['start_time']:
            flash('That time slot overlaps with an existing booking.', 'warning')
            conn.close()
            return redirect(url_for('index'))

    conn.execute("""
        INSERT INTO bookings (user_name, room, date, start_time, end_time, people, remark, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_name, room, date, start_time, end_time,
        int(people) if people else None, remark,
        datetime.now().isoformat(timespec="seconds")
    ))
    conn.commit()
    conn.close()
    flash('Booking created successfully!', 'success')
    return redirect(url_for('index'))


# ---------------- Admin Auth & Pages ----------------
ADMIN_USER = 'admin'
ADMIN_PASS = 'admin123'


def admin_required(f):
    from functools import wraps

    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get('is_admin'):
            flash('Please log in as admin.', 'warning')
            return redirect(url_for('admin_login'))
        return f(*a, **kw)

    return wrapper


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = (request.form.get('password') or '').strip()
        if username == ADMIN_USER and password == ADMIN_PASS:
            session['is_admin'] = True
            flash('Logged in as admin.', 'success')
            return redirect(url_for('admin_dashboard'))
        flash('Invalid credentials.', 'danger')
    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('index'))


@app.route('/admin')
@admin_required
def admin_dashboard():
    archive_past_bookings()
    conn = get_db()
    rows = conn.execute("SELECT * FROM bookings ORDER BY date DESC, start_time").fetchall()
    conn.close()
    return render_template('admin_dashboard.html', bookings=rows)


@app.route('/admin/history')
@admin_required
def admin_history():
    conn = get_db()
    rows = conn.execute("SELECT * FROM bookings_history ORDER BY archived_at DESC").fetchall()
    conn.close()
    return render_template('admin_history.html', bookings=rows)


# ----- delete a record from history -----
@app.route('/admin/history/delete/<int:history_id>', methods=['POST'])
@admin_required
def admin_delete_history(history_id):
    conn = get_db()
    conn.execute("DELETE FROM bookings_history WHERE id = ?", (history_id,))
    conn.commit()
    conn.close()
    flash('History record deleted.', 'info')
    return redirect(url_for('admin_history'))


# ----- Admin: add/edit/delete booking -----
@app.route('/admin/add', methods=['GET', 'POST'])
@admin_required
def admin_add():
    if request.method == 'POST':
        user_name = (request.form.get('user_name') or '').strip()
        room = request.form.get('room')
        date = request.form.get('date')
        start_time = request.form.get('start_time')
        end_time = request.form.get('end_time')
        people = request.form.get('people')
        remark = (request.form.get('remark') or '').strip()

        if not user_name or not room or not date or not start_time or not end_time:
            flash('Please fill in all required fields.', 'danger')
            return redirect(url_for('admin_add'))
        if end_time <= start_time:
            flash('End time must be later than start time.', 'warning')
            return redirect(url_for('admin_add'))

        conn = get_db()
        overlaps_rows = conn.execute(
            "SELECT start_time, end_time FROM bookings WHERE date=? AND room=?",
            (date, room)
        ).fetchall()
        for r in overlaps_rows:
            if start_time < r['end_time'] and end_time > r['start_time']:
                flash('That time slot overlaps.', 'warning')
                conn.close()
                return redirect(url_for('admin_add'))

        conn.execute("""
            INSERT INTO bookings (user_name, room, date, start_time, end_time, people, remark, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_name, room, date, start_time, end_time,
            int(people) if people else None, remark,
            datetime.now().isoformat(timespec="seconds")
        ))
        conn.commit()
        conn.close()
        flash('Booking added successfully.', 'success')
        return redirect(url_for('admin_dashboard'))

    return render_template('admin_add.html')


@app.route('/admin/edit/<int:booking_id>', methods=['GET', 'POST'])
@admin_required
def admin_edit(booking_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not row:
        conn.close()
        flash('Booking not found.', 'danger')
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        user_name = (request.form.get('user_name') or '').strip()
        room = request.form.get('room')
        date = request.form.get('date')
        start_time = request.form.get('start_time')
        end_time = request.form.get('end_time')
        people = request.form.get('people')
        remark = (request.form.get('remark') or '').strip()

        if end_time <= start_time:
            flash('End time must be later than start time.', 'warning')
            return redirect(url_for('admin_edit', booking_id=booking_id))

        overlaps_rows = conn.execute(
            "SELECT start_time, end_time FROM bookings WHERE date=? AND room=? AND id<>?",
            (date, room, booking_id)
        ).fetchall()
        for r in overlaps_rows:
            if start_time < r['end_time'] and end_time > r['start_time']:
                flash('That time slot overlaps.', 'warning')
                return redirect(url_for('admin_edit', booking_id=booking_id))

        conn.execute("""
            UPDATE bookings
            SET user_name=?, room=?, date=?, start_time=?, end_time=?, people=?, remark=?
            WHERE id=?
        """, (
            user_name, room, date, start_time, end_time,
            int(people) if people else None, remark, booking_id
        ))
        conn.commit()
        conn.close()
        flash('Booking updated.', 'success')
        return redirect(url_for('admin_dashboard'))

    conn.close()
    return render_template('admin_edit.html', booking=row)


@app.route('/admin/delete/<int:booking_id>', methods=['POST'])
@admin_required
def admin_delete(booking_id):
    conn = get_db()
    conn.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()
    flash('Booking deleted.', 'info')
    return redirect(url_for('admin_dashboard'))


# ----- Room Management -----
@app.route('/admin/rooms')
@admin_required
def admin_rooms():
    conn = get_db()
    rooms = conn.execute("SELECT * FROM rooms ORDER BY id").fetchall()
    conn.close()
    return render_template('admin_rooms.html', rooms=rooms)


@app.route('/admin/rooms/add', methods=['POST'])
@admin_required
def admin_add_room():
    name = (request.form.get('name') or '').strip()
    if name:
        conn = get_db()
        try:
            conn.execute("INSERT INTO rooms (name) VALUES (?)", (name,))
            conn.commit()
            flash('Room added.', 'success')
        except sqlite3.IntegrityError:
            flash('Room already exists.', 'danger')
        conn.close()
    return redirect(url_for('admin_rooms'))


@app.route('/admin/rooms/delete/<int:room_id>', methods=['POST'])
@admin_required
def admin_delete_room(room_id):
    conn = get_db()
    conn.execute("DELETE FROM rooms WHERE id=?", (room_id,))
    conn.commit()
    conn.close()
    flash('Room deleted.', 'info')
    return redirect(url_for('admin_rooms'))


@app.route('/admin/rooms/edit/<int:room_id>', methods=['POST'])
@admin_required
def admin_edit_room(room_id):
    new_name = (request.form.get('name') or '').strip()
    if new_name:
        conn = get_db()
        try:
            conn.execute("UPDATE rooms SET name=? WHERE id=?", (new_name, room_id))
            conn.commit()
            flash('Room updated.', 'success')
        except sqlite3.IntegrityError:
            flash('Room name already exists.', 'danger')
        conn.close()
    return redirect(url_for('admin_rooms'))


# ---------------- JSON APIs (used by UI scripts) ----------------
@app.route("/api/admin/alerts")
def api_admin_alerts():
    """Return meetings starting/ending within 15 minutes for admin toast."""
    if not session.get("is_admin"):
        # still return ok False so client stops
        resp = jsonify({"ok": False, "alerts": []})
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
        return resp, 200

    now = datetime.now()
    horizon = now + timedelta(minutes=15)
    today_local = now.strftime("%Y-%m-%d")  # use LOCAL date

    cur = get_db().execute("""
        SELECT id, date, start_time, end_time, room, user_name
        FROM bookings
        WHERE date = ?
        ORDER BY start_time
    """, (today_local,))
    rows = cur.fetchall()
    cur.close()

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
            alerts.append({
                "id": f"start-{r['id']}",
                "type": "start",
                "when": r["start_time"],
                "room": r["room"],
                "user": r["user_name"],
                "msg": f"Meeting in {r['room']} starts at {r['start_time']} (Booked by {r['user_name']})."
            })
        if end_dt and now <= end_dt <= horizon:
            alerts.append({
                "id": f"end-{r['id']}",
                "type": "end",
                "when": r["end_time"],
                "room": r["room"],
                "user": r["user_name"],
                "msg": f"Meeting in {r['room']} ends at {r['end_time']} (Booked by {r['user_name']})."
            })

    resp = jsonify({"ok": True, "alerts": alerts})
    # no caching so polling always sees fresh data
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp, 200


@app.route("/api/available_times")
def api_available_times():
    """
    Returns available start slots (30-min) for a room+date,
    and valid end slots after a chosen start. Removes booked starts.
    """
    room = request.args.get("room")
    date_str = request.args.get("date")
    start_sel = request.args.get("start")  # optional: compute valid ENDs

    if not room or not date_str:
        return jsonify({"ok": False, "error": "room and date are required"}), 400

    db = get_db()
    booked = load_booked_intervals(db, room, date_str)

    # Base windows 09:00..17:00
    day_start, day_end = "09:00", "17:00"
    starts_all = generate_halfhour_slots(day_start, day_end)  # 09:00..16:30

    # Filter START options: remove any start whose 30-min block overlaps a booking
    starts_available = []
    for s in starts_all:
        s_m = t2min(s)
        e_m = s_m + 30
        if any(overlaps(s_m, e_m, b0, b1) for (b0, b1) in booked):
            continue
        starts_available.append(s)

    # END options after selecting a start: stop before the next booking
    ends_available = []
    if start_sel:
        s_m = t2min(start_sel)
        ends_all = [min2t(x) for x in range(s_m + 30, t2min(day_end) + 1, 30)]
        # next blocking booking start after selected start
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
if __name__ == '__main__':
    init_db()
    app.run(debug=True)
