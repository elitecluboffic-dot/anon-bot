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
)

logger = logging.getLogger(__name__)
OWNER_ID = int(os.getenv("OWNER_ID", 0))

INTERESTS_LIST = ["music", "gaming", "anime", "sport", "film", "tech", "food", "travel", "art", "random"]


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

    partner = get_user(partner_id)
    user    = get_user(user_id)

    def badge(u):
        return "⭐ *Premium*" if u and u.get("is_premium") else ""

    await safe_send(bot, user_id,
        f"🎉 *Stranger ditemukan!* {badge(partner)}\n\n"
        "Kamu terhubung secara anonim.\n"
        "/next — cari stranger lain\n"
        "/stop — akhiri chat",
        parse_mode=ParseMode.MARKDOWN
    )
    await safe_send(bot, partner_id,
        f"🎉 *Stranger ditemukan!* {badge(user)}\n\n"
        "Kamu terhubung secara anonim.\n"
        "/next — cari stranger lain\n"
        "/stop — akhiri chat",
        parse_mode=ParseMode.MARKDOWN
    )
    return True


async def disconnect_pair(bot, user_id: int, user_data: dict, notify_self=True, notify_partner=True):
    partner_id = user_data.get("partner_id")
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
                "⭐ Fitur ini khusus *Premium*.\n\n"
                "Hubungi admin untuk upgrade: /premium",
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
        "/find — Cari stranger\n"
        "/next — Skip ke stranger lain\n"
        "/stop — Akhiri chat\n"
        "/profile — Atur profil & gender\n"
        "/stats — Statistik\n"
        "/premium — Info Premium\n"
        "/help — Bantuan lengkap\n\n"
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
        await update.message.reply_text("❌ *Pencarian dibatalkan.*\n\nKetik /find untuk cari lagi.", parse_mode=ParseMode.MARKDOWN)
        return
    if data["status"] == "chatting":
        await disconnect_pair(ctx.bot, user.id, data)


