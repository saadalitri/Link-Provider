"""
Multi-Channel Force Subscribe Bot — MongoDB + Render Compatible
------------------------------------------------------------------------------------
Commands:
  /start                   - welcome message with photo; admins also get the
                              admin panel (menu of every action below)
  /start <code>            - open a specific post link

  /add   (/addchannel)     - register a channel (category: drama, legacy default)
                              (reply to a forwarded post from that channel);
                              also generates a link for that specific post
  /addA                     - same, but registers the channel under Anime
  /addD                     - same, but registers the channel under Drama
  /del   (/removechannel)  - pick a channel to remove (any category)
  /channels                - pick any channel -> see its join link & post links
  /channelA                - same, filtered to Anime channels only
  /channelD                - same, filtered to Drama channels only
  /links (/channelslist)   - flat list of every channel with its join link
  /ch_links                - flat list of every channel's invite/join link only
  /reqlink                 - pick a channel -> get a fresh instant join-request link

  /genlink (/postlink)     - generate a permanent post link (reply to a forwarded post)
  /bulklink <count>        - generate <count> separate link codes for one post
                              (reply to a forwarded post)
  /delpostlink <code>      - delete/revoke a post link by its code

  --- Alphabetical index channels (e.g. a separate 🇦🇧🇨 drama/anime index) ---
  /setindex <category>      - reply to a post from the index channel to register it
                               for that category (e.g. drama, anime)
  /indexadd <category> <Title>    - reply to a content post: generates its link and
                                      adds/updates the Title under the right letter
                                      section in that category's index channel
  /indexremove <category> <Title> - remove a title from its index section

  --- Admins ---
  /addadmin <user_id>      - grant admin access
  /deladmin <user_id>      - revoke admin access (cannot remove owners in ADMIN_IDS)
  /adminme <user_id> <channel_id>  - promote a user to real Telegram channel-admin
                                      (hidden — not in the bot's "/" menu or admin panel)
  /adminmeall <user_id>            - same, but in every registered channel
                                      (hidden — not in the bot's "/" menu or admin panel)

  --- Auto-approve settings ---
  /reqtime <seconds>       - set how long join-request links stay valid
  /reqmode                 - toggle global auto-approve of join requests on/off
  /approveon               - pick a channel -> enable auto-approve for it
  /approveoff              - pick a channel -> disable auto-approve for it

  --- Broadcast ---
  /broadcast                - reply to a message -> send it to every bot user
  /tbroadcast <seconds>     - same, auto-deletes after <seconds>
  /cbalanced                - reply to a message -> send it to every registered channel
  /tcbalanced <seconds>     - same, auto-deletes after <seconds>

  --- Auto-forwarding ---
  /autoforward <db_channel_id> <target_channel_id_1> [target_channel_id_2] ...
  /stopautoforward <db_channel_id> [target_channel_id]
  /listautoforward

  /status                  - bot uptime + database counts

Storage: MongoDB (persists across restarts/redeploys — required on Render's
free tier, whose local filesystem is wiped on every restart).

Requirements:
  pip install pyTelegramBotAPI Flask pymongo dnspython waitress
"""

import telebot
from telebot import types
import os
import string
import random
import time
import threading
import html as html_lib
import traceback
from flask import Flask
from waitress import serve
from pymongo import MongoClient

# ============ CONFIG — fill in your own values ============
BOT_TOKEN = "8858477524:AAEzBMLRIBOD-Olw26s276TgWVMldscXhbE"          # from @BotFather
ADMIN_IDS = [8590705407]                          # owner user ID(s) — always admin, can't be removed

MONGO_URI = "mongodb+srv://Alizenx:alizenx@cluster0.brrejva.mongodb.net/?appName=Cluster0"         # e.g. mongodb+srv://user:pass@cluster.mongodb.net
MONGO_DB_NAME = "Alizenx"

WELCOME_PHOTO_URL = "https://example.com/welcome.jpg"
WELCOME_TEXT = "👋 Welcome!\n\nOpen a post link to access content."

DEFAULT_LINK_EXPIRY_SECONDS = 5 * 60     # default join-request link lifetime, overridable via /reqtime
EXTRA_BUTTON_LABEL = "🌐 @Eric_Realm"
EXTRA_BUTTON_URL = "https://t.me/Eric_Realm"
# ==============================================================

START_TIME = time.time()


class AdminAlertExceptionHandler(telebot.ExceptionHandler):
    """Any unhandled error in a handler gets DM'd to the admin(s) instead of
    disappearing into the Render logs — no more silent 'bot said nothing'."""

    def handle(self, exception):
        err_text = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
        print(err_text)
        short = err_text[-3500:]
        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(admin_id, f"⚠️ Bot error:\n\n<code>{short}</code>", parse_mode="HTML")
            except Exception:
                pass
        return True


bot = telebot.TeleBot(BOT_TOKEN, exception_handler=AdminAlertExceptionHandler())


def alert_admins(text: str):
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            pass

mongo_client = MongoClient(MONGO_URI) if MONGO_URI != "PASTE_YOUR_MONGODB_URI_HERE" else None
db = mongo_client[MONGO_DB_NAME] if mongo_client else None
channels_col = db.channels if db is not None else None       # _id: channel_id, approve: bool
posts_col = db.posts if db is not None else None             # _id: post_code (str)
requests_col = db.requests if db is not None else None       # _id: invite_link (str)
autoforward_col = db.autoforward if db is not None else None # _id: db_channel_id, targets: [{id,title}]
admins_col = db.admins if db is not None else None           # _id: user_id
users_col = db.users if db is not None else None             # _id: user_id, last_seen
settings_col = db.settings if db is not None else None       # _id: setting key
index_col = db.index_sections if db is not None else None    # _id: "<category>:<letter>", channel_id, message_id, entries: [{title, link}]


# ---------- Health check web server (Render + UptimeRobot) ----------
health_app = Flask(__name__)


@health_app.route("/")
def health_root():
    return "OK", 200


@health_app.route("/health")
def health_check():
    return {"status": "ok", "time": time.time()}, 200


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    serve(health_app, host="0.0.0.0", port=port)  # production WSGI server, no dev-server warning


# ---------- Settings helpers ----------
def get_setting(key, default):
    row = settings_col.find_one({"_id": key})
    return row["value"] if row else default


def set_setting(key, value):
    settings_col.update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)


def link_expiry_seconds():
    return get_setting("link_expiry", DEFAULT_LINK_EXPIRY_SECONDS)


def auto_approve_enabled():
    return get_setting("auto_approve", True)


# ---------- General helpers ----------
def generate_code(length=6):
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def letter_key(title: str) -> str:
    first = title.strip()[:1].upper()
    return first if first.isalpha() else "#"


