# telegram_bot.py — PANAHANNET Telegram bot.
#
# A single-bot control plane for the panel:
#   • Admin menu (set from the panel or /addadmin): stats, users, orders,
#     broadcasts, panel link.
#   • User menu + built-in shop: packages, card-to-card checkout, receipt
#     approval by admins, and automatic account provisioning/renewal.
#   • Free trial (optional, once per Telegram account).
#
# The bot runs on long-polling so it works on plain VPS / Railway without a
# public webhook. It reads/writes its config in main.SETTINGS["telegram"], which
# is persisted to the panel state file.
import asyncio
import html
import logging

import httpx

logger = logging.getLogger("PANAHANNET-Bot")

TG_API = "https://api.telegram.org/bot{token}/{method}"

_POLL_TASK: asyncio.Task | None = None
_LAST_UPDATE_ID = 0

DEFAULT_PACKAGES = [
    {"id": "p1", "title": "۱ ماهه · ۳۰ گیگ", "days": 30, "gb": 30, "price": 100000},
    {"id": "p2", "title": "۱ ماهه · ۶۰ گیگ", "days": 30, "gb": 60, "price": 160000},
    {"id": "p3", "title": "۲ ماهه · ۱۰۰ گیگ", "days": 60, "gb": 100, "price": 260000},
    {"id": "p4", "title": "۳ ماهه · ۲۰۰ گیگ", "days": 90, "gb": 200, "price": 400000},
]


def _M():
    """Import the running main module lazily (avoids circular imports)."""
    import main  # type: ignore
    return main


def _parse_ids(raw) -> list:
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = str(raw or "").replace(";", ",").split(",")
    out = []
    for it in items:
        s = str(it).strip()
        if not s:
            continue
        if s.lstrip("-").isdigit():
            n = int(s)
            if n not in out:
                out.append(n)
    return out


def state() -> dict:
    """Return (and lazily initialise) the bot state stored in panel SETTINGS."""
    M = _M()
    tg = M.SETTINGS.setdefault("telegram", {})
    if not isinstance(tg, dict):
        tg = {}
        M.SETTINGS["telegram"] = tg
    tg.setdefault("enabled", True)
    tg.setdefault("bot_token", M.brand("bot_token"))
    tg.setdefault("admin_ids", _parse_ids(M.brand("admin_ids")))
    shop = tg.setdefault("shop", {})
    shop.setdefault("enabled", True)
    shop.setdefault("card_number", "")
    shop.setdefault("card_holder", "")
    shop.setdefault("packages", [dict(p) for p in DEFAULT_PACKAGES])
    shop.setdefault("trial", {"enabled": True, "days": 1, "gb": 2})
    tg.setdefault("orders", [])
    tg.setdefault("tg_users", {})
    tg.setdefault("pending", {})
    return tg


def is_admin(tg_id) -> bool:
    try:
        n = int(tg_id)
    except Exception:
        return False
    return n in state().get("admin_ids", [])


def _esc(s) -> str:
    return html.escape(str(s or ""))


def panel_url() -> str:
    M = _M()
    host = M.SETTINGS.get("domain") or M.get_host()
    return f"https://{host}{M.PANEL_PATH}"


# ── Telegram API helpers ─────────────────────────────────────────────────────
async def api(method: str, **params) -> dict:
    tok = str(state().get("bot_token") or "").strip()
    if not tok:
        return {"ok": False, "description": "no_token"}
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            r = await client.post(TG_API.format(token=tok, method=method), json=params)
            return r.json()
    except Exception as e:
        logger.warning("tg api %s failed: %s", method, e)
        return {"ok": False, "description": str(e)}


async def send(chat_id, text: str, kb: dict | None = None) -> dict:
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if kb:
        params["reply_markup"] = kb
    return await api("sendMessage", **params)


async def answer_cb(cb_id: str, text: str = "", alert: bool = False):
    return await api("answerCallbackQuery", callback_query_id=cb_id,
                     text=text, show_alert=alert)


