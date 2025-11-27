import os
import sqlite3
import psycopg2
from psycopg2.extras import execute_values

# 1) SQLite source (your old file)
SQLITE_PATH = "booking.db"

# 2) PostgreSQL target (Render)
# Make sure DATABASE_URL is already set in your environment
PG_URL = os.environ.get("DATABASE_URL")

if not PG_URL:
    raise SystemExit("DATABASE_URL is not set. Please set it first.")

# ---------- Connect to both databases ----------
sqlite_conn = sqlite3.connect(SQLITE_PATH)
sqlite_conn.row_factory = sqlite3.Row
sqlite_cur = sqlite_conn.cursor()

pg_conn = psycopg2.connect(PG_URL)
pg_conn.autocommit = True
pg_cur = pg_conn.cursor()

print("Connected to SQLite and PostgreSQL.")

# ---------- Create tables on PostgreSQL (if not exist) ----------
print("Ensuring tables exist on PostgreSQL...")

pg_cur.execute("""
CREATE TABLE IF NOT EXISTS rooms (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);
""")

pg_cur.execute("""
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
""")

pg_cur.execute("""
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

# ---------- Copy rooms ----------
print("Migrating rooms...")
sqlite_cur.execute("SELECT * FROM rooms")
rooms = sqlite_cur.fetchall()
for r in rooms:
    try:
        pg_cur.execute(
            "INSERT INTO rooms (id, name) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
            (r["id"], r["name"])
        )
    except Exception as e:
        print("Room insert error:", e)

# ---------- Copy bookings ----------
print("Migrating active bookings...")
sqlite_cur.execute("SELECT * FROM bookings")
bookings = sqlite_cur.fetchall()
if bookings:
    values = [
        (
            b["id"], b["user_name"], b["room"], b["date"],
            b["start_time"], b["end_time"], b["people"],
            b["remark"], b["created_at"]
        )
        for b in bookings
    ]
    execute_values(pg_cur, """
        INSERT INTO bookings (id, user_name, room, date, start_time, end_time, people, remark, created_at)
        VALUES %s
        ON CONFLICT (id) DO NOTHING
    """, values)

# ---------- Copy bookings_history ----------
print("Migrating history...")
sqlite_cur.execute("SELECT * FROM bookings_history")
history = sqlite_cur.fetchall()
if history:
    values = [
        (
            h["id"], h["user_name"], h["room"], h["date"],
            h["start_time"], h["end_time"], h["people"],
            h["remark"], h["created_at"], h["archived_at"]
        )
        for h in history
    ]
    execute_values(pg_cur, """
        INSERT INTO bookings_history
        (id, user_name, room, date, start_time, end_time, people, remark, created_at, archived_at)
        VALUES %s
        ON CONFLICT (id) DO NOTHING
    """, values)

print("Migration finished.")

sqlite_cur.close()
sqlite_conn.close()
pg_cur.close()
pg_conn.close()
print("All connections closed.")