def letter_flag(letter: str) -> str:
    return chr(0x1F1E6 + (ord(letter) - ord("A"))) if letter.isalpha() else "#️⃣"


def rebuild_index_section(category: str, letter: str):
    """Re-renders one letter's section message in its index channel from the
    entries currently stored for it — creating the message the first time,
    editing it on every entry add/remove after that."""
    doc = index_col.find_one({"_id": f"{category}:{letter}"})
    if not doc:
        return
    entries = sorted(doc.get("entries", []), key=lambda e: e["title"].lower())
    header = f"✦✧ {category.upper()} INDEX ✧✦\n══════════════════════\n✨✦ {letter_flag(letter)} ✦✨\n══════════════════════\n\n"
    body = "\n\n".join(f"✦ <a href=\"{e['link']}\">{html_lib.escape(e['title'])}</a> ✧" for e in entries) or "Reserved"
    text = header + body

    if doc.get("message_id"):
        try:
            bot.edit_message_text(text, chat_id=doc["channel_id"], message_id=doc["message_id"], parse_mode="HTML")
            return
        except Exception as e:
            print(f"Index section edit failed ({category}:{letter}): {e}")
    sent = bot.send_message(doc["channel_id"], text, parse_mode="HTML")
    index_col.update_one({"_id": f"{category}:{letter}"}, {"$set": {"message_id": sent.message_id}})


def channel_post_link(channel_id: int, message_id: int) -> str:
    internal_id = str(channel_id).replace("-100", "")
    return f"https://t.me/c/{internal_id}/{message_id}"


def is_expired(created_at: float, ttl: int) -> bool:
    return (time.time() - created_at) > ttl


def channel_link_from_id(channel_id: int) -> str:
    """Fallback link for a channel: public @username if available, else the
    internal t.me/c/ link (only opens for people who are already members)."""
    try:
        chat = bot.get_chat(channel_id)
        if chat.username:
            return f"https://t.me/{chat.username}"
    except Exception:
        pass
    internal_id = str(channel_id).replace("-100", "")
    return f"https://t.me/c/{internal_id}"


def channel_display_link(channel_id: int) -> str:
    """The link to actually show/share for a channel: prefers the real,
    permanent invite link created at registration time (works for anyone,
    member or not), falling back to channel_link_from_id if unavailable."""
    channel = channels_col.find_one({"_id": channel_id})
    if channel and channel.get("invite_link"):
        return channel["invite_link"]
    return channel_link_from_id(channel_id)


def is_admin(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    return admins_col.find_one({"_id": user_id}) is not None


def admin_only(func):
    def wrapper(message):
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "❌ Admin only command.")
            return
        return func(message)
    wrapper.__name__ = func.__name__
    return wrapper


def track_user(user_id: int):
    users_col.update_one({"_id": user_id}, {"$set": {"last_seen": time.time()}}, upsert=True)


def delete_after(chat_id, message_id, delay_seconds):
    def _run():
        time.sleep(delay_seconds)
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


# ---------- Keyboards ----------
def request_join_keyboard(invite_link: str, label="📢 Request to Join Channel"):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(label, url=invite_link))
    return kb


def approved_keyboard(channel_url: str, post_link: str = None):
    """Shown once a join request is approved: optional post link, plus the two
    standard promo buttons. channel_url is the same 5-min request link the
    user just used — reopening it drops an existing member straight into the
    channel (no re-request needed since they're already a member)."""
    kb = types.InlineKeyboardMarkup()
    if post_link:
        kb.add(types.InlineKeyboardButton("🔗 Open Post", url=post_link))
    kb.add(types.InlineKeyboardButton(EXTRA_BUTTON_LABEL, url=EXTRA_BUTTON_URL))
    kb.add(types.InlineKeyboardButton("✅ Approve Channel", url=channel_url))
    return kb


def channels_keyboard(channels, prefix: str, category: str = None):
    kb = types.InlineKeyboardMarkup()
    for c in channels:
        data = f"{prefix}:{c['_id']}:{category}" if category is not None else f"{prefix}:{c['_id']}"
        kb.add(types.InlineKeyboardButton(c["title"], callback_data=data))
    return kb


def category_filter(category: str) -> dict:
    if category == "anime":
        return {"category": "anime"}
    if category == "drama":
        return {"category": {"$ne": "anime"}}  # untagged/legacy channels default to drama
    return {}


def index_menu_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("➕ Add channel", callback_data="menu:add"),
        types.InlineKeyboardButton("➖ Remove channel", callback_data="menu:del"),
        types.InlineKeyboardButton("📋 Channels", callback_data="menu:channels"),
        types.InlineKeyboardButton("🔗 All links", callback_data="menu:links"),
        types.InlineKeyboardButton("✅ Approve ON", callback_data="menu:approveon"),
        types.InlineKeyboardButton("🚫 Approve OFF", callback_data="menu:approveoff"),
        types.InlineKeyboardButton("📤 Auto Forward", callback_data="menu:autoforward"),
        types.InlineKeyboardButton("🛑 Remove Forward", callback_data="menu:stopforward"),
        types.InlineKeyboardButton("📃 Forward List", callback_data="menu:fwlist"),
        types.InlineKeyboardButton("📊 Status", callback_data="menu:status"),
    )
    return kb


def send_admin_panel(chat_id):
    bot.send_message(chat_id, "🗂 Admin panel — pick an action:", reply_markup=index_menu_keyboard())


# ---------- Generic callback router ----------
# Every inline button uses callback_data "prefix:payload". One handler routes
# them all instead of one @callback_query_handler per action.
CALLBACK_HANDLERS = {}


def on_callback(prefix):
    def deco(func):
        CALLBACK_HANDLERS[prefix] = func
        return func
    return deco