def _btn(text, data):
    return {"text": text, "callback_data": data}


def _url_btn(text, url):
    return {"text": text, "url": url}


def _rows(*rows):
    return {"inline_keyboard": [r for r in rows if r]}


def _main_menu() -> dict:
    return _rows(
        [_btn("🛒 خرید اشتراک", "menu:buy"), _btn("👤 حساب من", "menu:my")],
        [_btn("🎁 تست رایگان", "menu:trial"), _btn("☎️ پشتیبانی", "menu:support")],
    )


def _admin_menu() -> dict:
    return _rows(
        [_btn("📊 آمار", "admin:stats"), _btn("👥 کاربران", "admin:users")],
        [_btn("🧾 سفارش‌ها", "admin:orders"), _btn("📢 پیام همگانی", "admin:broadcast")],
        [_url_btn("🖥 پنل مدیریت", panel_url())],
        [_btn("🛒 منوی فروشگاه", "menu:buy")],
    )


# ── Panel account helpers ────────────────────────────────────────────────────
def _first_inbound_id():
    M = _M()
    for iid, ib in M.INBOUNDS.items():
        if (ib.get("protocol") or "").lower() in ("vless", "reality", "worker"):
            return iid
    return next(iter(M.INBOUNDS.keys()), None)


def _user_sub_url(uid: str, u: dict) -> str:
    M = _M()
    host = M.SETTINGS.get("domain") or M.get_host()
    return M.build_sub_url(host, "user", uid, u)


async def create_panel_user(name: str, days: int, gb: float, tg_id=None):
    M = _M()
    body = {
        "username": name,
        "expire_days": int(days or 0),
        "traffic_limit_gb": float(gb or 0),
        "protocol": "vless",
        "password": M.secrets.token_urlsafe(12),
    }
    iid = _first_inbound_id()
    if iid:
        body["inbound_id"] = iid
    res = await M.create_user_core(body)
    if tg_id is not None:
        tgs = state().setdefault("tg_users", {})
        tgs[str(tg_id)] = {
            "panel_user_id": res.get("user_id"),
            "username": res.get("username"),
            "created_at": M.datetime.now().isoformat(),
        }
        asyncio.create_task(M.save_state())
    return res


async def extend_panel_user(uid: str, days: int, gb: float):
    M = _M()
    async with M.USERS_LOCK:
        u = M.USERS.get(uid)
        if not u:
            return None
        base = M.datetime.now()
        if u.get("expire_at"):
            try:
                base = max(base, M.datetime.fromisoformat(u["expire_at"]))
            except Exception:
                pass
        u["expire_at"] = (base + M.timedelta(days=int(days or 0))).isoformat()
        if gb and float(gb) > 0:
            u["traffic_limit_bytes"] = int(u.get("traffic_limit_bytes", 0)) + int(float(gb) * 1024 ** 3)
        u["status"] = "active"
        M.sub_hash_for("user", uid, u)
    asyncio.create_task(M.save_state())
    return u


def _find_user_by_name(name: str):
    M = _M()
    for uid, u in M.USERS.items():
        if u.get("username") == name:
            return uid, u
    return None, None


# ── Message handlers ─────────────────────────────────────────────────────────
async def _cmd_start(chat_id, tg_id, text):
    tg = state()
    name = "دوست عزیز"
    if is_admin(tg_id):
        await send(chat_id,
                   f"👋 سلام {name}!\nبه <b>پنل مدیریت {_esc(_M().brand('panel_name'))}</b> خوش آمدید.\n\n"
                   f"🆔 آیدی شما: <code>{tg_id}</code>",
                   _admin_menu())
        return
    shop = tg.get("shop", {})
    welcome = shop.get("welcome") or (
        f"🎉 به <b>{_esc(_M().brand('panel_name'))}</b> خوش آمدید!\n\n"
        "از منوی زیر می‌توانید اشتراک بخرید، حساب خود را ببینید و درخواست تست رایگان بدهید."
    )
    await send(chat_id, welcome, _main_menu())


