import os
import logging
from psycopg2.pool import SimpleConnectionPool
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is required")

db_pool = SimpleConnectionPool(minconn=1, maxconn=10, dsn=DATABASE_URL)


def get_conn():
    conn = db_pool.getconn()
    try:
        conn.cursor().execute("SELECT 1")
    except Exception:
        db_pool.putconn(conn, close=True)
        conn = db_pool.getconn()
    return conn


def release(conn):
    if conn:
        db_pool.putconn(conn)


def init_db():
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id        BIGINT PRIMARY KEY,
                username       TEXT,
                status         TEXT DEFAULT 'idle',
                partner_id     BIGINT DEFAULT NULL,
                total_chats    INTEGER DEFAULT 0,
                gender         TEXT DEFAULT NULL,
                interests      TEXT[] DEFAULT '{}',
                gender_filter  TEXT DEFAULT NULL,
                is_premium     BOOLEAN DEFAULT FALSE,
                premium_until  TIMESTAMPTZ DEFAULT NULL,
                is_invisible   BOOLEAN DEFAULT FALSE,
                warnings       INTEGER DEFAULT 0,
                is_banned      BOOLEAN DEFAULT FALSE,
                ban_until      TIMESTAMPTZ DEFAULT NULL,
                created_at     TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                user_id        BIGINT PRIMARY KEY,
                joined_at      TIMESTAMPTZ DEFAULT NOW(),
                is_premium     BOOLEAN DEFAULT FALSE,
                gender         TEXT DEFAULT NULL,
                gender_filter  TEXT DEFAULT NULL,
                interests      TEXT[] DEFAULT '{}'
            );
        """)
        # Pastikan kolom baru ada untuk database lama
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_until TIMESTAMPTZ DEFAULT NULL;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS warnings INTEGER DEFAULT 0;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_until TIMESTAMPTZ DEFAULT NULL;")
        conn.commit()
        logger.info("DB initialized")
    except Exception as e:
        logger.error(f"init_db error: {e}")
    finally:
        release(conn)


# ── User ──────────────────────────────────────────────

def upsert_user(user_id: int, username: str):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (user_id, username)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
        """, (user_id, username or "anon"))
        conn.commit()
    except Exception as e:
        logger.error(f"upsert_user: {e}")
    finally:
        release(conn)


def get_user(user_id: int):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"get_user: {e}")
        return None
    finally:
        release(conn)


def set_status(user_id: int, status: str, partner_id=None):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET status = %s, partner_id = %s WHERE user_id = %s",
            (status, partner_id, user_id)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"set_status: {e}")
    finally:
        release(conn)


def update_profile(user_id: int, gender: str = None, interests: list = None):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        if gender is not None:
            cur.execute("UPDATE users SET gender = %s WHERE user_id = %s", (gender, user_id))
        if interests is not None:
            cur.execute("UPDATE users SET interests = %s WHERE user_id = %s", (interests, user_id))
        conn.commit()
    except Exception as e:
        logger.error(f"update_profile: {e}")
    finally:
        release(conn)


def update_filters(user_id: int, gender_filter: str = "reset"):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        val = None if gender_filter == "reset" else gender_filter
        cur.execute("UPDATE users SET gender_filter = %s WHERE user_id = %s", (val, user_id))
        conn.commit()
    except Exception as e:
        logger.error(f"update_filters: {e}")
    finally:
        release(conn)


def set_invisible(user_id: int, invisible: bool):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE users SET is_invisible = %s WHERE user_id = %s", (invisible, user_id))
        conn.commit()
    except Exception as e:
        logger.error(f"set_invisible: {e}")
    finally:
        release(conn)


def set_premium(user_id: int, active: bool, days: int = 30):
    """
    Set premium user.
    active=True  → aktifkan premium selama `days` hari (default 30)
    active=False → cabut premium
    """
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        if active:
            from datetime import datetime, timedelta, timezone
            until = datetime.now(timezone.utc) + timedelta(days=days)
            cur.execute(
                "UPDATE users SET is_premium = TRUE, premium_until = %s WHERE user_id = %s",
                (until, user_id)
            )
        else:
            cur.execute(
                "UPDATE users SET is_premium = FALSE, premium_until = NULL WHERE user_id = %s",
                (user_id,)
            )
        conn.commit()
    except Exception as e:
        logger.error(f"set_premium: {e}")
    finally:
        release(conn)


def check_premium_expiry() -> list:
    """
    Cabut premium yang sudah expired.
    Dipanggil otomatis setiap hari via job_queue.
    Return: list user_id yang premiumnya dicabut.
    """
    conn = None
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        conn = get_conn()
        cur = conn.cursor()
        # Ambil dulu siapa yang expired
        cur.execute("""
            SELECT user_id FROM users
            WHERE is_premium = TRUE
              AND premium_until IS NOT NULL
              AND premium_until < %s
        """, (now,))
        rows = cur.fetchall()
        expired_ids = [r[0] for r in rows]
        if expired_ids:
            cur.execute("""
                UPDATE users
                SET is_premium = FALSE, premium_until = NULL
                WHERE is_premium = TRUE
                  AND premium_until IS NOT NULL
                  AND premium_until < %s
            """, (now,))
            conn.commit()
            logger.info(f"Premium expired for {len(expired_ids)} users: {expired_ids}")
        return expired_ids
    except Exception as e:
        logger.error(f"check_premium_expiry: {e}")
        return []
    finally:
        release(conn)


def increment_chats(user_id: int):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE users SET total_chats = total_chats + 1 WHERE user_id = %s", (user_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"increment_chats: {e}")
    finally:
        release(conn)


# ── Queue ─────────────────────────────────────────────