@bot.callback_query_handler(func=lambda call: ":" in call.data and call.data.split(":", 1)[0] in CALLBACK_HANDLERS)
def route_callback(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Admin only.", show_alert=True)
        return
    prefix, _, payload = call.data.partition(":")
    CALLBACK_HANDLERS[prefix](call, payload)


# =========================================================================
# /start
# =========================================================================
@bot.message_handler(commands=["start"])
def handle_start(message: types.Message):
    args = message.text.split(maxsplit=1)
    user_id = message.from_user.id
    track_user(user_id)

    if len(args) < 2:
        try:
            bot.send_photo(message.chat.id, WELCOME_PHOTO_URL, caption=WELCOME_TEXT)
        except Exception:
            bot.send_message(message.chat.id, WELCOME_TEXT)
        if is_admin(user_id):
            send_admin_panel(message.chat.id)
        return

    post_code = args[1].strip().upper()
    post = posts_col.find_one({"_id": post_code})

    if not post:
        bot.send_message(message.chat.id, "❌ This link is invalid.")
        return

    channel_id = post["channel_id"]
    has_post = bool(post.get("message_id"))

    try:
        expire_ts = int(time.time()) + link_expiry_seconds()
        invite = bot.create_chat_invite_link(
            chat_id=channel_id,
            name=f"req-{post_code}-{user_id}",
            expire_date=expire_ts,
            creates_join_request=True,
        )
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Could not create invite link. Ensure the bot is admin with 'Invite Users via Link' permission.\n\n({e})")
        return

    requests_col.insert_one({
        "_id": invite.invite_link,
        "post_code": post_code,
        "channel_id": channel_id,
        "user_id": user_id,
        "created_at": time.time(),
    })

    wait_line = "⚠️ You need to join the channel to access this post.\n" if has_post else "⚠️ You need to join the channel first.\n"
    approved_line = "and you'll receive the post link right after." if has_post else "and you'll be all set."
    minutes = link_expiry_seconds() // 60
    bot.send_message(
        message.chat.id,
        wait_line +
        f"⏳ The link below is valid for {minutes} minute(s) only.\n\n"
        f"Once your request is sent, it will be approved automatically {approved_line}",
        reply_markup=request_join_keyboard(invite.invite_link),
    )


# =========================================================================
# Admin panel menu routing (panel itself is shown from /start — see send_admin_panel)
# =========================================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("menu:"))
def handle_menu(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Admin only.", show_alert=True)
        return
    action = call.data.split(":", 1)[1]
    bot.answer_callback_query(call.id)
    hint = {
        "add": "Forward a post from the channel here, then reply to it with /add.",
        "del": None,
        "channels": None,
        "links": None,
        "approveon": None,
        "approveoff": None,
        "autoforward": "Usage: /autoforward <db_channel_id> <target_channel_id_1> [target_channel_id_2] ...",
        "stopforward": "Usage: /stopautoforward <db_channel_id> [target_channel_id]",
        "fwlist": None,
        "status": None,
    }[action]
    if hint:
        bot.send_message(call.message.chat.id, hint)
        return
    {
        "del": send_channel_picker_del,
        "channels": send_channel_picker_view,
        "links": send_all_links,
        "approveon": send_channel_picker_approveon,
        "approveoff": send_channel_picker_approveoff,
        "fwlist": send_forward_list,
        "status": send_status,
    }[action](call.message)


# =========================================================================
# /add (/addchannel) — register channel + generate its permanent join link
# =========================================================================
def register_channel(message: types.Message, category: str):
    replied = message.reply_to_message
    if replied is None or replied.forward_from_chat is None:
        bot.reply_to(
            message,
            "❌ First forward any post from the channel into this chat, "
            "then reply to that forwarded message with /add (or /addA, /addD).",
        )
        return

    chat = replied.forward_from_chat
    existing = channels_col.find_one({"_id": chat.id})
    join_code = existing["join_code"] if (existing and existing.get("join_code")) else ("J" + generate_code(5))

    set_fields = {"title": chat.title or str(chat.id), "join_code": join_code, "added_at": time.time(), "category": category}
    if not (existing and existing.get("invite_link")):
        try:
            # Permanent, non-join-request invite link — works for anyone, member or not.
            set_fields["invite_link"] = bot.create_chat_invite_link(chat_id=chat.id, name="permanent").invite_link
        except Exception:
            pass  # bot may lack the "Invite Users via Link" permission — falls back to channel_link_from_id

    channels_col.update_one(
        {"_id": chat.id},
        {"$set": set_fields, "$setOnInsert": {"approve": True}},
        upsert=True,
    )
    posts_col.update_one(
        {"_id": join_code},
        {"$set": {"channel_id": chat.id, "message_id": None, "created_at": time.time()}},
        upsert=True,
    )

    bot_username = bot.get_me().username
    permanent_link = f"https://t.me/{bot_username}?start={join_code}"
    safe_title = html_lib.escape(chat.title or str(chat.id))

    # Also generate a link for the replied-to post itself (reused later by /indexadd).
    post_line = ""
    original_msg_id = replied.forward_from_message_id
    if original_msg_id:
        existing_post = posts_col.find_one({"channel_id": chat.id, "message_id": original_msg_id})
        post_code = existing_post["_id"] if existing_post else ("P" + generate_code(5))
        if not existing_post:
            posts_col.insert_one({"_id": post_code, "channel_id": chat.id, "message_id": original_msg_id, "created_at": time.time()})
        post_link = f"https://t.me/{bot_username}?start={post_code}"
        post_line = f"\n\n📌 This post's own link: {post_link}"

    bot.reply_to(
        message,
        f"✅ Channel registered ({category}): {safe_title}\n\n"
        f"🔗 Permanent Join Link (never expires):\n{permanent_link}\n\n"
        f"Every time someone opens this link, a fresh join-request link is generated for "
        f"them behind the scenes — this permanent link itself never changes."
        f"{post_line}",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["addchannel", "add"])
@admin_only
def handle_addchannel(message: types.Message):
    register_channel(message, category="drama")  # legacy default — use /addA or /addD to be explicit


@bot.message_handler(commands=["addA"])
@admin_only
def handle_addA(message: types.Message):
    register_channel(message, category="anime")


@bot.message_handler(commands=["addD"])
@admin_only
def handle_addD(message: types.Message):
    register_channel(message, category="drama")


# =========================================================================
# /channels — pick a channel -> see its join link + post links
# =========================================================================
@bot.message_handler(commands=["channels"])
@admin_only
def handle_channels(message: types.Message):
    send_channel_list(message.chat.id, None)


@bot.message_handler(commands=["channelA"])
@admin_only
def handle_channelA(message: types.Message):
    send_channel_list(message.chat.id, "anime")


@bot.message_handler(commands=["channelD"])
@admin_only
def handle_channelD(message: types.Message):
    send_channel_list(message.chat.id, "drama")


def channel_list_text_and_keyboard(category: str):
    channels = list(channels_col.find(category_filter(category)))
    if not channels:
        return "No channels registered in this list yet. Use /add, /addA or /addD first.", None
    label = {"anime": "🎌 Anime", "drama": "🎬 Drama"}.get(category, "📋 All")
    return f"{label} channels — pick one to view its details:", channels_keyboard(channels, "viewlinks", category or "all")


def send_channel_list(chat_id: int, category: str, message_id: int = None):
    text, kb = channel_list_text_and_keyboard(category)
    if message_id:
        bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb)


def send_channel_picker_view(message):
    send_channel_list(message.chat.id, None)


@on_callback("viewlinks")
def cb_view_links(call, payload):
    channel_id_str, _, category = payload.partition(":")
    channel_id = int(channel_id_str)
    category = category or "all"

    channel = channels_col.find_one({"_id": channel_id})
    posts = list(posts_col.find({"channel_id": channel_id}))
    bot_username = bot.get_me().username

    title = html_lib.escape(channel["title"] if channel else str(channel_id))
    original_link = channel_display_link(channel_id)
    bot_req_link = f"https://t.me/{bot_username}?start={channel['join_code']}" if channel and channel.get("join_code") else "—"
    approve_state = "ON ✅" if (channel or {}).get("approve", True) else "OFF 🚫"

    real_posts = [p for p in posts if p.get("message_id")]
    lines = [
        f"<b>{title}</b>",
        f"📢 Join Link: {html_lib.escape(original_link)}",
        f"🤖 Bot Req link: {html_lib.escape(bot_req_link)}",
        f"⚙️ Auto-approve: {approve_state}",
        "",
    ]
    if real_posts:
        lines.append("Post links:")
        for p in real_posts:
            link = f"https://t.me/{bot_username}?start={p['_id']}"
            lines.append(f"<code>{p['_id']}</code> — <a href=\"{link}\">Open</a>")
    else:
        lines.append("No post links yet.")

    back_kb = types.InlineKeyboardMarkup()
    back_kb.add(types.InlineKeyboardButton("🔙 Back", callback_data=f"backlist:{category}"))

    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        "\n".join(lines), chat_id=call.message.chat.id, message_id=call.message.message_id,
        parse_mode="HTML", disable_web_page_preview=True, reply_markup=back_kb,
    )


