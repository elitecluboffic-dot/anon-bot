import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import Forbidden
import os

from src.db import (
    upsert_user, get_user, set_status, update_profile, update_filters,
    set_invisible, set_premium, increment_chats,
    join_queue, leave_queue, pop_match, queue_count, global_stats,
    check_premium_expiry,
    set_last_partner, clear_last_partner,  # ← tambahan dari db.py
)

logger = logging.getLogger(__name__)
OWNER_ID       = int(os.getenv("OWNER_ID", 0))
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "admin")

INTERESTS_LIST = ["music", "gaming", "anime", "sport", "film", "tech", "food", "travel", "art", "random"]


# ── Badge System ──────────────────────────────────────

def get_tier(user_data: dict) -> str:
    """Return tier: 'owner' | 'vip' | 'free'"""
    if not user_data:
        return "free"
    if user_data.get("user_id") == OWNER_ID:
        return "owner"
    if user_data.get("is_premium"):
        return "vip"
    return "free"


def build_match_msg_for(partner_data: dict) -> str:
    """
    Bangun pesan notif match sesuai tier si partner.
    Dikirim ke user yang sedang di-match-in.
    """
    tier = get_tier(partner_data)

    if tier == "owner":
        return (
            "👑 *PEMILIK BOT DITEMUKAN!*\n"
            "〰〰〰〰〰〰〰〰〰〰\n\n"
            "Wah kamu beruntung banget! Kamu\n"
            "lagi ngobrol sama *Pemilik Bot* langsung.\n\n"
            "🤫 Tetap anonim seperti biasa ya.\n\n"
            "▸ /next — cari stranger lain\n"
            "▸ /stop — akhiri chat"
        )
    elif tier == "vip":
        return (
            "💎 *STRANGER VIP DITEMUKAN!*\n"
            "〰〰〰〰〰〰〰〰〰〰\n\n"
            "✨ Stranger kamu adalah pengguna *VIP*!\n"
            "Nikmati obrolan spesialmu.\n\n"
            "🤫 Semua tetap anonim.\n\n"
            "▸ /next — cari stranger lain\n"
            "▸ /stop — akhiri chat"
        )
    else:
        return (
            "🎉 *STRANGER DITEMUKAN!*\n"
            "〰〰〰〰〰〰〰〰〰〰\n\n"
            "Kamu terhubung secara anonim.\n\n"
            "▸ /next — cari stranger lain\n"
            "▸ /stop — akhiri chat"
        )


def my_role_line(user_data: dict) -> str:
    """
    Baris kecil yang ditampilkan ke diri sendiri saat match —
    supaya mereka tahu role mereka sendiri.
    """
    tier = get_tier(user_data)
    if tier == "owner":
        return "👑 Kamu terhubung sebagai *Pemilik Bot*."
    elif tier == "vip":
        return "💎 Kamu terhubung sebagai pengguna *VIP*."
    return ""


# ── Helpers ───────────────────────────────────────────

async def safe_send(bot, chat_id: int, text: str, **kwargs):
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except Forbidden:
        logger.warning(f"User {chat_id} blocked bot")
        return None
    except Exception as e:
        logger.error(f"safe_send to {chat_id}: {e}")
        return None


async def try_match(bot, user_id: int):
    data = get_user(user_id)
    partner_id = pop_match(data)
    if not partner_id:
        return False

    leave_queue(user_id)
    set_status(user_id, "chatting", partner_id=partner_id)
    set_status(partner_id, "chatting", partner_id=user_id)
    increment_chats(user_id)
    increment_chats(partner_id)

    # Bersihkan last_partner_id saat match baru ditemukan
    clear_last_partner(user_id)
    clear_last_partner(partner_id)

    partner = get_user(partner_id)
    user    = get_user(user_id)

    # Pesan ke user: info tier si partner
    user_msg = build_match_msg_for(partner)
    role_line_user = my_role_line(user)
    if role_line_user:
        user_msg += f"\n\n{role_line_user}"

    # Pesan ke partner: info tier si user
    partner_msg = build_match_msg_for(user)
    role_line_partner = my_role_line(partner)
    if role_line_partner:
        partner_msg += f"\n\n{role_line_partner}"

    await safe_send(bot, user_id, user_msg, parse_mode=ParseMode.MARKDOWN)
    await safe_send(bot, partner_id, partner_msg, parse_mode=ParseMode.MARKDOWN)

    report_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚨 Laporkan Stranger", callback_data="report_open")
    ]])
    await safe_send(bot, user_id, "🛡 Merasa tidak nyaman? Tap tombol di bawah.", reply_markup=report_keyboard)
    await safe_send(bot, partner_id, "🛡 Merasa tidak nyaman? Tap tombol di bawah.", reply_markup=report_keyboard)
    return True