async def _cmd_help(chat_id, tg_id):
    await send(chat_id,
               "📖 <b>راهنما</b>\n\n"
               "/start شروع\n/buy خرید اشتراک\n/my حساب من\n/trial تست رایگان\n"
               "برای پشتیبانی روی دکمه پشتیبانی بزنید.\n\n"
               "ارسال رسید پرداخت: عکس رسید را همین‌جا بفرستید تا ادمین تأیید کند.",
               _main_menu())


async def _cmd_my(chat_id, tg_id):
    M = _M()
    tg = state()
    rec = tg.get("tg_users", {}).get(str(tg_id))
    if not rec or not rec.get("panel_user_id"):
        await send(chat_id, "شما هنوز اشتراکی ندارید. از دکمه خرید استفاده کنید.", _main_menu())
        return
    uid = rec["panel_user_id"]
    async with M.USERS_LOCK:
        u = M.USERS.get(uid)
        u = dict(u) if u else None
    if not u:
        await send(chat_id, "حساب شما یافت نشد. با پشتیبانی تماس بگیرید.", _main_menu())
        return
    sub = _user_sub_url(uid, u)
    used = M.fmt_bytes(u.get("traffic_used_bytes", 0))
    limit = "∞" if not u.get("traffic_limit_bytes") else M.fmt_bytes(u["traffic_limit_bytes"])
    await send(chat_id,
               f"👤 <b>حساب شما</b>\n\n"
               f"کاربری: <code>{_esc(u.get('username'))}</code>\n"
               f"مصرف: {used} از {limit}\n"
               f"انقضا: {_esc(u.get('expire_at') or '—')}\n\n"
               f"🔗 لینک اشتراک:\n<code>{_esc(sub)}</code>",
               _rows([_btn("🔄 بروزرسانی", "menu:my"), _btn("🛒 تمدید/خرید", "menu:buy")]))


async def _cmd_buy(chat_id):
    shop = state().get("shop", {})
    if not shop.get("enabled", True):
        await send(chat_id, "فروشگاه فعلاً غیرفعال است.")
        return
    pkgs = shop.get("packages") or []
    if not pkgs:
        await send(chat_id, "فعلاً پکیجی برای فروش تعریف نشده است.", _main_menu())
        return
    rows = [[_btn(f"{p['title']} — {p['price']:,} تومان", f"buy:{p['id']}")] for p in pkgs]
    await send(chat_id, "🛒 <b>پکیج مورد نظر را انتخاب کنید:</b>", {"inline_keyboard": rows})


async def _cmd_trial(chat_id, tg_id):
    M = _M()
    tg = state()
    trial = tg.get("shop", {}).get("trial", {}) or {}
    if not trial.get("enabled", True):
        await send(chat_id, "تست رایگان غیرفعال است.", _main_menu())
        return
    tgs = tg.setdefault("tg_users", {})
    rec = tgs.get(str(tg_id))
    if rec and rec.get("trial_used"):
        await send(chat_id, "شما قبلاً از تست رایگان استفاده کرده‌اید.", _main_menu())
        return
    name = f"trial_{tg_id}"
    try:
        res = await create_panel_user(name, int(trial.get("days", 1)), float(trial.get("gb", 2)), tg_id)
    except Exception as e:
        await send(chat_id, f"خطا در ساخت تست: {_esc(e)}", _main_menu())
        return
    tgs[str(tg_id)] = {**(rec or {}), "panel_user_id": res.get("user_id"),
                       "username": name, "trial_used": True}
    asyncio.create_task(M.save_state())
    await send(chat_id,
               f"🎁 <b>اشتراک تست شما فعال شد!</b>\n\n"
               f"🔗 لینک اشتراک:\n<code>{_esc(res.get('subscription_url'))}</code>",
               _main_menu())