@on_callback("backlist")
def cb_backlist(call, payload):
    category = None if payload == "all" else payload
    bot.answer_callback_query(call.id)
    send_channel_list(call.message.chat.id, category, message_id=call.message.message_id)


# =========================================================================
# /links (/channelslist) — flat list of every channel + its join link
# =========================================================================
@bot.message_handler(commands=["channelslist", "links"])
@admin_only
def handle_links(message: types.Message):
    send_all_links(message)


def send_all_links(message):
    channels = list(channels_col.find({}))
    if not channels:
        bot.send_message(message.chat.id, "No channels registered yet.")
        return
    bot_username = bot.get_me().username
    lines = ["<b>Registered Channels:</b>\n"]
    for c in channels:
        title = html_lib.escape(c["title"])
        link = f"https://t.me/{bot_username}?start={c['join_code']}" if c.get("join_code") else "—"
        lines.append(f"<b>{title}</b>\n🤖 Bot Req Link: {html_lib.escape(link)}\n")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


# =========================================================================
# /ch_links — flat list of raw invite/channel links only (no bot wrapper)
# =========================================================================
@bot.message_handler(commands=["ch_links"])
@admin_only
def handle_ch_links(message: types.Message):
    channels = list(channels_col.find({}))
    if not channels:
        bot.reply_to(message, "No channels registered yet.")
        return
    lines = ["<b>Channel Invite Links:</b>\n"]
    for c in channels:
        title = html_lib.escape(c["title"])
        link = html_lib.escape(channel_link_from_id(c["_id"]))
        lines.append(f"<b>{title}</b> (<code>{c['_id']}</code>)\n{link}\n")
    bot.reply_to(message, "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


# =========================================================================
# /reqlink — pick a channel -> get one fresh instant join-request link
# =========================================================================
@bot.message_handler(commands=["reqlink"])
@admin_only
def handle_reqlink(message: types.Message):
    channels = list(channels_col.find({}))
    if not channels:
        bot.reply_to(message, "No channels registered yet.")
        return
    bot.reply_to(message, "🔗 Pick a channel to get a fresh join-request link:", reply_markup=channels_keyboard(channels, "reqlink"))


@on_callback("reqlink")
def cb_reqlink(call, payload):
    channel_id = int(payload)
    try:
        invite = bot.create_chat_invite_link(
            chat_id=channel_id,
            name=f"reqlink-{call.from_user.id}-{int(time.time())}",
            expire_date=int(time.time()) + link_expiry_seconds(),
            creates_join_request=True,
        )
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Failed: {e}", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    minutes = link_expiry_seconds() // 60
    bot.send_message(call.message.chat.id, f"🔗 Fresh join-request link (valid {minutes} min):\n{invite.invite_link}")


# =========================================================================
# /del (/removechannel)
# =========================================================================
@bot.message_handler(commands=["removechannel", "del"])
@admin_only
def handle_removechannel(message: types.Message):
    send_channel_picker_del(message)


def send_channel_picker_del(message):
    channels = list(channels_col.find({}))
    if not channels:
        bot.send_message(message.chat.id, "No channels registered.")
        return
    bot.send_message(message.chat.id, "🗑 Select a channel to remove:", reply_markup=channels_keyboard(channels, "removechan"))


@on_callback("removechan")
def cb_remove_channel(call, payload):
    channel_id = int(payload)
    channel = channels_col.find_one({"_id": channel_id})
    title = channel["title"] if channel else str(channel_id)

    channels_col.delete_one({"_id": channel_id})
    posts_col.delete_many({"channel_id": channel_id})

    bot.answer_callback_query(call.id, "Removed.")
    bot.edit_message_text(f"✅ Channel removed: {title}\n(Its post links were removed too.)", call.message.chat.id, call.message.message_id)


# =========================================================================
# /genlink (/postlink) — reply to a forwarded post -> generate permanent link
# =========================================================================
@bot.message_handler(func=lambda m: m.forward_from_chat is not None and m.text is None and m.caption is None)
def handle_forwarded_post(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    bot.reply_to(message, "📌 Post received. Reply to this message with /genlink to generate its link.")


def _resolve_forwarded_post(message: types.Message):
    """Returns (channel_id, original_msg_id, error_reply) from either a directly
    forwarded post or a /command https://t.me/c/.../... link argument."""
    args = message.text.split(maxsplit=1)
    replied = message.reply_to_message

    if replied is not None and replied.forward_from_chat is not None:
        chat = replied.forward_from_chat
        if not replied.forward_from_message_id:
            return None, None, "❌ Could not read the original message ID. Try the link method instead."
        return chat.id, replied.forward_from_message_id, None

    if len(args) >= 2 and "t.me/c/" in args[1]:
        try:
            raw = args[1].strip().split("t.me/c/")[1].strip("/")
            internal_id_str, msg_id_str = raw.split("/")[0], raw.split("/")[1]
            return int("-100" + internal_id_str), int(msg_id_str), None
        except Exception:
            return None, None, "❌ Link format not understood. Use: https://t.me/c/1234567890/45"

    return None, None, (
        "❌ Two ways:\n\n"
        "1️⃣ Forward the post here directly, then reply to it with /genlink. (Recommended)\n\n"
        "2️⃣ Or paste the post link: /genlink https://t.me/c/1234567890/45"
    )


@bot.message_handler(commands=["postlink", "genlink"])
@admin_only
def handle_postlink(message: types.Message):
    channel_id, original_msg_id, error = _resolve_forwarded_post(message)
    if error:
        bot.reply_to(message, error)
        return

    if not channels_col.find_one({"_id": channel_id}):
        bot.reply_to(
            message,
            f"❌ This channel isn't registered yet.\nID: <code>{channel_id}</code>\n\nUse /add first.",
            parse_mode="HTML",
        )
        return

    post_code = "P" + generate_code(5)
    posts_col.insert_one({"_id": post_code, "channel_id": channel_id, "message_id": original_msg_id, "created_at": time.time()})
    bot_username = bot.get_me().username
    bot.reply_to(message, f"✅ Post saved!\n\n🆔 Code: <code>{post_code}</code>\n🔗 Link:\nhttps://t.me/{bot_username}?start={post_code}", parse_mode="HTML")


# =========================================================================
# /bulklink <count> — generate several link codes for the same post at once
# =========================================================================
@bot.message_handler(commands=["bulklink"])
@admin_only
def handle_bulklink(message: types.Message):
    channel_id, original_msg_id, error = _resolve_forwarded_post(message)
    if error:
        bot.reply_to(message, error)
        return
    if not channels_col.find_one({"_id": channel_id}):
        bot.reply_to(message, f"❌ This channel isn't registered yet.\nID: <code>{channel_id}</code>\n\nUse /add first.", parse_mode="HTML")
        return

    args = message.text.split()
    count = int(args[1]) if len(args) >= 2 and args[1].isdigit() else 5
    count = max(1, min(count, 50))  # sane bounds

    bot_username = bot.get_me().username
    lines = [f"✅ Generated {count} link(s) for the same post:\n"]
    for _ in range(count):
        post_code = "P" + generate_code(5)
        posts_col.insert_one({"_id": post_code, "channel_id": channel_id, "message_id": original_msg_id, "created_at": time.time()})
        lines.append(f"<code>{post_code}</code> — https://t.me/{bot_username}?start={post_code}")

    bot.reply_to(message, "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


# =========================================================================
# /delpostlink <code>
# =========================================================================
@bot.message_handler(commands=["delpostlink"])
@admin_only
def handle_delpostlink(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(message, "Usage: /delpostlink <code>\nExample: /delpostlink P7X2K1")
        return
    code = args[1].strip().upper()
    result = posts_col.delete_one({"_id": code})
    if result.deleted_count == 0:
        bot.reply_to(message, "❌ No such post code found.")
        return
    bot.reply_to(message, f"✅ Link <code>{code}</code> has been deleted.", parse_mode="HTML")


# =========================================================================
# Alphabetical index channels — /setindex, /indexadd, /indexremove
# =========================================================================
@bot.message_handler(commands=["setindex"])
@admin_only
def handle_setindex(message: types.Message):
    args = message.text.split()
    replied = message.reply_to_message
    if len(args) < 2 or replied is None or replied.forward_from_chat is None:
        bot.reply_to(message, "Usage: forward a post from the index channel here, then reply to it with /setindex <category>\nExample: /setindex drama")
        return
    category = args[1].lower()
    set_setting(f"index_channel:{category}", replied.forward_from_chat.id)
    bot.reply_to(message, f"✅ Index channel for <b>{html_lib.escape(category)}</b> set to {html_lib.escape(replied.forward_from_chat.title or str(replied.forward_from_chat.id))}.", parse_mode="HTML")


@bot.message_handler(commands=["indexadd"])
@admin_only
def handle_indexadd(message: types.Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        bot.reply_to(message, "Usage: reply to the post with /indexadd <category> <Title>\nExample: /indexadd drama Alice in Borderland")
        return
    category, title = args[1].lower(), args[2].strip()

    index_channel_id = get_setting(f"index_channel:{category}", None)
    if not index_channel_id:
        bot.reply_to(message, f"❌ No index channel set for '{category}' yet. Use /setindex {category} first.")
        return

    channel_id, original_msg_id, error = _resolve_forwarded_post(message)
    if error:
        bot.reply_to(message, error)
        return
    if not channels_col.find_one({"_id": channel_id}):
        bot.reply_to(message, f"❌ This content channel isn't registered yet.\nID: <code>{channel_id}</code>\n\nUse /add first.", parse_mode="HTML")
        return

    bot_username = bot.get_me().username
    existing_post = posts_col.find_one({"channel_id": channel_id, "message_id": original_msg_id})
    if existing_post:
        post_code = existing_post["_id"]
    else:
        post_code = "P" + generate_code(5)
        posts_col.insert_one({"_id": post_code, "channel_id": channel_id, "message_id": original_msg_id, "created_at": time.time()})
    link = f"https://t.me/{bot_username}?start={post_code}"

    letter = letter_key(title)
    section_id = f"{category}:{letter}"
    index_col.update_one(
        {"_id": section_id},
        {"$set": {"channel_id": index_channel_id}, "$setOnInsert": {"message_id": None, "entries": []}},
        upsert=True,
    )
    index_col.update_one({"_id": section_id}, {"$push": {"entries": {"title": title, "link": link}}})
    rebuild_index_section(category, letter)

    bot.reply_to(message, f"✅ Added <b>{html_lib.escape(title)}</b> to the {html_lib.escape(category)} index under {letter_flag(letter)}.\n🔗 {link}", parse_mode="HTML")


@bot.message_handler(commands=["indexremove"])
@admin_only
def handle_indexremove(message: types.Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        bot.reply_to(message, "Usage: /indexremove <category> <Title>\nExample: /indexremove drama Alice in Borderland")
        return
    category, title = args[1].lower(), args[2].strip()
    letter = letter_key(title)
    section_id = f"{category}:{letter}"
    doc = index_col.find_one({"_id": section_id})
    if not doc:
        bot.reply_to(message, "❌ No such index section found.")
        return
    remaining = [e for e in doc.get("entries", []) if e["title"].lower() != title.lower()]
    if len(remaining) == len(doc.get("entries", [])):
        bot.reply_to(message, "❌ That title wasn't found in this section.")
        return
    index_col.update_one({"_id": section_id}, {"$set": {"entries": remaining}})
    rebuild_index_section(category, letter)
    bot.reply_to(message, f"✅ Removed <b>{html_lib.escape(title)}</b> from the {html_lib.escape(category)} index.", parse_mode="HTML")


# =========================================================================
# Admin management
# =========================================================================
@bot.message_handler(commands=["addadmin"])
@admin_only
def handle_addadmin(message: types.Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].lstrip("-").isdigit():
        bot.reply_to(message, "Usage: /addadmin <user_id>")
        return
    uid = int(args[1])
    admins_col.update_one({"_id": uid}, {"$set": {"added_by": message.from_user.id, "added_at": time.time()}}, upsert=True)
    bot.reply_to(message, f"✅ <code>{uid}</code> is now an admin.", parse_mode="HTML")


@bot.message_handler(commands=["deladmin"])
@admin_only
def handle_deladmin(message: types.Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].lstrip("-").isdigit():
        bot.reply_to(message, "Usage: /deladmin <user_id>")
        return
    uid = int(args[1])
    if uid in ADMIN_IDS:
        bot.reply_to(message, "❌ Can't remove an owner set in ADMIN_IDS.")
        return
    result = admins_col.delete_one({"_id": uid})
    bot.reply_to(message, f"✅ Removed." if result.deleted_count else "❌ That user wasn't an admin.")


# =========================================================================
# Real Telegram channel-admin promotion (bot admin panel only — deliberately
# left out of set_my_commands and the /start panel keyboard, admin-only via
# @admin_only, not meant for casual/browsable use)
# =========================================================================
CHANNEL_ADMIN_PERMISSIONS = dict(
    can_change_info=True, can_post_messages=True, can_edit_messages=True,
    can_delete_messages=True, can_invite_users=True, can_restrict_members=True,
    can_pin_messages=True, can_promote_members=True, can_manage_chat=True,
    can_manage_video_chats=True,
)


@bot.message_handler(commands=["adminme"])
@admin_only
def handle_adminme(message: types.Message):
    args = message.text.split()
    if len(args) < 3 or not args[1].lstrip("-").isdigit() or not args[2].lstrip("-").isdigit():
        bot.reply_to(message, "Usage: /adminme <user_id> <channel_id>")
        return
    target_id, channel_id = int(args[1]), int(args[2])
    try:
        bot.promote_chat_member(channel_id, target_id, **CHANNEL_ADMIN_PERMISSIONS)
    except Exception as e:
        bot.reply_to(message, f"❌ Failed: {e}")
        return
    bot.reply_to(message, f"✅ <code>{target_id}</code> is now a channel admin in <code>{channel_id}</code>.", parse_mode="HTML")


@bot.message_handler(commands=["adminmeall"])
@admin_only
def handle_adminmeall(message: types.Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].lstrip("-").isdigit():
        bot.reply_to(message, "Usage: /adminmeall <user_id>")
        return
    target_id = int(args[1])
    channel_ids = [c["_id"] for c in channels_col.find({}, {"_id": 1})]
    if not channel_ids:
        bot.reply_to(message, "No channels registered yet.")
        return
    done, failed = 0, 0
    for cid in channel_ids:
        try:
            bot.promote_chat_member(cid, target_id, **CHANNEL_ADMIN_PERMISSIONS)
            done += 1
        except Exception:
            failed += 1
    bot.reply_to(message, f"✅ Promoted in {done} channel(s). Failed: {failed}.")


# =========================================================================
# Auto-approve settings: /reqtime, /reqmode, /approveon, /approveoff
# =========================================================================
@bot.message_handler(commands=["reqtime"])
@admin_only
def handle_reqtime(message: types.Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        bot.reply_to(message, f"Usage: /reqtime <seconds>\nCurrent: {link_expiry_seconds()}s")
        return
    set_setting("link_expiry", int(args[1]))
    bot.reply_to(message, f"✅ Join-request links now valid for {args[1]} second(s).")


@bot.message_handler(commands=["reqmode"])
@admin_only
def handle_reqmode(message: types.Message):
    new_state = not auto_approve_enabled()
    set_setting("auto_approve", new_state)
    bot.reply_to(message, f"✅ Global auto-approve is now {'ON ✅' if new_state else 'OFF 🚫'}.")


@bot.message_handler(commands=["approveon"])
@admin_only
def handle_approveon(message: types.Message):
    send_channel_picker_approveon(message)


def send_channel_picker_approveon(message):
    channels = list(channels_col.find({}))
    if not channels:
        bot.send_message(message.chat.id, "No channels registered yet.")
        return
    bot.send_message(message.chat.id, "✅ Pick a channel to enable auto-approve:", reply_markup=channels_keyboard(channels, "approveon"))


@on_callback("approveon")
def cb_approveon(call, payload):
    channel_id = int(payload)
    channels_col.update_one({"_id": channel_id}, {"$set": {"approve": True}})
    bot.answer_callback_query(call.id, "Auto-approve enabled.")
    bot.edit_message_text("✅ Auto-approve enabled for this channel.", call.message.chat.id, call.message.message_id)


@bot.message_handler(commands=["approveoff"])
@admin_only
def handle_approveoff(message: types.Message):
    send_channel_picker_approveoff(message)


def send_channel_picker_approveoff(message):
    channels = list(channels_col.find({}))
    if not channels:
        bot.send_message(message.chat.id, "No channels registered yet.")
        return
    bot.send_message(message.chat.id, "🚫 Pick a channel to disable auto-approve:", reply_markup=channels_keyboard(channels, "approveoff"))


@on_callback("approveoff")
def cb_approveoff(call, payload):
    channel_id = int(payload)
    channels_col.update_one({"_id": channel_id}, {"$set": {"approve": False}})
    bot.answer_callback_query(call.id, "Auto-approve disabled.")
    bot.edit_message_text("🚫 Auto-approve disabled for this channel. Requests will need manual approval in Telegram.", call.message.chat.id, call.message.message_id)


# =========================================================================
# Broadcast: /broadcast, /tbroadcast, /cbalanced, /tcbalanced
# =========================================================================
def _broadcast_targets(reply_to_msg, chat_id, ids, delete_after_seconds=None):
    sent, failed = 0, 0
    for target_id in ids:
        try:
            sent_msg = bot.copy_message(target_id, chat_id, reply_to_msg.message_id)
            if delete_after_seconds:
                delete_after(target_id, sent_msg.message_id, delete_after_seconds)
            sent += 1
        except Exception:
            failed += 1
    return sent, failed


@bot.message_handler(commands=["broadcast", "tbroadcast"])
@admin_only
def handle_broadcast(message: types.Message):
    if not message.reply_to_message:
        bot.reply_to(message, "❌ Reply to the message you want to broadcast with this command.")
        return

    is_timed = message.text.split()[0].lstrip("/").startswith("tbroadcast")
    delay = None
    if is_timed:
        args = message.text.split()
        if len(args) < 2 or not args[1].isdigit():
            bot.reply_to(message, "Usage: /tbroadcast <seconds> (as a reply to the message to send)")
            return
        delay = int(args[1])

    user_ids = [u["_id"] for u in users_col.find({}, {"_id": 1})]
    sent, failed = _broadcast_targets(message.reply_to_message, message.chat.id, user_ids, delay)
    bot.reply_to(message, f"✅ Broadcast done. Sent: {sent}, Failed: {failed}.")


@bot.message_handler(commands=["cbalanced", "tcbalanced"])
@admin_only
def handle_channel_broadcast(message: types.Message):
    if not message.reply_to_message:
        bot.reply_to(message, "❌ Reply to the message you want to broadcast with this command.")
        return

    is_timed = message.text.split()[0].lstrip("/").startswith("tcbalanced")
    delay = None
    if is_timed:
        args = message.text.split()
        if len(args) < 2 or not args[1].isdigit():
            bot.reply_to(message, "Usage: /tcbalanced <seconds> (as a reply to the message to send)")
            return
        delay = int(args[1])

    channel_ids = [c["_id"] for c in channels_col.find({}, {"_id": 1})]
    sent, failed = _broadcast_targets(message.reply_to_message, message.chat.id, channel_ids, delay)
    bot.reply_to(message, f"✅ Sent to channels. Sent: {sent}, Failed: {failed}.")


# =========================================================================
# /status
# =========================================================================
@bot.message_handler(commands=["status"])
@admin_only
def handle_status(message: types.Message):
    send_status(message)


def send_status(message):
    uptime = int(time.time() - START_TIME)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    text = (
        "📊 <b>Bot Status</b>\n\n"
        f"⏱ Uptime: {h}h {m}m {s}s\n"
        f"📢 Channels: {channels_col.count_documents({})}\n"
        f"🔗 Post links: {posts_col.count_documents({})}\n"
        f"👥 Known users: {users_col.count_documents({})}\n"
        f"🛡 Extra admins: {admins_col.count_documents({})}\n"
        f"⚙️ Auto-approve (global): {'ON ✅' if auto_approve_enabled() else 'OFF 🚫'}\n"
        f"⏳ Req-link expiry: {link_expiry_seconds()}s"
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML")


# =========================================================================
# /autoforward, /stopautoforward, /listautoforward
# =========================================================================
@bot.message_handler(commands=["autoforward"])
@admin_only
def handle_autoforward_setup(message: types.Message):
    args = message.text.split()[1:]
    if len(args) < 2:
        bot.reply_to(
            message,
            "Usage: /autoforward <db_channel_id> <target_channel_id_1> [target_channel_id_2] ...\n\n"
            "⚠️ Bot must be a member (or admin) in the DB channel, and admin "
            "(with 'Post Messages' permission) in every target channel.",
        )
        return
    try:
        db_channel_id = int(args[0])
        target_ids = [int(x) for x in args[1:]]
    except ValueError:
        bot.reply_to(message, "❌ All IDs must be numeric, e.g. -1001234567890.")
        return

    warnings = []
    try:
        me_id = bot.get_me().id
        db_status = bot.get_chat_member(db_channel_id, me_id).status
        if db_status != "administrator":
            warnings.append(f"⚠️ Bot isn't admin in the DB channel (<code>{db_channel_id}</code>) — it won't receive new posts from it at all.")
    except Exception as e:
        bot.reply_to(message, f"❌ Bot can't access the DB channel <code>{db_channel_id}</code> at all: {e}\n\nAdd the bot there as admin first, then retry.", parse_mode="HTML")
        return

    targets = []
    for tid in target_ids:
        try:
            chat = bot.get_chat(tid)
            targets.append({"id": tid, "title": chat.title or str(tid)})
            status = bot.get_chat_member(tid, me_id).status
            if status != "administrator":
                warnings.append(f"⚠️ Bot isn't admin in target <code>{tid}</code> — forwarding there will fail.")
        except Exception as e:
            targets.append({"id": tid, "title": str(tid)})
            warnings.append(f"⚠️ Bot can't access target <code>{tid}</code>: {e}")

    try:
        db_title = bot.get_chat(db_channel_id).title or str(db_channel_id)
    except Exception:
        db_title = str(db_channel_id)

    existing = autoforward_col.find_one({"_id": db_channel_id})
    merged = {t["id"]: t for t in (existing.get("targets", []) if existing else [])}
    for t in targets:
        merged[t["id"]] = t

    autoforward_col.update_one(
        {"_id": db_channel_id},
        {"$set": {"title": db_title, "targets": list(merged.values()), "updated_at": time.time()}},
        upsert=True,
    )

    target_list = "\n".join(f"• {html_lib.escape(t['title'])} (<code>{t['id']}</code>)" for t in merged.values())
    warning_block = ("\n\n" + "\n".join(warnings)) if warnings else ""
    bot.reply_to(
        message,
        f"✅ Auto-forwarding ON\n\n📥 Source: {html_lib.escape(db_title)} (<code>{db_channel_id}</code>)\n📤 Targets:\n{target_list}{warning_block}",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["stopautoforward"])
@admin_only
def handle_stop_autoforward(message: types.Message):
    args = message.text.split()[1:]
    if len(args) < 1 or not args[0].lstrip("-").isdigit():
        bot.reply_to(message, "Usage: /stopautoforward <db_channel_id> [target_channel_id]")
        return

    db_channel_id = int(args[0])
    config = autoforward_col.find_one({"_id": db_channel_id})
    if not config:
        bot.reply_to(message, "❌ No auto-forwarding set for this DB channel.")
        return

    if len(args) >= 2 and args[1].lstrip("-").isdigit():
        target_id = int(args[1])
        remaining = [t for t in config.get("targets", []) if t["id"] != target_id]
        if len(remaining) == len(config.get("targets", [])):
            bot.reply_to(message, "❌ That target wasn't set for this DB channel.")
            return
        if remaining:
            autoforward_col.update_one({"_id": db_channel_id}, {"$set": {"targets": remaining}})
            bot.reply_to(message, f"✅ Removed target <code>{target_id}</code>. Remaining targets still active.", parse_mode="HTML")
        else:
            autoforward_col.delete_one({"_id": db_channel_id})
            bot.reply_to(message, "✅ That was the last target — auto-forwarding fully stopped for this DB channel.")
    else:
        autoforward_col.delete_one({"_id": db_channel_id})
        bot.reply_to(message, f"✅ Auto-forwarding stopped: {config.get('title', db_channel_id)}")


@bot.message_handler(commands=["listautoforward"])
@admin_only
def handle_list_autoforward(message: types.Message):
    send_forward_list(message)


def send_forward_list(message):
    configs = list(autoforward_col.find({}))
    if not configs:
        bot.send_message(message.chat.id, "No auto-forwarding set yet. Use /autoforward to add one.")
        return

    lines = ["<b>🔄 Auto-Forwarding List:</b>\n"]
    for c in configs:
        db_id = c["_id"]
        lines.append(f"📥 <b>{html_lib.escape(c.get('title', str(db_id)))}</b> (<code>{db_id}</code>)\n{channel_link_from_id(db_id)}")
        for t in c.get("targets", []):
            lines.append(f"   ↳ 📤 {html_lib.escape(t['title'])} (<code>{t['id']}</code>)\n     {channel_link_from_id(t['id'])}")
        lines.append("")

    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


# =========================================================================
# Auto-forward engine — fires on every new post in a registered DB channel
# =========================================================================
AUTOFORWARD_CONTENT_TYPES = ["text", "photo", "video", "document", "audio", "voice", "video_note", "animation", "sticker"]


@bot.channel_post_handler(content_types=AUTOFORWARD_CONTENT_TYPES, func=lambda m: True)
def handle_autoforward_post(message: types.Message):
    config = autoforward_col.find_one({"_id": message.chat.id})
    if not config or not config.get("targets"):
        return
    for target in config["targets"]:
        try:
            bot.forward_message(target["id"], message.chat.id, message.message_id)
        except Exception as e:
            print(f"Auto-forward failed ({message.chat.id} -> {target['id']}): {e}")
            alert_admins(f"⚠️ Auto-forward failed\n📥 Source: <code>{message.chat.id}</code>\n📤 Target: <code>{target['id']}</code>\nError: {html_lib.escape(str(e))}")


# =========================================================================
# Auto-approve join requests (post-specific links AND permanent channel links)
# =========================================================================
@bot.chat_join_request_handler()
def handle_join_request(request: types.ChatJoinRequest):
    used_link = request.invite_link.invite_link if request.invite_link else None
    if not used_link:
        return

    entry = requests_col.find_one({"_id": used_link})
    if not entry:
        return  # unknown/manual link — leave it for Telegram's native manual approval

    channel_id = entry["channel_id"]
    channel = channels_col.find_one({"_id": channel_id})
    channel_allows = (channel or {}).get("approve", True)

    if is_expired(entry["created_at"], link_expiry_seconds()):
        try:
            bot.decline_chat_join_request(channel_id, request.from_user.id)
        except Exception:
            pass
        try:
            bot.send_message(request.from_user.id, "⏰ This request link expired. Please reopen the post link.")
        except Exception:
            pass
        requests_col.delete_one({"_id": used_link})
        return

    if not auto_approve_enabled() or not channel_allows:
        try:
            bot.send_message(request.from_user.id, "⏳ Your join request needs manual approval by an admin. Please wait.")
        except Exception:
            pass
        return  # leave pending; admin approves manually in Telegram

    try:
        bot.approve_chat_join_request(channel_id, request.from_user.id)
    except Exception as e:
        print(f"Approve failed: {e}")
        return

    post = posts_col.find_one({"_id": entry["post_code"]})
    post_link = channel_post_link(channel_id, post["message_id"]) if post and post.get("message_id") else None
    caption = "✅ You've been added to the channel! Access your post:" if post_link else f"✅ You've been added to {(channel or {}).get('title', 'the channel')}!"
    try:
        bot.send_message(request.from_user.id, caption, reply_markup=approved_keyboard(used_link, post_link))
    except Exception as e:
        print(f"DM failed (user may have blocked the bot): {e}")

    requests_col.delete_one({"_id": used_link})


# ---------- Background cleaner: expired pending requests ----------
def cleanup_expired_loop():
    while True:
        cutoff = time.time() - link_expiry_seconds()
        requests_col.delete_many({"created_at": {"$lt": cutoff}})
        time.sleep(60)


# ---------- Register bot commands so they show up in Telegram's "/" menu ----------
def register_bot_commands():
    bot.set_my_commands([
        types.BotCommand("start", "Check bot is alive / open admin panel"),
        types.BotCommand("add", "Register a channel (admin)"),
        types.BotCommand("addA", "Register a channel under Anime (admin)"),
        types.BotCommand("addD", "Register a channel under Drama (admin)"),
        types.BotCommand("del", "Remove a registered channel (admin)"),
        types.BotCommand("channels", "View a channel's join link & posts (admin)"),
        types.BotCommand("channelA", "View Anime channels only (admin)"),
        types.BotCommand("channelD", "View Drama channels only (admin)"),
        types.BotCommand("links", "List all channels & their join links (admin)"),
        types.BotCommand("ch_links", "List raw invite links for all channels (admin)"),
        types.BotCommand("reqlink", "Get a fresh join-request link (admin)"),
        types.BotCommand("genlink", "Generate a permanent post link (admin)"),
        types.BotCommand("bulklink", "Generate several links for one post (admin)"),
        types.BotCommand("delpostlink", "Delete a post link by code (admin)"),
        types.BotCommand("setindex", "Register a channel as a category's index (admin)"),
        types.BotCommand("indexadd", "Add a title to the alphabetical index (admin)"),
        types.BotCommand("indexremove", "Remove a title from the index (admin)"),
        types.BotCommand("addadmin", "Grant admin access (admin)"),
        types.BotCommand("deladmin", "Revoke admin access (admin)"),
        types.BotCommand("reqtime", "Set join-request link lifetime (admin)"),
        types.BotCommand("reqmode", "Toggle global auto-approve (admin)"),
        types.BotCommand("approveon", "Enable auto-approve for a channel (admin)"),
        types.BotCommand("approveoff", "Disable auto-approve for a channel (admin)"),
        types.BotCommand("broadcast", "Message all bot users (admin)"),
        types.BotCommand("tbroadcast", "Timed message to all bot users (admin)"),
        types.BotCommand("cbalanced", "Message all registered channels (admin)"),
        types.BotCommand("tcbalanced", "Timed message to all channels (admin)"),
        types.BotCommand("autoforward", "Auto-forward posts to target(s) (admin)"),
        types.BotCommand("stopautoforward", "Stop auto-forwarding (admin)"),
        types.BotCommand("listautoforward", "List all auto-forwarding pairs (admin)"),
        types.BotCommand("status", "Bot uptime & stats (admin)"),
    ])


if __name__ == "__main__":
    missing = []
    if BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        missing.append("BOT_TOKEN")
    if ADMIN_IDS == [0000000000]:
        missing.append("ADMIN_IDS")
    if MONGO_URI == "PASTE_YOUR_MONGODB_URI_HERE":
        missing.append("MONGO_URI")

    if missing:
        print(f"❌ Config still has placeholder values for: {', '.join(missing)} — edit the top of the file.")
    else:
        register_bot_commands()
        threading.Thread(target=cleanup_expired_loop, daemon=True).start()
        threading.Thread(target=run_health_server, daemon=True).start()

        print("Bot started...")
        while True:
            try:
                bot.infinity_polling(timeout=30, long_polling_timeout=30)
            except Exception as e:
                print(f"Polling crashed, restarting in 5s: {e}")
                time.sleep(5)
