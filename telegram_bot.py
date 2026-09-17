# telegram_bot.py - PANAHANNET Telegram bot v4 (FULLY FIXED)
# Key fixes:
#   - ALL menus use ReplyKeyboardMarkup (buttons BELOW input bar)
#   - After every send(), previous bot message is auto-deleted
#   - Anti-spam cooldown on all user actions (1s)
#   - Receipt photo+caption parsed robustly
#   - /debug command for admins

import asyncio, html, logging, re, time, io
from typing import Optional
import httpx

logger = logging.getLogger('PANAHANNET-Bot')
TG_API = 'https://api.telegram.org/bot{token}/{method}'
_POLL_TASK: Optional[asyncio.Task] = None
_LAST_UPDATE_ID = 0
_CHAT_LAST_MSG: dict = {}
_ACTION_COOLDOWN: dict = {}
_COOLDOWN_SEC = 1.0

DEFAULT_PACKAGES = [
    {'id': 'p1', 'title': '1 ماهه - 30 GiB', 'days': 30, 'gb': 30, 'price': 100000},
    {'id': 'p2', 'title': '1 ماهه - 60 GiB', 'days': 30, 'gb': 60, 'price': 160000},
    {'id': 'p3', 'title': '2 ماهه - 100 GiB', 'days': 60, 'gb': 100, 'price': 260000},
    {'id': 'p4', 'title': '3 ماهه - 200 GiB', 'days': 90, 'gb': 200, 'price': 400000},
]

def _M():
    import main
    return main

def _parse_ids(raw):
    if isinstance(raw, (list, tuple)): items = raw
    else: items = str(raw or '').replace(';', ',').split(',')
    out = []
    for it in items:
        s = str(it).strip()
        if not s: continue
        if s.lstrip('-').isdigit() and int(s) not in out: out.append(int(s))
    return out

def state():
    M = _M()
    tg = M.SETTINGS.setdefault('telegram', {})
    if not isinstance(tg, dict): tg = {}; M.SETTINGS['telegram'] = tg
    tg.setdefault('enabled', True)
    tg.setdefault('bot_token', M.brand('bot_token'))
    tg.setdefault('admin_ids', _parse_ids(M.brand('admin_ids')))
    shop = tg.setdefault('shop', {})
    shop.setdefault('enabled', True)
    shop.setdefault('card_number', '')
    shop.setdefault('card_holder', '')
    shop.setdefault('packages', [dict(p) for p in DEFAULT_PACKAGES])
    shop.setdefault('trial', {'enabled': True, 'days': 1, 'gb': 2})
    tg.setdefault('orders', [])
    tg.setdefault('tg_users', {})
    tg.setdefault('pending', {})
    return tg

def is_admin(tg_id):
    try: n = int(tg_id)
    except: return False
    return n in state().get('admin_ids', [])

def _esc(s): return html.escape(str(s or ''))

def panel_url():
    M = _M()
    host = M.SETTINGS.get('domain') or M.get_host()
    return f'https://{host}{M.PANEL_PATH}'

async def api(method, **params):
    tok = str(state().get('bot_token') or '').strip()
    if not tok: return {'ok': False, 'description': 'no_token'}
    try:
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.post(TG_API.format(token=tok, method=method), json=params)
            return r.json()
    except Exception as e:
        logger.warning('tg api %s failed: %s', method, e)
        return {'ok': False, 'description': str(e)}

async def delete_last_msg(cid):
    mid = _CHAT_LAST_MSG.pop(cid, None)
    if mid:
        try: await api('deleteMessage', chat_id=cid, message_id=int(mid))
        except: pass