# ── Callback handlers ────────────────────────────────────────────────────────
async def _cb_buy(chat_id, tg_id, pkg_id, cb_id):
    tg = state()
    pkg = next((p for p in tg.get("shop", {}).get("packages", []) if p["id"] == pkg_id), None)
    if not pkg:
        await answer_cb(cb_id, "پکیج یافت نشد", True)
        return
    order = {
        "id": f"ord{int(_M().time.time())}{tg_id}",
        "tg_id": tg_id,
        "package": pkg,
        "status": "pending_payment",
        "created_at": _M().datetime.now().isoformat(),
    }
    tg.setdefault("orders", []).append(order)
    tg.setdefault("pending", {})[str(tg_id)] = order["id"]
    asyncio.create_task(_M().save_state())

    shop = tg.get("shop", {})
    card = shop.get("card_number") or "—"
    holder = shop.get("card_holder") or "—"
    await answer_cb(cb_id, "سفارش ثبت شد")
    await send(chat_id,
               f"🧾 <b>سفارش ثبت شد</b>\n\n"
               f"پکیج: {_esc(pkg['title'])}\n"
               f"مبلغ: <b>{pkg['price']:,} تومان</b>\n\n"
               f"💳 شماره کارت:\n<code>{_esc(card)}</code>\n"
               f"به نام: {_esc(holder)}\n\n"
               f"پس از واریز، <b>عکس رسید</b> را همین‌جا ارسال کنید تا تأیید شود.")
    await _notify_admins(
        f"🛒 <b>سفارش جدید</b>\n\n"
        f"کاربر: <code>{tg_id}</code>\nپکیج: {_esc(pkg['title'])}\n"
        f"مبلغ: {pkg['price']:,} تومان\nآیدی سفارش: <code>{order['id']}</code>",
        order["id"])


async def _cb_admin_stats(chat_id, cb_id):
    M = _M()
    async with M.USERS_LOCK:
        users = len(M.USERS)
        active = sum(1 for u in M.USERS.values() if u.get("status") == "active")
        used = sum(u.get("traffic_used_bytes", 0) for u in M.USERS.values())
    tg = state()
    pending = [o for o in tg.get("orders", []) if o.get("status", "").startswith("pending")]
    await answer_cb(cb_id)
    await send(chat_id,
               f"📊 <b>آمار پنل</b>\n\n"
               f"👥 کاربران: {users} (فعال: {active})\n"
               f"📈 مصرف کل: {M.fmt_bytes(used)}\n"
               f"🧾 سفارش‌های در انتظار: {len(pending)}\n"
               f"🟢 اتصال‌های فعال: {len(M.connections)}",
               _admin_menu())


async def _cb_admin_users(chat_id, cb_id):
    M = _M()
    async with M.USERS_LOCK:
        items = list(M.USERS.items())
    items.sort(key=lambda kv: kv[1].get("created_at") or "", reverse=True)
    lines = ["👥 <b>آخرین کاربران</b>\n"]
    for uid, u in items[:20]:
        lines.append(f"• <code>{_esc(u.get('username'))}</code> — {_esc(u.get('status'))} — "
                     f"{M.fmt_bytes(u.get('traffic_used_bytes', 0))}")
    if len(items) > 20:
        lines.append(f"\n… و {len(items) - 20} کاربر دیگر")
    await answer_cb(cb_id)
    await send(chat_id, "\n".join(lines), _admin_menu())