def join_queue(user: dict):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        is_premium = user.get("is_premium", False)
        cur.execute("""
            INSERT INTO queue (user_id, is_premium, gender, gender_filter, interests, joined_at)
            VALUES (%s, %s, %s, %s, %s,
                CASE WHEN %s THEN NOW() - INTERVAL '10 minutes' ELSE NOW() END
            )
            ON CONFLICT (user_id) DO NOTHING
        """, (
            user["user_id"],
            is_premium,
            user.get("gender"),
            user.get("gender_filter"),
            user.get("interests") or [],
            is_premium,
        ))
        conn.commit()
    except Exception as e:
        logger.error(f"join_queue: {e}")
    finally:
        release(conn)


def leave_queue(user_id: int):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM queue WHERE user_id = %s", (user_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"leave_queue: {e}")
    finally:
        release(conn)


def pop_match(seeker: dict):
    """
    Cari match terbaik:
    - Premium diutamakan (head-start 10 menit di joined_at)
    - Cocokkan gender_filter kedua arah
    - Interest overlap diutamakan
    """
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        seeker_id     = seeker["user_id"]
        seeker_gender = seeker.get("gender")
        seeker_filter = seeker.get("gender_filter")
        seeker_ints   = seeker.get("interests") or []

        cur.execute("""
            SELECT user_id
            FROM queue
            WHERE user_id != %s
              AND (gender_filter IS NULL OR gender_filter = %s OR %s IS NULL)
              AND (%s IS NULL OR gender = %s OR gender IS NULL)
            ORDER BY
                CASE WHEN interests && %s::text[] THEN 0 ELSE 1 END,
                joined_at ASC
            LIMIT 1
        """, (
            seeker_id,
            seeker_gender, seeker_gender,
            seeker_filter, seeker_filter,
            seeker_ints,
        ))
        row = cur.fetchone()
        if not row:
            return None

        matched_id = row["user_id"]
        cur.execute("DELETE FROM queue WHERE user_id = %s", (matched_id,))
        conn.commit()
        return matched_id

    except Exception as e:
        logger.error(f"pop_match: {e}")
        return None
    finally:
        release(conn)


def queue_count():
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM queue")
        return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"queue_count: {e}")
        return 0
    finally:
        release(conn)


# ── Stats ─────────────────────────────────────────────

def global_stats():
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users WHERE is_invisible = FALSE")
        total_users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE status = 'chatting' AND is_invisible = FALSE")
        active_chats = cur.fetchone()[0] // 2
        return total_users, active_chats
    except Exception as e:
        logger.error(f"global_stats: {e}")
        return 0, 0
    finally:
        release(conn)


# ── Report & Moderation ───────────────────────────────

def add_report(reporter_id: int, reported_id: int, reason: str):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id          SERIAL PRIMARY KEY,
                reporter_id BIGINT NOT NULL,
                reported_id BIGINT NOT NULL,
                reason      TEXT,
                status      TEXT DEFAULT 'pending',
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("""
            INSERT INTO reports (reporter_id, reported_id, reason)
            VALUES (%s, %s, %s)
        """, (reporter_id, reported_id, reason))
        conn.commit()
    except Exception as e:
        logger.error(f"add_report: {e}")
    finally:
        release(conn)


def get_pending_reports():
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT r.*, u.username as reported_username
            FROM reports r
            LEFT JOIN users u ON u.user_id = r.reported_id
            WHERE r.status = 'pending'
            ORDER BY r.created_at DESC
        """)
        return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"get_pending_reports: {e}")
        return []
    finally:
        release(conn)


def resolve_report(report_id: int):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE reports SET status = 'resolved' WHERE id = %s", (report_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"resolve_report: {e}")
    finally:
        release(conn)


def add_warning(user_id: int) -> int:
    """Tambah warning ke user, return jumlah warning sekarang."""
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE users SET warnings = warnings + 1 WHERE user_id = %s
            RETURNING warnings
        """, (user_id,))
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else 0
    except Exception as e:
        logger.error(f"add_warning: {e}")
        return 0
    finally:
        release(conn)


def ban_user(user_id: int, until=None, permanent: bool = False):
    """Ban user. until = datetime object untuk ban sementara, permanent = True untuk permanen."""
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        if permanent:
            cur.execute(
                "UPDATE users SET is_banned = TRUE, ban_until = NULL WHERE user_id = %s",
                (user_id,)
            )
        else:
            cur.execute(
                "UPDATE users SET is_banned = TRUE, ban_until = %s WHERE user_id = %s",
                (until, user_id)
            )
        conn.commit()
    except Exception as e:
        logger.error(f"ban_user: {e}")
    finally:
        release(conn)


def unban_user(user_id: int):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET is_banned = FALSE, ban_until = NULL WHERE user_id = %s",
            (user_id,)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"unban_user: {e}")
    finally:
        release(conn)


def is_banned(user_id: int) -> bool:
    """Cek apakah user masih kena ban."""
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT is_banned, ban_until FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if not row:
            return False
        if not row["is_banned"]:
            return False
        # Cek apakah ban sementara sudah habis
        if row["ban_until"] is not None:
            from datetime import datetime, timezone
            if datetime.now(timezone.utc) > row["ban_until"]:
                unban_user(user_id)
                return False
        return True
    except Exception as e:
        logger.error(f"is_banned: {e}")
        return False
    finally:
        release(conn)


def get_user_modinfo(user_id: int):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT user_id, username, warnings, is_banned, ban_until,
                   total_chats, is_premium, premium_until
            FROM users WHERE user_id = %s
        """, (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"get_user_modinfo: {e}")
        return None
    finally:
        release(conn)