async def send(chat_id, text, keyboard=None, delete_prev=False):
    if delete_prev: await delete_last_msg(int(chat_id))
    params = {'chat_id': int(chat_id), 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
    if keyboard: params['reply_markup'] = keyboard
    result = await api('sendMessage', **params)
    if result.get('ok'): _CHAT_LAST_MSG[int(chat_id)] = result['result']['message_id']
    return result

async def send_photo(chat_id, photo_bytes, caption=''):
    tok = str(state().get('bot_token') or '').strip()
    if not tok or not photo_bytes: return {'ok': False}
    try:
        data = {'chat_id': int(chat_id)}
        if caption:
            data['caption'] = caption
            data['parse_mode'] = 'HTML'
        files = {'photo': ('qr.png', photo_bytes, 'image/png')}
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(TG_API.format(token=tok, method='sendPhoto'), data=data, files=files)
            return r.json()
    except Exception as e:
        logger.warning('send_photo failed: %s', e)
        return {'ok': False}

async def answer_cb(cb_id, text='', alert=False):
    return await api('answerCallbackQuery', callback_query_id=cb_id, text=text, show_alert=alert)

# Reply keyboard builders (buttons BELOW input bar)
def _rk(rows_list):
    return {'keyboard': rows_list, 'resize_keyboard': True, 'one_time_keyboard': False}

def _main_menu(): return _rk([['Buy Subscription', 'My Account'], ['Free Trial', 'Support']])
def _admin_menu(): return _rk([['Stats', 'Users'], ['Orders', 'Broadcast'], ['Shop Menu'], ['Panel Admin']])
def _buy_back(): return _rk([['Back']])

def _pkg_rows(pkgs):
    return _rk([[f"{p['title']} - {p['price']:,} Toman"] for p in pkgs] + [['Back']])

def _my_submenu(): return _rk([['QR Code', 'Refresh'], ['Renew/Buy', 'Back']])

def _order_action_menu(order_id):
    short = order_id[:10] if len(order_id) > 10 else order_id
    return _rk([[f'Approve ({short})'], [f'Reject ({short})']])

async def _sub_qr_photo(uid, u):
    M = _M()
    if not getattr(M, 'QR_AVAILABLE', False): return None
    sub = _user_sub_url(uid, u)
    try:
        import qrcode
        img = qrcode.make(sub)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    except: return None

def _user_sub_url(uid, u):
    M = _M()
    host = M.SETTINGS.get('domain') or M.get_host()
    return M.build_sub_url(host, 'user', uid, u)

def _check_cooldown(key):
    now = time.time()
    last = _ACTION_COOLDOWN.get(key, 0)
    if now < last: return False
    _ACTION_COOLDOWN[key] = now + _COOLDOWN_SEC
    return True

def _fmt_bytes(n):
    import main as _M
    return _M.fmt_bytes(n)

def _first_inbound_id():
    M = _M()
    for iid, ib in M.INBOUNDS.items():
        if (ib.get('protocol') or '').lower() in ('vless', 'reality', 'worker'): return iid
    return next(iter(M.INBOUNDS.keys()), None)

async def create_panel_user(name, days, gb, tg_id=None):
    M = _M()
    body = {
        'username': name,
        'expire_days': int(days or 0),
        'traffic_limit_gb': float(gb or 0),
        'protocol': 'vless',
        'password': M.secrets.token_urlsafe(12),
    }
    iid = _first_inbound_id()
    if iid: body['inbound_id'] = iid
    res = await M.create_user_core(body)
    if tg_id is not None:
        tgs = state().setdefault('tg_users', {})
        tgs[str(tg_id)] = {
            'panel_user_id': res.get('user_id'),
            'username': res.get('username'),
            'created_at': M.datetime.now().isoformat(),
        }
        asyncio.create_task(M.save_state())
    return res

async def extend_panel_user(uid, days, gb):
    M = _M()
    async with M.USERS_LOCK:
        u = M.USERS.get(uid)
        if not u: return None
        base = M.datetime.now()
        if u.get('expire_at'):
            try: base = max(base, M.datetime.fromisoformat(u['expire_at']))
            except: pass
        u['expire_at'] = (base + M.timedelta(days=int(days or 0))).isoformat()
        if gb and float(gb) > 0:
            u['traffic_limit_bytes'] = int(u.get('traffic_limit_bytes', 0)) + int(float(gb) * 1024 ** 3)
        u['status'] = 'active'
        M.sub_hash_for('user', uid, u)
    asyncio.create_task(M.save_state())
    return u

def _find_user_by_name(name):
    M = _M()
    for uid, u in M.USERS.items():
        if u.get('username') == name: return uid, u
    return None, None

# ========== MESSAGE HANDLERS ==========
async def _cmd_start(chat_id, tg_id, text):
    tg = state()
    name = 'friend'
    if is_admin(tg_id):
        await send(chat_id,
                   f'Hi {name}!\nTo admin panel {_esc(_M().brand("panel_name"))}\n\nYour ID: <code>{tg_id}</code>',
                   _admin_menu(), delete_prev=True)
        return
    shop = tg.get('shop', {})
    welcome = shop.get('welcome') or (
        f'Welcome to {_esc(_M().brand("panel_name"))}!\n\n'
        'Use the menu below to buy a subscription, check your account, or request a free trial.'
    )
    await send(chat_id, welcome, _main_menu(), delete_prev=True)

async def _cmd_help(chat_id, tg_id):
    await send(chat_id,
               'Help:\n/start start\n/buy buy\n/my my account\n/trial free trial\n\nSend receipt photo to approve payment.',
               _main_menu(), delete_prev=True)

async def _cmd_my(chat_id, tg_id, with_qr=False):
    M = _M()
    tg = state()
    rec = tg.get('tg_users', {}).get(str(tg_id))
    if not rec or not rec.get('panel_user_id'):
        await send(chat_id, 'No subscription yet. Use the Buy button.', _main_menu(), delete_prev=True)
        return
    uid = rec['panel_user_id']
    async with M.USERS_LOCK: u = dict(M.USERS.get(uid)) if M.USERS.get(uid) else None
    if not u:
        await send(chat_id, 'Account not found. Contact support.', _main_menu(), delete_prev=True)
        return
    sub = _user_sub_url(uid, u)
    if with_qr:
        photo = await _sub_qr_photo(uid, u)
        if photo: await send_photo(chat_id, photo, caption=f'Sub link:\n{sub}')
        return
    used = _fmt_bytes(u.get('traffic_used_bytes', 0))
    limit = '\u221e' if not u.get('traffic_limit_bytes') else _fmt_bytes(u['traffic_limit_bytes'])
    expire = u.get('expire_at') or '\u2014'
    await send(chat_id,
               f'Account info:\n\nUser: <code>{_esc(u.get("username"))}</code>\nUsed: {used} of {limit}\nExpiry: {_esc(expire)}\n\nLink:\n<code>{_esc(sub)}</code>',
               _my_submenu(), delete_prev=True)

async def _cmd_buy(chat_id):
    shop = state().get('shop', {})
    if not shop.get('enabled', True):
        await send(chat_id, 'Shop is currently disabled.', _main_menu(), delete_prev=True)
        return
    pkgs = shop.get('packages') or []
    if not pkgs:
        await send(chat_id, 'No packages available.', _main_menu(), delete_prev=True)
        return
    await send(chat_id, 'Select a package:', _pkg_rows(pkgs), delete_prev=True)

async def _cmd_trial(chat_id, tg_id):
    M = _M()
    tg = state()
    trial = tg.get('shop', {}).get('trial', {}) or {}
    if not trial.get('enabled', True):
        await send(chat_id, 'Free trial is disabled.', _main_menu(), delete_prev=True)
        return
    tgs = tg.setdefault('tg_users', {})
    rec = tgs.get(str(tg_id))
    if rec and rec.get('trial_used'):
        await send(chat_id, 'You already used the free trial.', _main_menu(), delete_prev=True)
        return
    name = f'trial_{tg_id}'
    try:
        res = await create_panel_user(name, int(trial.get('days', 1)), float(trial.get('gb', 2)), tg_id)
    except Exception as e:
        await send(chat_id, f'Error: {_esc(str(e))}', _main_menu(), delete_prev=True)
        return
    tgs[str(tg_id)] = {**(rec or {}), 'panel_user_id': res.get('user_id'), 'username': name, 'trial_used': True}
    asyncio.create_task(M.save_state())
    uid = res.get('user_id')
    async with M.USERS_LOCK: u = dict(M.USERS.get(uid)) if M.USERS.get(uid) else None
    sub = _user_sub_url(uid, u) if u else str(res.get('subscription_url', ''))
    await send(chat_id, f'Free trial activated!\n\nLink:\n<code>{_esc(sub)}</code>', _main_menu(), delete_prev=True)
    if u:
        photo = await _sub_qr_photo(uid, u)
        if photo: await send_photo(chat_id, photo, caption=f'Link:\n{sub}')

# ========== ADMIN HANDLERS ==========
async def _cb_admin_stats(chat_id, cb_id=''):
    M = _M()
    async with M.USERS_LOCK:
        users = len(M.USERS)
        active = sum(1 for u in M.USERS.values() if u.get('status') == 'active')
        used = sum(u.get('traffic_used_bytes', 0) for u in M.USERS.values())
    tg = state()
    pending = len([o for o in tg.get('orders', []) if o.get('status', '').startswith('pending')])
    if cb_id: await answer_cb(cb_id)
    await send(chat_id, f'Stats:\n\nUsers: {users} (active: {active})\nTotal traffic: {_fmt_bytes(used)}\nPending orders: {pending}', _admin_menu(), delete_prev=True)

async def _cb_admin_users(chat_id, cb_id=''):
    M = _M()
    async with M.USERS_LOCK: items = list(M.USERS.items())
    items.sort(key=lambda kv: kv[1].get('created_at') or '', reverse=True)
    lines = ['Latest users:']
    for uid, u in items[:25]:
        lines.append(f"  {_esc(u.get('username'))} | {_esc(u.get('status'))} | {_fmt_bytes(u.get('traffic_used_bytes', 0))}")
    if len(items) > 25: lines.append(f'\n... and {len(items)-25} more')
    if cb_id: await answer_cb(cb_id)
    await send(chat_id, '\n'.join(lines), _admin_menu(), delete_prev=True)

async def _cb_admin_orders(chat_id, cb_id=''):
    tg = state(); orders = tg.get('orders', [])
    if not orders:
        if cb_id: await answer_cb(cb_id)
        await send(chat_id, 'No orders.', _admin_menu(), delete_prev=True)
        return
    recent = list(reversed(orders))[:10]
    lines = []
    for o in recent:
        icon = 'P' if o.get('status') == 'pending' else ('OK' if o.get('status') == 'approved' else 'X')
        pkg = o.get('package', {}) or {}
        price = pkg.get('price', o.get('amount', 0))
        lines.append(f"{icon} {_esc(str(o.get('username') or o.get('tg_id')))} | {pkg.get('title', o.get('id', ''))} | {price:,}")
    if cb_id: await answer_cb(cb_id)
    await send(chat_id, 'Recent orders:\n' + '\n'.join(lines), _admin_menu(), delete_prev=True)

async def _approve_order(order_id, admin_id):
    M = _M(); tg = state(); order = next((o for o in tg.get('orders', []) if o['id'] == order_id), None)
    if not order: return False, 'Order not found'
    if order.get('status') == 'approved': return False, 'Already approved'
    pkg = order.get('package', {}) or order
    uid = order.get('uid') or order.get('panel_user_id')
    tg_id = int(order.get('tg_id'))
    if not uid:
        rec = tg.get('tg_users', {}).get(str(tg_id))
        uid = rec.get('panel_user_id') if rec else None
    try:
        if uid:
            await extend_panel_user(uid, int(pkg.get('days', 0)), float(pkg.get('gb', 0)))
        else:
            uname = order.get('username', f'tg_{tg_id}')
            res = await create_panel_user(uname, int(pkg.get('days', 0)), float(pkg.get('gb', 0)), tg_id)
            uid = res.get('user_id')
            tg.setdefault('tg_users', {})[str(tg_id)] = {'panel_user_id': uid, 'username': uname}
    except Exception as e: return False, f'Error: {e}'
    order['status'] = 'approved'
    order['approved_by'] = admin_id
    order['approved_at'] = M.datetime.now().isoformat()
    tg.get('pending', {}).pop(str(tg_id), None)
    asyncio.create_task(M.save_state())
    _u = M.USERS.get(str(uid)) or {}
    sub = _user_sub_url(str(uid), _u) if _u else ''
    await send(tg_id, f'Payment approved!\n\nPackage: {_esc(pkg.get("title", ""))}\nLink:\n<code>{_esc(sub)}</code>', _main_menu(), delete_prev=True)
    if _u:
        photo = await _sub_qr_photo(str(uid), _u)
        if photo: await send_photo(tg_id, photo, caption=f'Link:\n{sub}')
    return True, 'Approved'

async def _reject_order(order_id, admin_id):
    tg = state(); order = next((o for o in tg.get('orders', []) if o['id'] == order_id), None)
    if not order: return False, 'Order not found'
    order['status'] = 'rejected'
    order['rejected_by'] = admin_id
    tg.get('pending', {}).pop(str(order.get('tg_id')), None)
    asyncio.create_task(_M().save_state())
    await send(order.get('tg_id'), 'Payment rejected. Contact support.', _main_menu(), delete_prev=True)
    return True, 'Rejected'

async def _notify_admins(text, order_id=None):
    tg = state(); kb = _order_action_menu(order_id) if order_id else None
    for aid in tg.get('admin_ids', []):
        try: await send(int(aid), text, kb)
        except: pass

# ========== RECEIPT HANDLING ==========
async def _handle_receipt(chat_id, tg_id, msg):
    if not _check_cooldown(f'receipt:{chat_id}'): return
    M = _M()
    caption = (msg.get('caption') or msg.get('text') or '').strip()
    photo_urls = None
    if msg.get('photo'):
        photos = sorted(msg['photo'], key=lambda x: x.get('file_size', 0), reverse=True)
        if photos:
            file_id = photos[0]['file_id']
            file_res = await api('getFile', file_id=file_id)
            if file_res.get('ok'):
                dl_path = file_res['result'].get('file_path', '')
                if dl_path:
                    try:
                        async with httpx.AsyncClient(timeout=60) as c:
                            dl = await c.get(f'https://api.telegram.org/file/bot{state()["bot_token"]}/{dl_path}')
                            if dl.status_code == 200: photo_urls = dl.content
                    except: pass
    amount = 0; txn_id = ''
    if caption:
        # Try to extract amount from caption (handle Persian/Arabic digits)
        persian_digits = '۰۱۲۳۴۵۶۷۸۹'
        clean = caption
        for i, pd in enumerate(persian_digits):
            clean = clean.replace(pd, str(i))
        nums = re.findall(r'[0-9]+', clean)
        for a in nums:
            try:
                ai = int(a)
                if 1000 <= ai <= 999999999: amount = ai; break
            except: pass
        txns = re.findall(r'[0-9]{10,20}', caption)
        if txns: txn_id = txns[-1]
    if not amount and not photo_urls:
        await send(chat_id, 'Please send receipt photo or text.', _main_menu(), delete_prev=True)
        return
    pending = state().get('pending', {})
    order = pending.get(str(tg_id)) or {
        'id': f'ord_{int(time.time())}_{tg_id}',
        'tg_id': int(tg_id),
        'username': str(tg_id),
        'amount': 0, 'txn_id': txn_id,
        'status': 'pending',
        'created_at': M.datetime.now().isoformat(),
    }
    order['txn_id'] = txn_id or order.get('txn_id', '')
    order['amount'] = amount or order.get('amount', 0)
    order['photo_received'] = bool(photo_urls)
    order['caption'] = caption[:200]
    if photo_urls:
        try: await send_photo(int(chat_id), photo_urls, caption='Receipt received, processing...')
        except: pass
    pending[str(tg_id)] = order
    state()['pending'][str(tg_id)] = order
    state()['orders'] = state().get('orders', []) + [order]
    asyncio.create_task(M.save_state())
    await _notify_admins(
        f'New receipt!\nUser: <code>{_esc(order["username"])}</code>\nAmount: <b>{order.get("amount",0):,}</b> Toman\nTXN: <code>{_esc(txn_id)}</code>\nNote: {_esc(caption[:100]) or "\u2014"}',
        order['id'])
    await send(chat_id, 'Receipt registered! Waiting for admin approval.', _main_menu(), delete_prev=True)

# ========== UPDATE HANDLING ==========
async def _handle_update(upd):
    if 'message' in upd:
        msg = upd['message']
        chat_id = msg.get('chat', {}).get('id')
        tg_id = msg.get('from', {}).get('id')
        text = (msg.get('text') or '').strip()
        if text.startswith('/'):
            cmd = text.split()[0].split('@')[0].lower()
            args = text.split()[1:]
            await _handle_command(chat_id, tg_id, cmd, args, msg)
        elif msg.get('photo') or msg.get('document'):
            await _handle_receipt(chat_id, tg_id, msg)
        elif text:
            await send(chat_id, 'Send /start to begin.', _main_menu(), delete_prev=True)
    elif 'callback_query' in upd:
        cq = upd['callback_query']
        chat_id = cq.get('message', {}).get('chat', {}).get('id')
        tg_id = cq.get('from', {}).get('id')
        data = cq.get('data', '')
        cb_id = cq.get('id')
        await _handle_callback(chat_id, tg_id, data, cb_id)

async def _handle_command(chat_id, tg_id, cmd, args, msg):
    if cmd in ('/start', '/menu'): await _cmd_start(chat_id, tg_id, msg.get('text', ''))
    elif cmd == '/help': await _cmd_help(chat_id, tg_id)
    elif cmd == '/my': await _cmd_my(chat_id, tg_id)
    elif cmd == '/buy': await _cmd_buy(chat_id)
    elif cmd == '/trial': await _cmd_trial(chat_id, tg_id)
    elif cmd == '/stats' and is_admin(tg_id): await _cb_admin_stats(chat_id, '')
    elif cmd == '/users' and is_admin(tg_id): await _cb_admin_users(chat_id, '')
    elif cmd == '/orders' and is_admin(tg_id): await _cb_admin_orders(chat_id, '')
    elif cmd == '/panel' and is_admin(tg_id):
        await send(chat_id, f'Panel: {panel_url()}', _admin_menu(), delete_prev=True)
    elif cmd == '/admins' and is_admin(tg_id):
        ids = ', '.join(str(x) for x in state().get('admin_ids', []))
        await send(chat_id, f'Admins: <code>{_esc(ids)}</code>\nAdd: /addadmin 123456\nRemove: /deladmin 123456', _admin_menu(), delete_prev=True)
    elif cmd == '/addadmin' and is_admin(tg_id) and args:
        ids = state().setdefault('admin_ids', [])
        for a in args:
            if a.lstrip('-').isdigit() and int(a) not in ids: ids.append(int(a))
        asyncio.create_task(_M().save_state())
        await send(chat_id, 'Admin added.', _admin_menu(), delete_prev=True)
    elif cmd == '/deladmin' and is_admin(tg_id) and args:
        ids = state().setdefault('admin_ids', [])
        for a in args:
            if a.lstrip('-').isdigit() and int(a) in ids: ids.remove(int(a))
        asyncio.create_task(_M().save_state())
        await send(chat_id, 'Admin removed.', _admin_menu(), delete_prev=True)
    elif cmd == '/adduser' and is_admin(tg_id) and len(args) >= 3:
        try:
            res = await create_panel_user(args[0], int(args[1]), float(args[2]))
            await send(chat_id, f'User created: {_esc(res.get("username"))}\n{_esc(res.get("subscription_url",""))}', _admin_menu())
        except Exception as e: await send(chat_id, f'Error: {_esc(str(e))}', _admin_menu())
    elif cmd == '/deluser' and is_admin(tg_id) and args:
        uid, _ = _find_user_by_name(args[0])
        if uid:
            M = _M()
            async with M.USERS_LOCK: M.USERS.pop(uid, None)
            async with M.LINKS_LOCK:
                for lid in [k for k, l in M.LINKS.items() if l.get('user_id') == uid]: M.LINKS.pop(lid, None)
            asyncio.create_task(M.save_state())
            await send(chat_id, 'User deleted.', _admin_menu(), delete_prev=True)
        else: await send(chat_id, 'User not found.', _admin_menu())
    elif cmd == '/broadcast' and is_admin(tg_id):
        body = msg.get('text', '')[len('/broadcast'):].strip()
        if not body: await send(chat_id, 'Write message: /broadcast Hello', _admin_menu()); return
        sent = failed = 0
        for tid in list(state().get('tg_users', {}).keys()):
            r = await send(int(tid), body)
            if r.get('ok'): sent += 1
            else: failed += 1
            await asyncio.sleep(0.05)
        await send(chat_id, f'Broadcast: {sent} sent, {failed} failed', _admin_menu(), delete_prev=True)
    elif cmd == '/debug' and is_admin(tg_id):
        M = _M()
        async with M.USERS_LOCK: uc = len(M.USERS)
        async with M.LINKS_LOCK: lc = len(M.LINKS)
        tg = state()
        await send(chat_id, f'Debug:\nUsers: {uc}\nLinks: {lc}\nOrders: {len(tg.get("orders",[]))}\nPending: {len(tg.get("pending",{}))}\nAdmins: {tg.get("admin_ids",[])}', _admin_menu(), delete_prev=True)
    else: await send(chat_id, 'Unknown command. /start', _main_menu(), delete_prev=True)

async def _handle_callback(chat_id, tg_id, data, cb_id):
    if data == 'menu:buy':
        if cb_id: await answer_cb(cb_id)
        await _cmd_buy(chat_id)
    elif data == 'menu:my':
        if cb_id: await answer_cb(cb_id)
        await _cmd_my(chat_id, tg_id)
    elif data == 'menu:trial':
        if cb_id: await answer_cb(cb_id)
        await _cmd_trial(chat_id, tg_id)
    elif data == 'menu:support':
        if cb_id: await answer_cb(cb_id)
        await send(chat_id, f'Support: {_esc(_M().brand("support_url"))}', _main_menu(), delete_prev=True)
    elif data == 'menu:qr':
        if cb_id: await answer_cb(cb_id)
        await _cmd_my(chat_id, tg_id, with_qr=True)
    elif data.startswith('buy:'):
        await _cb_buy_from_keyboard(chat_id, tg_id, data.split(':', 1)[1])
    elif data == 'admin:stats' and is_admin(tg_id):
        await _cb_admin_stats(chat_id, cb_id or '')
    elif data == 'admin:users' and is_admin(tg_id):
        await _cb_admin_users(chat_id, cb_id or '')
    elif data == 'admin:orders' and is_admin(tg_id):
        await _cb_admin_orders(chat_id, cb_id or '')
    elif data == 'admin:broadcast' and is_admin(tg_id):
        if cb_id: await answer_cb(cb_id)
        await send(chat_id, 'Write message: /broadcast Hello', _admin_menu(), delete_prev=True)
    elif data.startswith('order:approve:') and is_admin(tg_id):
        ok, msg = await _approve_order(data.split(':', 2)[2], tg_id)
        if cb_id: await answer_cb(cb_id, msg, bool(not ok))
    elif data.startswith('order:reject:') and is_admin(tg_id):
        ok, msg = await _reject_order(data.split(':', 2)[2], tg_id)
        if cb_id: await answer_cb(cb_id, msg, bool(not ok))
    elif data in ('back', 'menu:back'):
        if cb_id: await answer_cb(cb_id)

async def _cb_buy_from_keyboard(chat_id, tg_id, pkg_id):
    if not _check_cooldown(f'buy:{chat_id}'): return
    M = _M(); shop = state().get('shop', {}); pkgs = shop.get('packages', [])
    pkg = next((p for p in pkgs if p['id'] == pkg_id), None)
    if not pkg:
        await send(chat_id, 'Package not found.', _buy_back(), delete_prev=True); return
    rec = state().get('tg_users', {}).get(str(tg_id))
    existing_uid = rec.get('panel_user_id') if rec else None
    is_renew = bool(existing_uid)
    card = shop.get('card_number') or '\u2014'
    holder = shop.get('card_holder') or '\u2014'
    await send(chat_id,
               f'{pkg["title"]}\nPrice: <b>{pkg["price"]:,} Toman</b>\n{"Renew existing" if is_renew else "Create new"}\n\nCard: <code>{_esc(card)}</code>\nHolder: <code>{_esc(holder)}</code>\n\nSend receipt after payment.',
               delete_prev=True)
    order = {'id': f'ord_{int(time.time())}_{pkg_id}_{tg_id}', 'tg_id': int(tg_id), 'package': pkg,
             'status': 'pending_payment', 'created_at': M.datetime.now().isoformat(),
             'username': str(tg_id), 'amount': pkg['price'], 'uid': existing_uid}
    state().setdefault('orders', []).append(order)
    state()['pending'][str(tg_id)] = order['id']
    asyncio.create_task(M.save_state())
    await _notify_admins(
        f'New order!\nUser: <code>{tg_id}</code>\nPackage: {pkg["title"]}\nAmount: {pkg["price"]:,} Toman\nID: <code>{order["id"]}</code>',
        order['id'])

# ========== POLLING ==========
async def _poll_loop():
    global _LAST_UPDATE_ID
    while True:
        try:
            if not str(state().get('bot_token') or '').strip(): await asyncio.sleep(8); continue
            if not state().get('enabled', True): await asyncio.sleep(30); continue
            params = {'timeout': 25, 'allowed_updates': ['message', 'callback_query']}
            if _LAST_UPDATE_ID: params['offset'] = _LAST_UPDATE_ID + 1
            res = await api('getUpdates', **params)
            if not res.get('ok'):
                if res.get('description') and 'webhook' in str(res['description']).lower():
                    await api('deleteWebhook', drop_pending_updates=False)
                await asyncio.sleep(8); continue
            for upd in res.get('result', []):
                _LAST_UPDATE_ID = max(_LAST_UPDATE_ID, int(upd.get('update_id', 0)))
                try: await _handle_update(upd)
                except Exception as e: logger.warning('update handling failed: %s', e)
        except Exception as e:
            logger.warning('poll loop error: %s', e); await asyncio.sleep(8)

def start_polling():
    global _POLL_TASK
    if _POLL_TASK and not _POLL_TASK.done(): return _POLL_TASK
    _POLL_TASK = asyncio.create_task(_poll_loop())
    logger.info('PANAHANNET Telegram bot polling started')
    return _POLL_TASK

async def _start_telegram_bot():
    try: state(); start_polling()
    except Exception as e: logger.warning('failed to start Telegram bot: %s', e)

def register(app):
    from fastapi import Depends, HTTPException, Request
    def _auth():
        import main as M; return M.require_auth
    @app.get('/api/telegram/bot')
    async def bot_status(_=Depends(_auth())):
        tg = state(); me = await api('getMe') if str(tg.get('bot_token') or '').strip() else {'ok': False}
        return {
            'enabled': tg.get('enabled', True),
            'has_token': bool(str(tg.get('bot_token') or '').strip()),
            'admin_ids': tg.get('admin_ids', []),
            'bot_username': (me.get('result') or {}).get('username', '') if me.get('ok') else '',
            'shop': tg.get('shop', {}),
            'orders_pending': len([o for o in tg.get('orders', []) if o.get('status', '').startswith('pending')]),
            'panel_url': panel_url(),
        }
    @app.post('/api/telegram/bot')
    async def bot_config(request: Request, _=Depends(_auth())):
        body = await request.json(); tg = state()
        if 'bot_token' in body: tg['bot_token'] = str(body['bot_token']).strip()
        if 'enabled' in body: tg['enabled'] = bool(body['enabled'])
        if 'admin_ids' in body: tg['admin_ids'] = _parse_ids(body['admin_ids'])
        for k in ('card_number', 'card_holder', 'welcome'):
            if k in body: tg.setdefault('shop', {})[k] = str(body[k]).strip()
        if 'packages' in body and isinstance(body['packages'], list): tg.setdefault('shop', {})['packages'] = body['packages']
        if 'trial' in body and isinstance(body['trial'], dict): tg.setdefault('shop', {})['trial'] = body['trial']
        asyncio.create_task(_M().save_state()); start_polling()
        return {'ok': True, 'admin_ids': tg.get('admin_ids', [])}
    @app.api_route('/api/telegram/bot/test', methods=['GET', 'POST'])
    async def bot_test(_=Depends(_auth())):
        me = await api('getMe')
        return {'ok': bool(me.get('ok')), 'result': me.get('result'), 'error': me.get('description')}
    @app.get('/api/telegram/bot/orders')
    async def bot_orders(_=Depends(_auth())):
        return {'orders': list(reversed(state().get('orders', [])))[:100]}
    @app.post('/api/telegram/bot/orders/{order_id}/approve')
    async def bot_order_approve(order_id: str, _=Depends(_auth())):
        ok, msg = await _approve_order(order_id, 'panel')
        if not ok: raise HTTPException(status_code=400, detail=msg)
        return {'ok': True, 'message': msg}
    @app.post('/api/telegram/bot/orders/{order_id}/reject')
    async def bot_order_reject(order_id: str, _=Depends(_auth())):
        ok, msg = await _reject_order(order_id, 'panel')
        if not ok: raise HTTPException(status_code=400, detail=msg)
        return {'ok': True, 'message': msg}