async def _cb_admin_orders(chat_id, cb_id):
    tg = state()
    pending = [o for o in tg.get("orders", []) if o.get("status", "").startswith("pending")]
    await answer_cb(cb_id)
    if not pending:
        await send(chat_id, "🧾 سفارش در انتظاری وجود ندارد.", _admin_menu())
        return
    for o in pending[-10:]:
        pkg = o.get("package", {})
        await send(chat_id,
                   f"🧾 سفارش <code>{o['id']}</code>\n"
                   f"کاربر: <code>{o['tg_id']}</code>\n"
                   f"پکیج: {_esc(pkg.get('title'))} ({pkg.get('price', 0):,} تومان)\n"
                   f"وضعیت: {_esc(o.get('status'))}",
                   _rows([_btn("✅ تأیید", f"order:approve:{o['id']}"),
                          _btn("❌ رد", f"order:reject:{o['id']}")]))


async def _approve_order(order_id, admin_id):
    M = _M()
    tg = state()
    order = next((o for o in tg.get("orders", []) if o["id"] == order_id), None)
    if not order:
        return False, "سفارش یافت نشد"
    if order.get("status") == "approved":
        return False, "قبلاً تأیید شده"
    pkg = order.get("package", {})
    uid = order.get("tg_id")
    rec = tg.get("tg_users", {}).get(str(uid), {})
    try:
        if rec.get("panel_user_id") and _find_user_by_name(rec.get("username"))[0]:
            await extend_panel_user(rec["panel_user_id"], pkg.get("days", 0), pkg.get("gb", 0))
            _u = M.USERS.get(rec["panel_user_id"], {})
            sub = _user_sub_url(rec["panel_user_id"], _u)
        else:
            uname = f"tg{uid}"
            res = await create_panel_user(uname, pkg.get("days", 0), pkg.get("gb", 0), uid)
            sub = res.get("subscription_url")
            rec = tg.setdefault("tg_users", {}).setdefault(str(uid), {})
            rec["panel_user_id"] = res.get("user_id")
            rec["username"] = res.get("username")
    except Exception as e:
        return False, f"خطا: {e}"
    order["status"] = "approved"
    order["approved_by"] = admin_id
    order["approved_at"] = M.datetime.now().isoformat()
    tg.get("pending", {}).pop(str(uid), None)
    asyncio.create_task(M.save_state())
    await send(uid,
               f"✅ <b>پرداخت شما تأیید شد!</b>\n\n"
               f"پکیج: {_esc(pkg.get('title'))}\n"
               f"🔗 لینک اشتراک:\n<code>{_esc(sub)}</code>")
    return True, "تأیید شد"


async def _reject_order(order_id, admin_id):
    tg = state()
    order = next((o for o in tg.get("orders", []) if o["id"] == order_id), None)
    if not order:
        return False, "سفارش یافت نشد"
    order["status"] = "rejected"
    order["rejected_by"] = admin_id
    tg.get("pending", {}).pop(str(order.get("tg_id")), None)
    asyncio.create_task(_M().save_state())
    await send(order.get("tg_id"), "❌ متأسفانه پرداخت شما تأیید نشد. با پشتیبانی تماس بگیرید.")
    return True, "رد شد"


async def _notify_admins(text: str, order_id: str | None = None):
    tg = state()
    kb = _rows([_btn("✅ تأیید", f"order:approve:{order_id}"), _btn("❌ رد", f"order:reject:{order_id}")]) if order_id else None
    for aid in tg.get("admin_ids", []):
        try:
            await send(aid, text, kb)
        except Exception:
            continue


# ── Update handling ──────────────────────────────────────────────────────────
async def _handle_update(upd: dict):
    if "message" in upd:
        msg = upd["message"]
        chat_id = msg.get("chat", {}).get("id")
        tg_id = msg.get("from", {}).get("id")
        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            cmd = text.split()[0].split("@")[0].lower()
            args = text.split()[1:]
            await _handle_command(chat_id, tg_id, cmd, args, msg)
        elif msg.get("photo") or msg.get("document"):
            await _handle_receipt(chat_id, tg_id, msg)
        elif text:
            await send(chat_id, "برای شروع دستور /start را بزنید.", _main_menu())
    elif "callback_query" in upd:
        cq = upd["callback_query"]
        chat_id = cq.get("message", {}).get("chat", {}).get("id")
        tg_id = cq.get("from", {}).get("id")
        data = cq.get("data", "")
        cb_id = cq.get("id")
        await _handle_callback(chat_id, tg_id, data, cb_id)