async def disconnect_pair(bot, user_id: int, user_data: dict, notify_self=True, notify_partner=True):
    partner_id = user_data.get("partner_id")

    # Simpan last_partner_id sebelum di-clear, supaya bisa dilaporkan setelah disconnect
    if partner_id:
        set_last_partner(user_id, partner_id)
        set_last_partner(partner_id, user_id)

    set_status(user_id, "idle", partner_id=None)
    if partner_id:
        set_status(partner_id, "idle", partner_id=None)
        if notify_partner:
            await safe_send(bot, partner_id,
                "👋 *Stranger meninggalkan chat.*\n\nKetik /find untuk cari lagi.",
                parse_mode=ParseMode.MARKDOWN
            )
    if notify_self:
        await safe_send(bot, user_id,
            "🔴 *Chat diakhiri.*\n\nKetik /find untuk cari stranger baru.",
            parse_mode=ParseMode.MARKDOWN
        )


def premium_required(func):
    """Decorator: tolak kalau user bukan premium."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        data = get_user(user.id)
        if not data or not data.get("is_premium"):
            await update.message.reply_text(
                "💎 Fitur ini khusus *VIP*.\n\n"
                "Ketik /premium untuk info upgrade.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


# ── Commands ──────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username or user.first_name)
    await update.message.reply_text(
        "👤 *Selamat datang di Anonymous Chat!*\n\n"
        "Ngobrol dengan stranger secara anonim.\n\n"
        "📋 *Perintah dasar:*\n"
        "▸ /find — Cari stranger\n"
        "▸ /next — Skip ke stranger lain\n"
        "▸ /stop — Akhiri chat\n"
        "▸ /profile — Atur profil & gender\n"
        "▸ /stats — Statistik\n"
        "▸ /premium — Info VIP\n"
        "▸ /help — Bantuan lengkap\n\n"
        "Ketik /find untuk mulai!",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username or user.first_name)
    data = get_user(user.id)

    if data["status"] == "chatting":
        await update.message.reply_text("⚠️ Kamu sedang chat. Ketik /next atau /stop dulu.")
        return
    if data["status"] == "searching":
        await update.message.reply_text(
            f"🔍 Masih mencari... ({queue_count()} dalam antrian)\nKetik /stop untuk batal."
        )
        return

    join_queue(data)
    set_status(user.id, "searching")

    matched = await try_match(ctx.bot, user.id)
    if not matched:
        filter_info = ""
        if data.get("is_premium"):
            gf = data.get("gender_filter")
            ints = data.get("interests") or []
            if gf:
                filter_info += f"\n🔍 Filter gender: *{gf}*"
            if ints:
                filter_info += f"\n🏷 Interests: {', '.join(ints)}"
        await update.message.reply_text(
            f"🔍 *Mencari stranger...*\n"
            f"Antrian: {queue_count()} orang{filter_info}\n\n"
            "Ketik /stop untuk batal.",
            parse_mode=ParseMode.MARKDOWN
        )


async def cmd_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user.id)
    if not data:
        await update.message.reply_text("Ketik /start dulu.")
        return

    if data["status"] == "chatting":
        await disconnect_pair(ctx.bot, user.id, data, notify_self=False, notify_partner=True)
        await update.message.reply_text("⏭ Chat diakhiri. Mencari stranger baru...")
    elif data["status"] == "searching":
        await update.message.reply_text("🔍 Masih mencari...")
        return
    else:
        await update.message.reply_text("🔍 Mencari stranger...")

    data = get_user(user.id)
    join_queue(data)
    set_status(user.id, "searching")
    matched = await try_match(ctx.bot, user.id)
    if not matched:
        await update.message.reply_text(
            f"🔍 *Mencari stranger...*\nAntrian: {queue_count()} orang\n\nKetik /stop untuk batal.",
            parse_mode=ParseMode.MARKDOWN
        )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user.id)
    if not data:
        await update.message.reply_text("Ketik /start dulu.")
        return

    if data["status"] == "idle":
        await update.message.reply_text("😴 Tidak sedang chat. Ketik /find untuk mulai.")
        return
    if data["status"] == "searching":
        leave_queue(user.id)
        set_status(user.id, "idle")
        await update.message.reply_text(
            "❌ *Pencarian dibatalkan.*\n\nKetik /find untuk cari lagi.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    if data["status"] == "chatting":
        await disconnect_pair(ctx.bot, user.id, data)


async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user.id)
    if not data:
        upsert_user(user.id, user.username or user.first_name)
        data = get_user(user.id)

    tier = get_tier(data)
    gender = data.get("gender") or "Belum diset"
    interests = data.get("interests") or []

    tier_display = {
        "owner": "👑 Pemilik Bot",
        "vip":   "💎 VIP",
        "free":  "👤 Free",
    }.get(tier, "👤 Free")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👨 Cowok", callback_data="gender_male"),
            InlineKeyboardButton("👩 Cewek", callback_data="gender_female"),
            InlineKeyboardButton("❓ Rahasia", callback_data="gender_none"),
        ],
        [InlineKeyboardButton("🏷 Atur Interests (VIP)", callback_data="set_interests")],
    ])

    gender_display = {"male": "👨 Cowok", "female": "👩 Cewek"}.get(gender, "❓ Rahasia")
    interests_display = ", ".join(interests) if interests else "Belum diset"

    await update.message.reply_text(
        f"👤 *Profilmu:*\n\n"
        f"Status : {tier_display}\n"
        f"Gender : {gender_display}\n"
        f"Interests : {interests_display}\n\n"
        "Pilih gender kamu:",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user.id)
    if not data or not data.get("is_premium"):
        await update.message.reply_text(
            "💎 Fitur *Gender Filter* khusus VIP.\n\nKetik /premium untuk info.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    current = data.get("gender_filter") or "Siapa saja"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👨 Cowok", callback_data="filter_male"),
            InlineKeyboardButton("👩 Cewek", callback_data="filter_female"),
            InlineKeyboardButton("🔀 Siapa saja", callback_data="filter_any"),
        ]
    ])
    await update.message.reply_text(
        f"🔍 *Filter Gender* 💎\n\n"
        f"Filter saat ini: *{current}*\n\n"
        "Pilih filter:",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_invisible(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user.id)
    if not data or not data.get("is_premium"):
        await update.message.reply_text(
            "💎 Fitur *Invisible Mode* khusus VIP.\n\nKetik /premium untuk info.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    current = data.get("is_invisible", False)
    new_val = not current
    set_invisible(user.id, new_val)
    status = "🟢 *Aktif*" if new_val else "🔴 *Nonaktif*"
    await update.message.reply_text(
        f"👻 *Invisible Mode* {status}\n\n"
        + ("Kamu tidak akan muncul di statistik global." if new_val else "Kamu akan muncul di statistik global."),
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_premium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user.id)
    tier = get_tier(data)

    if tier == "owner":
        await update.message.reply_text(
            "👑 *PEMILIK BOT*\n"
            "〰〰〰〰〰〰〰〰〰〰\n\n"
            "Kamu adalah pemilik bot ini.\n"
            "Semua fitur aktif selamanya.\n\n"
            "▸ /filter — Gender filter\n"
            "▸ /invisible — Invisible mode\n"
            "▸ /stats — Statistik global",
            parse_mode=ParseMode.MARKDOWN
        )
    elif tier == "vip":
        until = data.get("premium_until")
        if until:
            from datetime import timezone
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            until_str = until.strftime("%d %b %Y")
            expiry_info = f"📅 Aktif hingga: *{until_str}*"
        else:
            expiry_info = "📅 Masa aktif: *Permanen*"

        await update.message.reply_text(
            "💎 *KAMU ADALAH VIP!*\n"
            "〰〰〰〰〰〰〰〰〰〰\n\n"
            f"{expiry_info}\n\n"
            "*Fitur aktif:*\n"
            "▸ 🔍 Gender Filter — /filter\n"
            "▸ 🏷 Interest Tags — /profile\n"
            "▸ 👻 Invisible Mode — /invisible\n"
            "▸ 🚀 Priority Queue (otomatis aktif)\n\n"
            "💎 Terima kasih sudah jadi VIP!",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            "💎 *ANONYMOUS CHAT VIP*\n"
            "〰〰〰〰〰〰〰〰〰〰\n\n"
            "*Fitur eksklusif:*\n"
            "▸ 🔍 *Gender Filter* — pilih mau chat sama siapa\n"
            "▸ 🏷 *Interest Tags* — match berdasarkan topik\n"
            "▸ 👻 *Invisible Mode* — hilang dari statistik global\n"
            "▸ 🚀 *Priority Queue* — dapat match lebih cepat\n"
            "▸ 💎 *Badge VIP* — keliatan keren pas ketemu stranger\n\n"
            "💰 *Harga:* Rp50.000 / bulan\n\n"
            f"Upgrade sekarang, hubungi @{OWNER_USERNAME}",
            parse_mode=ParseMode.MARKDOWN
        )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user.id)
    if not data:
        await update.message.reply_text("Ketik /start dulu.")
        return

    total_users, active_chats = global_stats()
    tier = get_tier(data)

    status_map = {"idle": "😴 Idle", "searching": "🔍 Mencari", "chatting": "💬 Chatting"}
    status_str = status_map.get(data["status"], data["status"])
    gender_display = {"male": "👨 Cowok", "female": "👩 Cewek"}.get(data.get("gender") or "", "❓")

    tier_display = {
        "owner": "👑 Pemilik Bot",
        "vip":   "💎 VIP",
        "free":  "👤 Free",
    }.get(tier, "👤 Free")

    if tier == "vip":
        until = data.get("premium_until")
        if until:
            from datetime import timezone
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            tier_display += f" (hingga {until.strftime('%d %b %Y')})"
        else:
            tier_display += " (Permanen)"

    await update.message.reply_text(
        f"📊 *Statistikmu:*\n"
        f"〰〰〰〰〰〰〰〰〰〰\n"
        f"Total chat : *{data['total_chats']}*\n"
        f"Status     : {status_str}\n"
        f"Gender     : {gender_display}\n"
        f"Role       : {tier_display}\n\n"
        f"🌍 *Global:*\n"
        f"Total user : *{total_users}*\n"
        f"Chat aktif : *{active_chats}*",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Perintah Lengkap:*\n\n"
        "*Dasar:*\n"
        "▸ /find — Cari stranger\n"
        "▸ /next — Skip ke stranger lain\n"
        "▸ /stop — Akhiri chat / batalkan pencarian\n"
        "▸ /profile — Atur gender & interests\n"
        "▸ /stats — Statistik kamu & global\n\n"
        "*VIP 💎:*\n"
        "▸ /filter — Filter gender stranger\n"
        "▸ /invisible — Toggle invisible mode\n"
        "▸ /premium — Info & fitur VIP\n\n"
        "💡 Semua pesan dikirim anonim.\n"
        "Foto, stiker, voice note semua support.",
        parse_mode=ParseMode.MARKDOWN
    )


# ── Admin Commands ────────────────────────────────────

async def cmd_addpremium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    if not ctx.args:
        await update.message.reply_text(
            "Usage:\n"
            "/addpremium <user_id>\n"
            "/addpremium <user_id> <hari>\n\n"
            "Contoh: /addpremium 123456789 30"
        )
        return

    try:
        target_id = int(ctx.args[0])
        days = int(ctx.args[1]) if len(ctx.args) > 1 else 30
        set_premium(target_id, True, days=days)

        from datetime import datetime, timedelta, timezone
        until = datetime.now(timezone.utc) + timedelta(days=days)
        until_str = until.strftime("%d %b %Y")

        await update.message.reply_text(
            f"✅ User `{target_id}` sekarang *VIP* selama *{days} hari*.\n"
            f"Berakhir: {until_str}",
            parse_mode=ParseMode.MARKDOWN
        )
        await safe_send(ctx.bot, target_id,
            "💎 *SELAMAT! KAMU SEKARANG VIP!*\n"
            "〰〰〰〰〰〰〰〰〰〰\n\n"
            f"Masa aktif: *{days} hari* (hingga {until_str})\n\n"
            "*Fitur yang tersedia:*\n"
            "▸ 🔍 Gender Filter — /filter\n"
            "▸ 🏷 Interest Tags — /profile\n"
            "▸ 👻 Invisible Mode — /invisible\n"
            "▸ 🚀 Priority Queue (otomatis aktif)\n"
            "▸ 💎 Badge VIP saat ketemu stranger\n\n"
            "Selamat menikmati fitur VIP kamu!",
            parse_mode=ParseMode.MARKDOWN
        )
    except ValueError:
        await update.message.reply_text("❌ Format salah. Contoh: /addpremium 123456789 30")


async def cmd_removepremium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    if not ctx.args:
        await update.message.reply_text("Usage: /removepremium <user_id>")
        return

    try:
        target_id = int(ctx.args[0])
        set_premium(target_id, False)
        await update.message.reply_text(
            f"✅ VIP user `{target_id}` dicabut.",
            parse_mode=ParseMode.MARKDOWN
        )
        await safe_send(ctx.bot, target_id,
            "⚠️ *VIP kamu telah dicabut.*\n\n"
            "Hubungi admin jika ada pertanyaan.\n"
            f"@{OWNER_USERNAME}",
            parse_mode=ParseMode.MARKDOWN
        )
    except ValueError:
        await update.message.reply_text("❌ user_id harus angka.")


# ── Scheduled Jobs ────────────────────────────────────

async def daily_backup(context: ContextTypes.DEFAULT_TYPE):
    expired = check_premium_expiry()
    for uid in expired:
        await safe_send(context.bot, uid,
            "⚠️ *VIP kamu telah berakhir.*\n\n"
            "Ketik /premium untuk memperpanjang.",
            parse_mode=ParseMode.MARKDOWN
        )
    if expired:
        await safe_send(context.bot, OWNER_ID,
            f"ℹ️ VIP expired: {len(expired)} user\nID: {expired}",
            parse_mode=ParseMode.MARKDOWN
        )

    try:
        from src.backup import do_backup
        filepath = do_backup()
        filename = os.path.basename(filepath)
        with open(filepath, "rb") as f:
            await context.bot.send_document(
                chat_id=OWNER_ID,
                document=f,
                filename=filename,
                caption=f"✅ Auto-backup: `{filename}`",
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception as e:
        logger.error(f"daily_backup error: {e}")
        await safe_send(context.bot, OWNER_ID, f"❌ Auto-backup gagal: {e}")


# ── Callback Handler ──────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = query.from_user
    data  = query.data
    await query.answer()

    if data.startswith("gender_"):
        val = data.split("_")[1]
        gender = None if val == "none" else val
        update_profile(user.id, gender=gender)
        display = {"male": "👨 Cowok", "female": "👩 Cewek"}.get(gender or "", "❓ Rahasia")
        await query.edit_message_text(f"✅ Gender diset ke: *{display}*", parse_mode=ParseMode.MARKDOWN)

    elif data == "set_interests":
        user_data = get_user(user.id)
        if not user_data or not user_data.get("is_premium"):
            await query.edit_message_text("💎 Fitur ini khusus VIP. Ketik /premium untuk info.")
            return
        current_ints = user_data.get("interests") or []
        buttons = []
        row = []
        for i, interest in enumerate(INTERESTS_LIST):
            mark = "✅ " if interest in current_ints else ""
            row.append(InlineKeyboardButton(f"{mark}{interest}", callback_data=f"int_{interest}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("💾 Simpan", callback_data="int_save")])
        ctx.user_data["editing_interests"] = list(current_ints)
        await query.edit_message_text(
            "🏷 *Pilih interests kamu* (maks 3):",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.MARKDOWN
        )

    elif data.startswith("int_") and data != "int_save":
        interest = data[4:]
        editing = ctx.user_data.get("editing_interests", [])
        if interest in editing:
            editing.remove(interest)
        elif len(editing) < 3:
            editing.append(interest)
        ctx.user_data["editing_interests"] = editing

        buttons = []
        row = []
        for i, itm in enumerate(INTERESTS_LIST):
            mark = "✅ " if itm in editing else ""
            row.append(InlineKeyboardButton(f"{mark}{itm}", callback_data=f"int_{itm}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("💾 Simpan", callback_data="int_save")])
        await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))

    elif data == "int_save":
        editing = ctx.user_data.get("editing_interests", [])
        update_profile(user.id, interests=editing)
        await query.edit_message_text(
            f"✅ Interests disimpan: *{', '.join(editing) if editing else 'kosong'}*",
            parse_mode=ParseMode.MARKDOWN
        )
        ctx.user_data.pop("editing_interests", None)

    elif data.startswith("filter_"):
        user_data = get_user(user.id)
        if not user_data or not user_data.get("is_premium"):
            await query.edit_message_text("💎 Fitur ini khusus VIP.")
            return
        val = data.split("_")[1]
        if val == "any":
            update_filters(user.id, "reset")
            await query.edit_message_text("✅ Filter gender: *Siapa saja*", parse_mode=ParseMode.MARKDOWN)
        else:
            update_filters(user.id, val)
            display = {"male": "👨 Cowok", "female": "👩 Cewek"}.get(val, val)
            await query.edit_message_text(f"✅ Filter gender: *{display}*", parse_mode=ParseMode.MARKDOWN)

    elif data == "report_open":
        user_data = get_user(user.id)
        if not user_data:
            await query.edit_message_text("⚠️ Kamu tidak sedang dalam chat.")
            return

        # Bisa lapor saat chatting ATAU setelah disconnect (pakai last_partner_id)
        if user_data["status"] != "chatting" and not user_data.get("last_partner_id"):
            await query.edit_message_text("⚠️ Kamu tidak sedang dalam chat.")
            return

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=f"report_{code}")]
            for label, code in REPORT_REASONS
        ] + [[InlineKeyboardButton("❌ Batal", callback_data="report_cancel")]])
        await query.edit_message_text(
            "🚨 *Laporkan Stranger*\n\nPilih alasan laporan:",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "report_cancel":
        await query.edit_message_text("✅ Laporan dibatalkan.")

    elif data.startswith("report_") and data != "report_open" and data != "report_cancel":
        reason = data[7:]
        user_data = get_user(user.id)
        if not user_data:
            await query.edit_message_text("⚠️ Terjadi kesalahan.")
            return

        # Ambil partner_id: dari sesi aktif atau dari last_partner_id
        if user_data["status"] == "chatting" and user_data.get("partner_id"):
            partner_id = user_data["partner_id"]
        elif user_data.get("last_partner_id"):
            partner_id = user_data["last_partner_id"]
        else:
            await query.edit_message_text("❌ Tidak ada stranger yang bisa dilaporkan.")
            return

        from src.db import add_report
        add_report(reporter_id=user.id, reported_id=partner_id, reason=reason)

        # Bersihkan last_partner_id setelah laporan dikirim
        clear_last_partner(user.id)

        await query.edit_message_text(
            "✅ *Laporan dikirim ke admin.*\n\nTerima kasih, admin akan meninjau segera.",
            parse_mode=ParseMode.MARKDOWN,
        )

        reporter_display = f"@{user.username}" if user.username else str(user.id)
        await safe_send(ctx.bot, OWNER_ID,
            f"🚨 *Laporan Baru!*\n\n"
            f"Pelapor   : {reporter_display} (`{user.id}`)\n"
            f"Dilaporkan: `{partner_id}`\n"
            f"Alasan    : *{reason}*\n\n"
            f"Tindakan:\n"
            f"/warn {partner_id}\n"
            f"/ban {partner_id} 7d\n"
            f"/ban {partner_id} permanent\n"
            f"/userinfo {partner_id}",
            parse_mode=ParseMode.MARKDOWN,
        )


# ── Message Forwarder ─────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg  = update.message
    data = get_user(user.id)

    if not data:
        upsert_user(user.id, user.username or user.first_name)
        await msg.reply_text("Ketik /start dulu.")
        return

    from src.db import is_banned
    if is_banned(user.id):
        await msg.reply_text(
            "🔨 *Kamu sedang di-ban* dan tidak bisa menggunakan bot ini.\n"
            "Hubungi admin jika ada keberatan.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if data["status"] != "chatting":
        if data["status"] == "searching":
            await msg.reply_text("🔍 Masih mencari stranger...")
        else:
            await msg.reply_text("Ketik /find untuk cari stranger dulu.")
        return

    partner_id = data["partner_id"]
    if not partner_id:
        set_status(user.id, "idle")
        await msg.reply_text("❌ Error. Ketik /find untuk cari lagi.")
        return

    try:
        if msg.text:
            await ctx.bot.send_message(partner_id, f"👤 *Stranger:*\n{msg.text}", parse_mode=ParseMode.MARKDOWN)
        elif msg.sticker:
            await ctx.bot.send_message(partner_id, "👤 *Stranger* mengirim stiker:", parse_mode=ParseMode.MARKDOWN)
            await ctx.bot.send_sticker(partner_id, msg.sticker.file_id)
        elif msg.photo:
            cap = f"👤 *Stranger:*\n{msg.caption}" if msg.caption else "👤 *Stranger* mengirim foto:"
            await ctx.bot.send_photo(partner_id, msg.photo[-1].file_id, caption=cap, parse_mode=ParseMode.MARKDOWN)
        elif msg.video:
            cap = f"👤 *Stranger:*\n{msg.caption}" if msg.caption else "👤 *Stranger* mengirim video:"
            await ctx.bot.send_video(partner_id, msg.video.file_id, caption=cap, parse_mode=ParseMode.MARKDOWN)
        elif msg.voice:
            await ctx.bot.send_message(partner_id, "👤 *Stranger* mengirim voice note:", parse_mode=ParseMode.MARKDOWN)
            await ctx.bot.send_voice(partner_id, msg.voice.file_id)
        elif msg.audio:
            await ctx.bot.send_message(partner_id, "👤 *Stranger* mengirim audio:", parse_mode=ParseMode.MARKDOWN)
            await ctx.bot.send_audio(partner_id, msg.audio.file_id)
        elif msg.document:
            cap = f"👤 *Stranger:*\n{msg.caption}" if msg.caption else "👤 *Stranger* mengirim file:"
            await ctx.bot.send_document(partner_id, msg.document.file_id, caption=cap, parse_mode=ParseMode.MARKDOWN)
        elif msg.video_note:
            await ctx.bot.send_message(partner_id, "👤 *Stranger* mengirim video note:", parse_mode=ParseMode.MARKDOWN)
            await ctx.bot.send_video_note(partner_id, msg.video_note.file_id)
        elif msg.location:
            await ctx.bot.send_message(partner_id, "👤 *Stranger* mengirim lokasi:", parse_mode=ParseMode.MARKDOWN)
            await ctx.bot.send_location(partner_id, msg.location.latitude, msg.location.longitude)
        else:
            await msg.reply_text("❌ Jenis pesan ini belum didukung.")

    except Forbidden:
        await disconnect_pair(ctx.bot, user.id, data, notify_self=True, notify_partner=False)
        await safe_send(ctx.bot, user.id, "⚠️ Stranger tidak bisa dihubungi. Chat diakhiri.")
    except Exception as e:
        logger.error(f"forward error: {e}")
        await msg.reply_text("❌ Gagal kirim pesan.")


# ── Error Handler ─────────────────────────────────────

async def error_handler(update, context):
    logger.error("Exception:", exc_info=context.error)


# ── Backup & Restore ──────────────────────────────────

async def cmd_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text("⏳ Membuat backup...")
    try:
        from src.backup import do_backup
        filepath = do_backup()
        filename = os.path.basename(filepath)
        with open(filepath, "rb") as f:
            await ctx.bot.send_document(
                chat_id=update.effective_user.id,
                document=f,
                filename=filename,
                caption=f"✅ Backup selesai: `{filename}`",
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Backup gagal: {e}")


async def cmd_restore(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.document:
        await msg.reply_text(
            "📂 Cara restore:\n"
            "1. Kirim file backup JSON ke chat ini\n"
            "2. Reply file tersebut dengan /restore"
        )
        return

    doc = msg.reply_to_message.document
    if not doc.file_name.endswith(".json"):
        await msg.reply_text("❌ File harus berformat .json")
        return

    await msg.reply_text("⏳ Memproses restore...")
    try:
        from src.backup import do_restore
        import tempfile

        file = await ctx.bot.get_file(doc.file_id)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            count = do_restore(tmp.name)

        await msg.reply_text(
            f"✅ *Restore selesai!*\n{count} user berhasil di-restore.",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        await msg.reply_text(f"❌ Restore gagal: {e}")


# ── Report System ─────────────────────────────────────

REPORT_REASONS = [
    ("🔞 Pelecehan seksual", "pelecehan"),
    ("😡 Pembullyan",        "bullying"),
    ("🤬 Kata kasar/SARA",   "kasar"),
    ("🔗 Spam/iklan",        "spam"),
    ("⚠️ Konten berbahaya",  "berbahaya"),
    ("📛 Lainnya",           "lainnya"),
]


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user.id)

    if not data or data["status"] != "chatting":
        await update.message.reply_text("⚠️ Kamu tidak sedang dalam chat.")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"report_{code}")]
        for label, code in REPORT_REASONS
    ] + [[InlineKeyboardButton("❌ Batal", callback_data="report_cancel")]])

    await update.message.reply_text(
        "🚨 *Laporkan Stranger*\n\nPilih alasan laporan:",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN,
    )


async def _send_report_button(bot, chat_id: int):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚨 Laporkan Stranger", callback_data="report_open")
    ]])
    await safe_send(bot, chat_id, "🛡 Merasa tidak nyaman? Tap tombol di bawah.", reply_markup=keyboard)


# ── Moderation Commands (Admin) ───────────────────────

async def cmd_warn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /warn <user_id>")
        return
    try:
        from src.db import add_warning
        target_id = int(ctx.args[0])
        count = add_warning(target_id)
        await update.message.reply_text(
            f"⚠️ Peringatan dikirim ke user `{target_id}`.\n"
            f"Total peringatan: *{count}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        await safe_send(ctx.bot, target_id,
            f"⚠️ *Peringatan dari Admin*\n\n"
            f"Kamu mendapat peringatan ke-*{count}* karena melanggar aturan.\n"
            f"Ulangi lagi dan kamu akan di-ban.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except ValueError:
        await update.message.reply_text("❌ user_id harus angka.")


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage:\n"
            "/ban <user_id> permanent\n"
            "/ban <user_id> 7d\n"
            "/ban <user_id> 1m\n"
            "/ban <user_id> 1y"
        )
        return
    try:
        from src.db import ban_user
        from datetime import datetime, timedelta, timezone

        target_id = int(ctx.args[0])
        duration  = ctx.args[1].lower() if len(ctx.args) > 1 else "permanent"

        target_data = get_user(target_id)
        if target_data and target_data["status"] == "chatting":
            await disconnect_pair(ctx.bot, target_id, target_data, notify_self=False, notify_partner=True)

        if duration == "permanent":
            ban_user(target_id, permanent=True)
            label = "permanen"
            until_str = "Permanen"
        else:
            now = datetime.now(timezone.utc)
            if duration.endswith("d"):
                until = now + timedelta(days=int(duration[:-1]))
                label = f"{duration[:-1]} hari"
            elif duration.endswith("m"):
                until = now + timedelta(days=int(duration[:-1]) * 30)
                label = f"{duration[:-1]} bulan"
            elif duration.endswith("y"):
                until = now + timedelta(days=int(duration[:-1]) * 365)
                label = f"{duration[:-1]} tahun"
            else:
                await update.message.reply_text("❌ Format durasi salah. Gunakan: 7d / 1m / 1y / permanent")
                return
            ban_user(target_id, until=until)
            until_str = until.strftime("%d %b %Y %H:%M UTC")

        await update.message.reply_text(
            f"🔨 User `{target_id}` di-ban *{label}*.\nBerakhir: {until_str}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await safe_send(ctx.bot, target_id,
            f"🔨 *Kamu telah di-ban* selama *{label}*.\n\n"
            "Alasan: Melanggar aturan komunitas.\n"
            f"Berakhir: {until_str}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Format salah.")


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        from src.db import unban_user
        target_id = int(ctx.args[0])
        unban_user(target_id)
        await update.message.reply_text(f"✅ User `{target_id}` di-unban.", parse_mode=ParseMode.MARKDOWN)
        await safe_send(ctx.bot, target_id, "✅ Ban kamu telah dicabut. Kamu bisa chat lagi.")
    except ValueError:
        await update.message.reply_text("❌ user_id harus angka.")


async def cmd_userinfo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /userinfo <user_id>")
        return
    try:
        from src.db import get_user_modinfo
        target_id = int(ctx.args[0])
        info = get_user_modinfo(target_id)
        if not info:
            await update.message.reply_text("❌ User tidak ditemukan.")
            return

        ban_status = (
            "🔨 Di-ban permanen" if info.get("is_banned") and not info.get("ban_until")
            else f"🔨 Di-ban sampai {info['ban_until']}" if info.get("is_banned")
            else "✅ Tidak di-ban"
        )

        if info.get("user_id") == OWNER_ID:
            role_str = "👑 Pemilik Bot"
        elif info.get("is_premium"):
            until = info.get("premium_until")
            if until:
                from datetime import timezone
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                role_str = f"💎 VIP (hingga {until.strftime('%d %b %Y')})"
            else:
                role_str = "💎 VIP (Permanen)"
        else:
            role_str = "👤 Free"

        await update.message.reply_text(
            f"👤 *User Info:*\n"
            f"〰〰〰〰〰〰〰〰〰〰\n"
            f"ID         : `{info['user_id']}`\n"
            f"Username   : @{info.get('username', '-')}\n"
            f"Total chat : {info.get('total_chats', 0)}\n"
            f"Peringatan : {info.get('warnings', 0)}\n"
            f"Role       : {role_str}\n"
            f"Status ban : {ban_status}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except ValueError:
        await update.message.reply_text("❌ user_id harus angka.")


async def cmd_reports(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    from src.db import get_pending_reports
    reports = get_pending_reports()
    if not reports:
        await update.message.reply_text("✅ Tidak ada laporan pending.")
        return
    text = f"📋 *Laporan Pending ({len(reports)}):*\n〰〰〰〰〰〰〰〰〰〰\n\n"
    for r in reports[:10]:
        text += (
            f"🆔 Report #{r['id']}\n"
            f"Dilaporkan : `{r['reported_id']}` (@{r.get('reported_username', '-')})\n"
            f"Alasan     : {r['reason']}\n"
            f"Waktu      : {r['created_at'].strftime('%d %b %Y %H:%M')}\n\n"
        )
    text += "Tindakan: /warn /ban /unban /userinfo"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