async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user.id)
    if not data:
        upsert_user(user.id, user.username or user.first_name)
        data = get_user(user.id)

    gender = data.get("gender") or "Belum diset"
    interests = data.get("interests") or []
    is_premium = data.get("is_premium", False)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👨 Cowok", callback_data="gender_male"),
            InlineKeyboardButton("👩 Cewek", callback_data="gender_female"),
            InlineKeyboardButton("❓ Rahasia", callback_data="gender_none"),
        ],
        [InlineKeyboardButton("🏷 Atur Interests (Premium)", callback_data="set_interests")],
    ])

    gender_display = {"male": "👨 Cowok", "female": "👩 Cewek"}.get(gender, "❓ Rahasia")
    interests_display = ", ".join(interests) if interests else "Belum diset"

    await update.message.reply_text(
        f"👤 *Profilmu:*\n\n"
        f"Gender: {gender_display}\n"
        f"Interests: {interests_display}\n"
        f"Status: {'⭐ Premium' if is_premium else 'Free'}\n\n"
        "Pilih gender kamu:",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user.id)
    if not data or not data.get("is_premium"):
        await update.message.reply_text(
            "⭐ Fitur *Gender Filter* khusus Premium.\n\nKetik /premium untuk info.",
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
        f"🔍 *Filter Gender* ⭐\n\n"
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
            "⭐ Fitur *Invisible Mode* khusus Premium.\n\nKetik /premium untuk info.",
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
    data = get_user(update.effective_user.id)
    is_premium = data and data.get("is_premium")

    if is_premium:
        await update.message.reply_text(
            "⭐ *Kamu sudah Premium!*\n\n"
            "Fitur aktif:\n"
            "• 🔍 Gender Filter — /filter\n"
            "• 🏷 Interest Tags — /profile\n"
            "• 👻 Invisible Mode — /invisible\n"
            "• 🚀 Priority Queue (otomatis aktif)",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            "⭐ *Anonymous Chat Premium*\n\n"
            "Fitur eksklusif:\n"
            "• 🔍 *Gender Filter* — pilih mau chat sama siapa\n"
            "• 🏷 *Interest Tags* — match berdasarkan topik\n"
            "• 👻 *Invisible Mode* — hilang dari statistik global\n"
            "• 🚀 *Priority Queue* — dapat match lebih cepat\n\n"
            "Untuk upgrade, hubungi admin bot ini.",
            parse_mode=ParseMode.MARKDOWN
        )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user(user.id)
    if not data:
        await update.message.reply_text("Ketik /start dulu.")
        return

    total_users, active_chats = global_stats()
    status_map = {"idle": "😴 Idle", "searching": "🔍 Mencari", "chatting": "💬 Chatting"}
    status_str = status_map.get(data["status"], data["status"])
    gender_display = {"male": "👨 Cowok", "female": "👩 Cewek"}.get(data.get("gender") or "", "❓")

    await update.message.reply_text(
        f"📊 *Statistikmu:*\n"
        f"Total chat: *{data['total_chats']}*\n"
        f"Status: {status_str}\n"
        f"Gender: {gender_display}\n"
        f"Premium: {'⭐ Ya' if data.get('is_premium') else 'Tidak'}\n\n"
        f"🌍 *Global:*\n"
        f"Total user: *{total_users}*\n"
        f"Chat aktif: *{active_chats}*",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Perintah Lengkap:*\n\n"
        "*Dasar:*\n"
        "/find — Cari stranger\n"
        "/next — Skip ke stranger lain\n"
        "/stop — Akhiri chat / batalkan pencarian\n"
        "/profile — Atur gender & interests\n"
        "/stats — Statistik kamu & global\n\n"
        "*Premium ⭐:*\n"
        "/filter — Filter gender stranger\n"
        "/invisible — Toggle invisible mode\n"
        "/premium — Info & fitur premium\n\n"
        "💡 Semua pesan dikirim anonim.\n"
        "Foto, stiker, voice note semua support.",
        parse_mode=ParseMode.MARKDOWN
    )


# ── Admin Commands ────────────────────────────────────

async def cmd_addpremium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    if not ctx.args:
        await update.message.reply_text("Usage: /addpremium <user_id>")
        return

    try:
        target_id = int(ctx.args[0])
        set_premium(target_id, True)
        await update.message.reply_text(f"✅ User {target_id} sekarang Premium.")
    except ValueError:
        await update.message.reply_text("❌ user_id harus angka.")


async def cmd_removepremium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    if not ctx.args:
        await update.message.reply_text("Usage: /removepremium <user_id>")
        return

    try:
        target_id = int(ctx.args[0])
        set_premium(target_id, False)
        await update.message.reply_text(f"✅ Premium user {target_id} dicabut.")
    except ValueError:
        await update.message.reply_text("❌ user_id harus angka.")


# ── Callback Handler ──────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = query.from_user
    data  = query.data
    await query.answer()

    # Gender profile
    if data.startswith("gender_"):
        val = data.split("_")[1]
        gender = None if val == "none" else val
        update_profile(user.id, gender=gender)
        display = {"male": "👨 Cowok", "female": "👩 Cewek"}.get(gender or "", "❓ Rahasia")
        await query.edit_message_text(f"✅ Gender diset ke: *{display}*", parse_mode=ParseMode.MARKDOWN)

    # Interest set (premium)
    elif data == "set_interests":
        user_data = get_user(user.id)
        if not user_data or not user_data.get("is_premium"):
            await query.edit_message_text("⭐ Fitur ini khusus Premium. Ketik /premium untuk info.")
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

        user_data = get_user(user.id)
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

    # Gender filter (premium)
    elif data.startswith("filter_"):
        user_data = get_user(user.id)
        if not user_data or not user_data.get("is_premium"):
            await query.edit_message_text("⭐ Fitur ini khusus Premium.")
            return
        val = data.split("_")[1]
        if val == "any":
            update_filters(user.id, "reset")
            await query.edit_message_text("✅ Filter gender: *Siapa saja*", parse_mode=ParseMode.MARKDOWN)
        else:
            update_filters(user.id, val)
            display = {"male": "👨 Cowok", "female": "👩 Cewek"}.get(val, val)
            await query.edit_message_text(f"✅ Filter gender: *{display}*", parse_mode=ParseMode.MARKDOWN)


# ── Message Forwarder ─────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg  = update.message
    data = get_user(user.id)

    if not data:
        upsert_user(user.id, user.username or user.first_name)
        await msg.reply_text("Ketik /start dulu.")
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

    # Harus reply ke file JSON yang dikirim
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