async def _handle_command(chat_id, tg_id, cmd, args, msg):
    if cmd in ("/start", "/menu"):
        await _cmd_start(chat_id, tg_id, msg.get("text", ""))
    elif cmd == "/help":
        await _cmd_help(chat_id, tg_id)
    elif cmd == "/my":
        await _cmd_my(chat_id, tg_id)
    elif cmd == "/buy":
        await _cmd_buy(chat_id)
    elif cmd == "/trial":
        await _cmd_trial(chat_id, tg_id)
    # ── admin commands ──
    elif cmd == "/stats" and is_admin(tg_id):
        await _cb_admin_stats(chat_id, "")
    elif cmd == "/users" and is_admin(tg_id):
        await _cb_admin_users(chat_id, "")
    elif cmd == "/orders" and is_admin(tg_id):
        await _cb_admin_orders(chat_id, "")
    elif cmd == "/panel" and is_admin(tg_id):
        await send(chat_id, f"🖥 {panel_url()}", _admin_menu())
    elif cmd == "/admins" and is_admin(tg_id):
        ids = ", ".join(str(x) for x in state().get("admin_ids", []))
        await send(chat_id, f"👑 ادمین‌ها:\n<code>{_esc(ids)}</code>\n\n"
                            "افزودن: <code>/addadmin 123456</code>\nحذف: <code>/deladmin 123456</code>")
    elif cmd == "/addadmin" and is_admin(tg_id) and args:
        ids = state().setdefault("admin_ids", [])
        for a in args:
            if a.lstrip("-").isdigit() and int(a) not in ids:
                ids.append(int(a))
        asyncio.create_task(_M().save_state())
        await send(chat_id, "✅ ادمین اضافه شد.", _admin_menu())
    elif cmd == "/deladmin" and is_admin(tg_id) and args:
        ids = state().setdefault("admin_ids", [])
        for a in args:
            if a.lstrip("-").isdigit() and int(a) in ids:
                ids.remove(int(a))
        asyncio.create_task(_M().save_state())
        await send(chat_id, "🗑 ادمین حذف شد.", _admin_menu())
    elif cmd == "/adduser" and is_admin(tg_id) and len(args) >= 3:
        try:
            res = await create_panel_user(args[0], int(args[1]), float(args[2]))
            await send(chat_id, f"✅ کاربر ساخته شد:\n{_esc(res.get('username'))}\n"
                                f"<code>{_esc(res.get('subscription_url'))}</code>", _admin_menu())
        except Exception as e:
            await send(chat_id, f"⚠️ خطا: {_esc(e)}")
    elif cmd == "/deluser" and is_admin(tg_id) and args:
        M = _M()
        uid, _u = _find_user_by_name(args[0])
        if uid:
            async with M.USERS_LOCK:
                M.USERS.pop(uid, None)
            async with M.LINKS_LOCK:
                for lid in [k for k, l in M.LINKS.items() if l.get("user_id") == uid]:
                    M.LINKS.pop(lid, None)
            asyncio.create_task(M.save_state())
            await send(chat_id, "🗑 کاربر حذف شد.", _admin_menu())
        else:
            await send(chat_id, "کاربر یافت نشد.")
    elif cmd == "/broadcast" and is_admin(tg_id):
        body = msg.get("text", "")[len("/broadcast"):].strip()
        if not body:
            await send(chat_id, "متن پیام را بنویسید: <code>/broadcast سلام …</code>")
            return
        sent = failed = 0
        for tid in list(state().get("tg_users", {}).keys()):
            r = await send(int(tid), body)
            if r.get("ok"):
                sent += 1
            else:
                failed += 1
            await asyncio.sleep(0.05)
        await send(chat_id, f"📢 ارسال شد: {sent} | ناموفق: {failed}", _admin_menu())
    else:
        await send(chat_id, "دستور ناشناخته. /start", _main_menu())


