import json
import os
import logging
from datetime import datetime
from src.db import get_conn, release

logger = logging.getLogger(__name__)

BACKUP_DIR = "backups"


def _ensure_dir():
    os.makedirs(BACKUP_DIR, exist_ok=True)


def do_backup() -> str:
    """Export semua data users ke JSON. Return path file backup."""
    _ensure_dir()
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("SELECT user_id, username, total_chats, gender, interests, gender_filter, is_premium, is_invisible, created_at FROM users")
        cols = [d[0] for d in cur.description]
        users = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            # convert datetime to string
            if d.get("created_at"):
                d["created_at"] = d["created_at"].isoformat()
            # convert list
            if d.get("interests") is None:
                d["interests"] = []
            users.append(d)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(BACKUP_DIR, f"backup_{timestamp}.json")

        with open(filename, "w", encoding="utf-8") as f:
            json.dump({"timestamp": timestamp, "users": users}, f, ensure_ascii=False, indent=2)

        # Hapus backup lama, simpan 7 terakhir
        _cleanup_old_backups()

        logger.info(f"Backup selesai: {filename} ({len(users)} users)")
        return filename

    except Exception as e:
        logger.error(f"do_backup error: {e}")
        raise
    finally:
        release(conn)


def do_restore(filepath: str) -> int:
    """Restore users dari file JSON. Return jumlah user yang di-restore."""
    conn = None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        users = data.get("users", [])
        if not users:
            return 0

        conn = get_conn()
        cur = conn.cursor()

        restored = 0
        for u in users:
            cur.execute("""
                INSERT INTO users (user_id, username, total_chats, gender, interests, gender_filter, is_premium, is_invisible)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    username      = EXCLUDED.username,
                    total_chats   = EXCLUDED.total_chats,
                    gender        = EXCLUDED.gender,
                    interests     = EXCLUDED.interests,
                    gender_filter = EXCLUDED.gender_filter,
                    is_premium    = EXCLUDED.is_premium,
                    is_invisible  = EXCLUDED.is_invisible
            """, (
                u["user_id"],
                u.get("username", "anon"),
                u.get("total_chats", 0),
                u.get("gender"),
                u.get("interests") or [],
                u.get("gender_filter"),
                u.get("is_premium", False),
                u.get("is_invisible", False),
            ))
            restored += 1

        conn.commit()
        logger.info(f"Restore selesai: {restored} users dari {filepath}")
        return restored

    except Exception as e:
        logger.error(f"do_restore error: {e}")
        raise
    finally:
        release(conn)


def latest_backup() -> str | None:
    """Return path backup terbaru, atau None kalau belum ada."""
    _ensure_dir()
    files = sorted([
        os.path.join(BACKUP_DIR, f)
        for f in os.listdir(BACKUP_DIR)
        if f.startswith("backup_") and f.endswith(".json")
    ])
    return files[-1] if files else None


def list_backups() -> list[str]:
    _ensure_dir()
    return sorted([
        f for f in os.listdir(BACKUP_DIR)
        if f.startswith("backup_") and f.endswith(".json")
    ], reverse=True)


def _cleanup_old_backups(keep: int = 7):
    """Hapus backup lama, simpan `keep` file terbaru."""
    _ensure_dir()
    files = sorted([
        os.path.join(BACKUP_DIR, f)
        for f in os.listdir(BACKUP_DIR)
        if f.startswith("backup_") and f.endswith(".json")
    ])
    for old in files[:-keep]:
        try:
            os.remove(old)
            logger.info(f"Hapus backup lama: {old}")
        except Exception:
            pass