async def _handle_callback(chat_id, tg_id, data, cb_id):
    if data == "menu:buy":
        await answer_cb(cb_id)
        await _cmd_buy(chat_id)
    elif data == "menu:my":
        await answer_cb(cb_id)
        await _cmd_my(chat_id, tg_id)
    elif data == "menu:trial":
        await answer_cb(cb_id)
        await _cmd_trial(chat_id, tg_id)
    elif data == "menu:support":
        await answer_cb(cb_id)
        await send(chat_id, f"☎️ پشتیبانی: {_esc(_M().brand('support_url'))}")
    elif data.startswith("buy:"):
        await _cb_buy(chat_id, tg_id, data.split(":", 1)[1], cb_id)
    elif data == "admin:stats" and is_admin(tg_id):
        await _cb_admin_stats(chat_id, cb_id)
    elif data == "admin:users" and is_admin(tg_id):
        await _cb_admin_users(chat_id, cb_id)
    elif data == "admin:orders" and is_admin(tg_id):
        await _cb_admin_orders(chat_id, cb_id)
    elif data == "admin:broadcast" and is_admin(tg_id):
        await answer_cb(cb_id)
        await send(chat_id, "متن پیام را بنویسید: <code>/broadcast سلام …</code>")
    elif data.startswith("order:approve:") and is_admin(tg_id):
        ok, msg = await _approve_order(data.split(":", 2)[2], tg_id)
        await answer_cb(cb_id, msg, True)
    elif data.startswith("order:reject:") and is_admin(tg_id):
        ok, msg = await _reject_order(data.split(":", 2)[2], tg_id)
        await answer_cb(cb_id, msg, True)
    else:
        await answer_cb(cb_id, "دسترسی ندارید", True)


async def _handle_receipt(chat_id, tg_id, msg):
    tg = state()
    order_id = tg.get("pending", {}).get(str(tg_id))
    if not order_id:
        await send(chat_id, "رسیدی برای سفارش فعال شما یافت نشد.")
        return
    order = next((o for o in tg.get("orders", []) if o["id"] == order_id), None)
    if order:
        order["status"] = "receipt_sent"
        asyncio.create_task(_M().save_state())
    await send(chat_id, "✅ رسید دریافت شد. پس از تأیید ادمین، اشتراک شما فعال می‌شود.", _main_menu())
    caption = (f"📎 <b>رسید پرداخت</b>\nکاربر: <code>{tg_id}</code>\n"
               f"سفارش: <code>{order_id}</code>")
    kb = _rows([_btn("✅ تأیید", f"order:approve:{order_id}"), _btn("❌ رد", f"order:reject:{order_id}")])
    photo = (msg.get("photo") or [])
    for aid in tg.get("admin_ids", []):
        try:
            if photo:
                fid = photo[-1]["file_id"]
                await api("sendPhoto", chat_id=aid, photo=fid, caption=caption,
                          parse_mode="HTML", reply_markup=kb)
            else:
                await api("sendDocument", chat_id=aid,
                          document=msg["document"]["file_id"], caption=caption,
                          parse_mode="HTML", reply_markup=kb)
        except Exception:
            continue


# ── Long-polling loop ────────────────────────────────────────────────────────
async def _poll_loop():
    global _LAST_UPDATE_ID
    await asyncio.sleep(5)
    while True:
        try:
            tg = state()
            tok = str(tg.get("bot_token") or "").strip()
            if not tok or not tg.get("enabled", True):
                await asyncio.sleep(10)
                continue
            params = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
            if _LAST_UPDATE_ID:
                params["offset"] = _LAST_UPDATE_ID + 1
            res = await api("getUpdates", **params)
            if not res.get("ok"):
                await asyncio.sleep(8)
                continue
            for upd in res.get("result", []):
                _LAST_UPDATE_ID = max(_LAST_UPDATE_ID, int(upd.get("update_id", 0)))
                try:
                    await _handle_update(upd)
                except Exception as e:
                    logger.warning("bot update handling failed: %s", e)
        except Exception as e:
            logger.warning("bot poll loop error: %s", e)
            await asyncio.sleep(8)


def start_polling():
    """Start (or restart) the long-polling task."""
    global _POLL_TASK
    if _POLL_TASK and not _POLL_TASK.done():
        return _POLL_TASK
    _POLL_TASK = asyncio.create_task(_poll_loop())
    logger.info("PANAHANNET Telegram bot polling started")
    return _POLL_TASK


async def _start_telegram_bot():
    """Entry point called from main.py startup."""
    try:
        tg = state()
        if str(tg.get("bot_token") or "").strip():
            start_polling()
        else:
            logger.info("Telegram bot disabled (no token configured)")
    except Exception as e:
        logger.warning("failed to start Telegram bot: %s", e)


# ── Panel-facing API (registered from main.py) ───────────────────────────────
def register(app):
    """Register bot-management routes on the FastAPI app."""
    from fastapi import Depends, HTTPException, Request

    def _auth():
        import main as M
        return M.require_auth

    @app.get("/api/telegram/bot")
    async def bot_status(_=Depends(_auth())):
        tg = state()
        me = await api("getMe") if str(tg.get("bot_token") or "").strip() else {"ok": False}
        return {
            "enabled": tg.get("enabled", True),
            "has_token": bool(str(tg.get("bot_token") or "").strip()),
            "admin_ids": tg.get("admin_ids", []),
            "bot_username": (me.get("result") or {}).get("username", "") if me.get("ok") else "",
            "shop": tg.get("shop", {}),
            "orders_pending": len([o for o in tg.get("orders", []) if o.get("status", "").startswith("pending")]),
            "panel_url": panel_url(),
        }

    @app.post("/api/telegram/bot")
    async def bot_config(request: Request, _=Depends(_auth())):
        body = await request.json()
        tg = state()
        if "bot_token" in body:
            tg["bot_token"] = str(body["bot_token"]).strip()
        if "enabled" in body:
            tg["enabled"] = bool(body["enabled"])
        if "admin_ids" in body:
            tg["admin_ids"] = _parse_ids(body["admin_ids"])
        for k in ("card_number", "card_holder", "welcome"):
            if k in body:
                tg.setdefault("shop", {})[k] = str(body[k]).strip()
        if "packages" in body and isinstance(body["packages"], list):
            tg.setdefault("shop", {})["packages"] = body["packages"]
        if "trial" in body and isinstance(body["trial"], dict):
            tg.setdefault("shop", {})["trial"] = body["trial"]
        asyncio.create_task(_M().save_state())
        start_polling()
        return {"ok": True, "admin_ids": tg.get("admin_ids", [])}

    @app.post("/api/telegram/bot/test")
    async def bot_test(_=Depends(_auth())):
        me = await api("getMe")
        return {"ok": bool(me.get("ok")), "result": me.get("result"), "error": me.get("description")}

    @app.get("/api/telegram/bot/orders")
    async def bot_orders(_=Depends(_auth())):
        return {"orders": list(reversed(state().get("orders", [])))[:100]}

    @app.post("/api/telegram/bot/orders/{order_id}/approve")
    async def bot_order_approve(order_id: str, _=Depends(_auth())):
        ok, msg = await _approve_order(order_id, "panel")
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        return {"ok": True, "message": msg}

    @app.post("/api/telegram/bot/orders/{order_id}/reject")
    async def bot_order_reject(order_id: str, _=Depends(_auth())):
        ok, msg = await _reject_order(order_id, "panel")
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        return {"ok": True, "message": msg}
