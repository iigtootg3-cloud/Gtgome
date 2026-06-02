import asyncio
import json
import logging
import random
import time
from aiohttp import web

from ..database.models import Hero, User, RaidBoss, RaidParticipant
from ..database.session import async_session
from ..services.battle_service import (
    Zone, apply_bleed, apply_vampirism, calculate_damage, format_combat_events,
    BLEED_DURATION, BLEED_DMG_PCT, CRIT_MULTIPLIER, get_monster, get_random_monster,
    hero_stats, scale_monster_to_hero, get_monster_intro_text
)
from ..services.game_service import (
    add_coins, add_hero_experience, ensure_active_hero,
)
from ..services.battle_storage import battle_storage
from ..utils.ui import MG_EMOJI, get_user_display_name
from ..config import settings
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import text, select, update

logger = logging.getLogger(__name__)

active_arena_battles: dict[str, dict] = {}
active_raids: dict[int, dict] = {}

_bot = None

def set_bot(bot) -> None:
    global _bot
    _bot = bot

BOT_TOKEN = settings.BOT_TOKEN
MAX_ROUNDS = 30
WAIT_TIMEOUT = 60
PVE_ROUND_TIMEOUT = 30
PVP_ROUND_TIMEOUT = 30
PVE_CONTINUE_TIMEOUT = 300
PVE_LOG_TTL = 300

RAID_DURATION              = 10800
RAID_ATTEMPT_DURATION      = 1200
RAID_REJOIN_COOLDOWN       = 1200
RAID_ATTACK_COOLDOWN       = 7
RAID_GLOBAL_ABILITY_INTERVAL = 90
RAID_EXP_PER_DMG           = 0.0155
RAID_COINS_PER_DMG         = 0.001
RAID_EXP_CAP               = 200
RAID_TOP5_COINS            = {1: 80, 2: 60, 3: 40, 4: 25, 5: 10}

RAID_BOSS_DEFS = {
    1: {
        "name": "Бог Пустоты",
        "base_hp_per_player": 8_192,
        "max_hp_cap": 3_000_000,
        "base_atk": 75,
        "base_def": 50,
        "image": "https://raw.githubusercontent.com/iigtootg3-cloud/Gtgome/main/IMG_20260601_041447_822.jpg",
        "color": "#7b2fff",
        "abilities": [
            {"id": "curse_abyss",  "name": "Проклятие Бездны", "icon": "🌀", "desc": "Снижает ATK/DEF случайных игроков на 15 сек"},
            {"id": "void_drain",   "name": "Пустотный Разряд", "icon": "⚫", "desc": "Урон всем + лечит себя на 1% max HP"},
            {"id": "shadow_mark",  "name": "Тёмная Метка",     "icon": "🖤", "desc": "Кровотечение на 3 случайных игроков"},
        ],
    },
    2: {
        "name": "Бог Луны",
        "base_hp_per_player": 7_680,
        "max_hp_cap": 2_800_000,
        "base_atk": 60,
        "base_def": 60,
        "image": "https://raw.githubusercontent.com/iigtootg3-cloud/Gtgome/main/IMG_20260601_041452_465.jpg",
        "color": "#4a90d9",
        "abilities": [
            {"id": "lunar_heal",   "name": "Лунное Исцеление",  "icon": "🌙", "desc": "Восстанавливает 5% max HP"},
            {"id": "lunar_dodge",  "name": "Лунное Уклонение",  "icon": "🌟", "desc": "Уклонение +25% на 10 сек"},
            {"id": "moonfall",     "name": "Лунопад",           "icon": "💫", "desc": "Удар по всем, снижает DEF"},
        ],
    },
    3: {
        "name": "Бог Света",
        "base_hp_per_player": 9_216,
        "max_hp_cap": 3_500_000,
        "base_atk": 85,
        "base_def": 55,
        "image": "https://raw.githubusercontent.com/iigtootg3-cloud/Gtgome/main/IMG_20260601_041504_597.jpg",
        "color": "#f5c518",
        "abilities": [
            {"id": "war_fury",     "name": "Ярость Света",      "icon": "⚡", "desc": "ATK +30% на 20 сек"},
            {"id": "blood_whirl",  "name": "Светлый Вихрь",     "icon": "🌪️","desc": "Мощный удар по всем"},
            {"id": "divine_shield","name": "Божественный Щит",  "icon": "🛡️","desc": "DEF +40% на 15 сек + лечение 3% max HP"},
        ],
    },
}

def scale_boss_stats(boss_id: int, player_count: int, avg_level: float) -> dict:
    tmpl = RAID_BOSS_DEFS.get(boss_id, RAID_BOSS_DEFS[1])
    lf = max(1.0, min(avg_level / 15.0, 2.5))
    base_per_player = tmpl.get("base_hp_per_player", 8_192)
    raw_hp = int(base_per_player * max(1, player_count) * lf)
    hp = max(15_000, min(raw_hp, tmpl["max_hp_cap"]))
    atk = int(tmpl["base_atk"] * (1 + (lf - 1) * 0.3))
    def_ = int(tmpl["base_def"] * (1 + (lf - 1) * 0.2))
    return {
        "boss_id": boss_id,
        "name": tmpl["name"],
        "hp": hp,
        "max_hp": hp,
        "atk": atk,
        "def": def_,
        "abilities": tmpl["abilities"],
        "image": tmpl["image"],
        "color": tmpl["color"],
        "atk_buff_until": 0, "atk_buff_pct": 0,
        "dodge_buff_until": 0, "dodge_buff_pct": 0,
        "def_buff_until": 0, "def_buff_pct": 0,
    }

async def _rescale_boss_if_needed(room: dict):
    n = len(room["players"])
    boss = room["boss"]
    if n > 15:
        return
    levels = [p.get("level", 1) for p in room["players"].values()]
    avg_lvl = sum(levels) / max(len(levels), 1)
    scaled = scale_boss_stats(boss["boss_id"], n, avg_lvl)
    already_damaged = boss["max_hp"] - boss["hp"]
    if scaled["hp"] > boss["max_hp"]:
        boss["max_hp"] = scaled["hp"]
        boss["hp"] = max(1, scaled["hp"] - already_damaged)
    boss["atk"] = scaled["atk"]
    boss["def"] = scaled["def"]

def boss_effective_stats(boss: dict) -> tuple[int, int, int]:
    now = time.time()
    atk = boss["atk"]
    def_ = boss["def"]
    dodge = 5
    if boss.get("atk_buff_until", 0) > now:
        atk = int(atk * (1 + boss.get("atk_buff_pct", 0) / 100))
    if boss.get("def_buff_until", 0) > now:
        def_ = int(def_ * (1 + boss.get("def_buff_pct", 0) / 100))
    if boss.get("dodge_buff_until", 0) > now:
        dodge += boss.get("dodge_buff_pct", 0)
    return atk, def_, dodge

def _apply_global_ability(room: dict, ability: dict) -> list[str]:
    log = []
    boss = room["boss"]
    now = time.time()
    aid = ability["id"]
    players = room["players"]

    if aid == "curse_abyss":
        targets = random.sample(list(players.keys()), min(3, len(players)))
        for uid in targets:
            p = players[uid]
            if p.get("alive", True):
                p.setdefault("debuffs", []).append({"type": "atk_def_down", "pct": 15, "until": now + 15})
                log.append(f"🌀 {room['names'].get(uid, str(uid))}: ATK/DEF -15% на 15 сек")

    elif aid == "void_drain":
        for uid, p in players.items():
            if p.get("alive", True):
                dmg = max(10, int(boss["atk"] * 0.35))
                p["hp"] = max(0, p["hp"] - dmg)
                log.append(f"⚫ {room['names'].get(uid, str(uid))}: -{dmg} HP от Пустотного Разряда")
                if p["hp"] <= 0:
                    _kill_player(room, uid)
        heal = min(int(boss["max_hp"] * 0.01), 50_000)
        boss["hp"] = min(boss["max_hp"], boss["hp"] + heal)
        log.append(f"⚫ Бог Пустоты восстановил {heal:,} HP")

    elif aid == "shadow_mark":
        targets = random.sample(list(players.keys()), min(3, len(players)))
        for uid in targets:
            p = players[uid]
            if p.get("alive", True):
                p["bleed_stacks"] = p.get("bleed_stacks", 0) + 2
                log.append(f"🖤 {room['names'].get(uid, str(uid))}: Кровотечение ×2")

    elif aid == "lunar_heal":
        heal = int(boss["max_hp"] * 0.05)
        boss["hp"] = min(boss["max_hp"], boss["hp"] + heal)
        log.append(f"🌙 Бог Луны исцелился на {heal:,} HP")

    elif aid == "lunar_dodge":
        boss["dodge_buff_until"] = now + 10
        boss["dodge_buff_pct"] = 25
        log.append("🌟 Бог Луны: уклонение +25% на 10 сек")

    elif aid == "moonfall":
        for uid, p in players.items():
            if p.get("alive", True):
                dmg = max(10, int(boss["atk"] * 0.30))
                p["hp"] = max(0, p["hp"] - dmg)
                p.setdefault("debuffs", []).append({"type": "def_down", "pct": 10, "until": now + 8})
                log.append(f"💫 {room['names'].get(uid, str(uid))}: -{dmg} HP, DEF -10% на 8 сек")
                if p["hp"] <= 0:
                    _kill_player(room, uid)

    elif aid == "war_fury":
        boss["atk_buff_until"] = now + 20
        boss["atk_buff_pct"] = 30
        log.append("⚡ Бог Света: ATK +30% на 20 сек!")

    elif aid == "blood_whirl":
        for uid, p in players.items():
            if p.get("alive", True):
                dmg = max(15, int(boss["atk"] * 0.50))
                p["hp"] = max(0, p["hp"] - dmg)
                log.append(f"🌪️ {room['names'].get(uid, str(uid))}: -{dmg} HP от Светлого Вихря")
                if p["hp"] <= 0:
                    _kill_player(room, uid)

    elif aid == "divine_shield":
        boss["def_buff_until"] = now + 15
        boss["def_buff_pct"] = 40
        heal = int(boss["max_hp"] * 0.03)
        boss["hp"] = min(boss["max_hp"], boss["hp"] + heal)
        log.append(f"🛡️ Бог Света: DEF +40% на 15 сек, лечение +{heal:,} HP")

    return log

def _kill_player(room: dict, uid: int):
    p = room["players"].get(uid)
    if p is None or not p.get("alive", True):
        return
    p["alive"] = False
    p["hp"] = 0
    p["death_time"] = time.time()
    room["rejoin_cooldowns"][uid] = time.time() + RAID_REJOIN_COOLDOWN
    name = room["names"].get(uid, str(uid))
    room["log"].append(f"💀 {name} пал в бою!")

async def _persist_log_if_needed(room: dict):
    room.setdefault("_log_last_persisted", 0)
    if len(room["log"]) - room["_log_last_persisted"] < 10:
        return
    room["_log_last_persisted"] = len(room["log"])
    try:
        async with async_session() as session:
            await session.execute(
                text("UPDATE raid_bosses SET log_json = :log WHERE id = :id"),
                {"log": json.dumps(room["log"][-100:], ensure_ascii=False), "id": room["raid_id"]}
            )
            await session.commit()
    except Exception as e:
        logger.error("Log persist error: %s", e)

async def ws_arena_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    mode      = request.query.get("mode", "pvp")
    battle_id = request.query.get("battle_id", "")
    user_id   = request.query.get("user_id", "")
    chat_id   = request.query.get("chat_id", "0")

    if not battle_id or not user_id:
        await ws.close(code=1008, message="Missing parameters")
        return ws
    try:
        user_id_int = int(user_id)
    except ValueError:
        await ws.close(code=1008, message="Invalid user_id")
        return ws

    try:
        first_msg = await ws.receive(timeout=10)
    except asyncio.TimeoutError:
        await ws.close(code=1008, message="Auth timeout")
        return ws
    if first_msg.type != web.WSMsgType.TEXT:
        await ws.close(code=1008, message="Expected auth")
        return ws
    try:
        json.loads(first_msg.data)
    except json.JSONDecodeError:
        await ws.close(code=1008, message="Invalid auth")
        return ws

    if mode == "pve":
        await _handle_pve(ws, battle_id, user_id_int, chat_id)
    elif mode == "raid":
        try:
            raid_id = int(battle_id)
        except ValueError:
            await ws.close(code=1008, message="Invalid raid_id")
            return ws
        await _handle_raid(ws, raid_id, user_id_int, int(chat_id))
    else:
        await _handle_pvp(ws, battle_id, user_id_int, chat_id)

    return ws

# ============================ РЕЙДЫ ============================
async def _handle_raid(ws, raid_id: int, user_id_int: int, chat_id: int):
    async with async_session() as session:
        result = await session.execute(select(RaidBoss).where(RaidBoss.id == raid_id))
        raid_row = result.scalar_one_or_none()

    if not raid_row or not raid_row.active:
        await ws.send_json({"type": "error", "message": "Рейд не найден или завершён."})
        await ws.close(code=4004)
        return

    if raid_id not in active_raids:
        boss_def = RAID_BOSS_DEFS.get(raid_row.boss_id, RAID_BOSS_DEFS[1])
        start_ts = raid_row.created_at.timestamp() if raid_row.created_at else time.time()
        boss = {
            "boss_id": raid_row.boss_id,
            "name": raid_row.name,
            "hp": raid_row.hp,
            "max_hp": raid_row.max_hp,
            "atk": raid_row.atk,
            "def": raid_row.def_val,
            "image": boss_def["image"],
            "color": boss_def["color"],
            "abilities": boss_def["abilities"],
            "atk_buff_until": 0, "atk_buff_pct": 0,
            "dodge_buff_until": 0, "dodge_buff_pct": 0,
            "def_buff_until": 0, "def_buff_pct": 0,
        }
        active_raids[raid_id] = {
            "raid_id": raid_id,
            "chat_id": chat_id,
            "boss": boss,
            "players": {},
            "connections": {},
            "names": {},
            "cooldowns": {},
            "rejoin_cooldowns": {},
            "total_damage": {},
            "player_exp_earned": {},
            "log": [],
            "_log_last_persisted": 0,
            "finished": False,
            "start_time": start_ts,
            "next_ability_at": time.time() + RAID_GLOBAL_ABILITY_INTERVAL,
            "last_hit_uid": None,
        }
        room = active_raids[raid_id]

        async with async_session() as session:
            result = await session.execute(
                select(RaidParticipant).where(RaidParticipant.raid_id == raid_id)
            )
            for p in result.scalars().all():
                if p.damage:
                    room["total_damage"][p.user_id] = p.damage

        asyncio.create_task(_raid_global_ability_loop(room))
        asyncio.create_task(_raid_timeout_task(room))
        logger.info("Raid room created in memory: raid_id=%s", raid_id)
    
    room = active_raids[raid_id]

    if room.get("finished"):
        await ws.send_json({"type": "raid_over", "message": "Рейд уже завершён."})
        await ws.close(code=1000)
        return

    user_name = await get_user_display_name(user_id_int)
    room["names"][user_id_int] = user_name

    existing = room["players"].get(user_id_int)
    if existing is None:
        from ..services.game_service import get_active_hero
        result = await get_active_hero(user_id_int)
        if result is None:
            await ws.send_json({"type": "error", "message": "Нет активного героя!"})
            await ws.close(code=4004)
            return
        user_hero, hero_obj = result
        stats = hero_stats(user_hero, hero_obj)
        room["players"][user_id_int] = {
            **stats,
            "alive": True,
            "death_time": 0,
            "debuffs": [],
            "bleed_stacks": 0,
        }
        room["total_damage"][user_id_int] = 0
        room["player_exp_earned"][user_id_int] = 0
        room["cooldowns"][user_id_int] = 0
        await _rescale_boss_if_needed(room)
        _add_log(room, f"⚔️ {user_name} вступил в рейд!")
        asyncio.create_task(_attempt_timeout_task(room, user_id_int))

    elif not existing.get("alive", True):
        rejoin_ready = room["rejoin_cooldowns"].get(user_id_int, 0)
        now = time.time()
        if now >= rejoin_ready:
            from ..services.game_service import get_active_hero
            result = await get_active_hero(user_id_int)
            if result:
                user_hero, hero_obj = result
                stats = hero_stats(user_hero, hero_obj)
                room["players"][user_id_int] = {
                    **stats,
                    "alive": True,
                    "death_time": 0,
                    "debuffs": [],
                    "bleed_stacks": 0,
                }
            else:
                existing["alive"] = True
                existing["hp"] = existing["max_hp"]
                existing["debuffs"] = []
                existing["bleed_stacks"] = 0
            room["cooldowns"][user_id_int] = 0
            _add_log(room, f"⚔️ {user_name} вернулся в бой!")
            asyncio.create_task(_attempt_timeout_task(room, user_id_int))

    room["connections"][user_id_int] = ws

    await _send_raid_state(room, ws, user_id_int)

    await _raid_broadcast(room, {
        "type": "player_joined",
        "user_id": user_id_int,
        "name": user_name,
        "players": _build_players_payload(room),
    })

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                await _handle_raid_message(room, user_id_int, msg.data)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        await asyncio.sleep(30)
        current_ws = room["connections"].get(user_id_int)
        if current_ws is ws:
            room["connections"].pop(user_id_int, None)

def _add_log(room: dict, line: str):
    room["log"].append(line)
    if len(room["log"]) > 500:
        room["log"] = room["log"][-500:]

async def _send_raid_state(room: dict, ws, user_id: int):
    boss = room["boss"]
    player = room["players"].get(user_id, {})
    now = time.time()
    cd_left = max(0.0, room["cooldowns"].get(user_id, 0) + RAID_ATTACK_COOLDOWN - now)
    rejoin_ready = room["rejoin_cooldowns"].get(user_id, 0)
    rejoin_cd_left = max(0.0, rejoin_ready - now)

    await ws.send_json({
        "type": "raid_state",
        "boss": {
            "name": boss["name"],
            "hp": boss["hp"],
            "max_hp": boss["max_hp"],
            "image": boss["image"],
            "color": boss["color"],
            "boss_id": boss["boss_id"],
            "abilities": [{"id": a["id"], "name": a["name"], "icon": a["icon"]} for a in boss["abilities"]],
        },
        "my_player": {
            "hp": player.get("hp", 0),
            "max_hp": player.get("max_hp", 1),
            "alive": player.get("alive", True),
            "damage_dealt": room["total_damage"].get(user_id, 0),
            "exp_earned": room["player_exp_earned"].get(user_id, 0),
            "rejoin_cd_left": rejoin_cd_left,
            "respawn_in": rejoin_cd_left,
        },
        "players": _build_players_payload(room),
        "leaderboard": _build_leaderboard(room),
        "attack_cooldown": cd_left,
        "next_ability_in": max(0.0, room["next_ability_at"] - now),
        "log": room["log"][-40:],
        "finished": room.get("finished", False),
        "time_left": max(0, RAID_DURATION - int(now - room["start_time"])),
    })

def _build_players_payload(room: dict) -> list:
    return [
        {
            "user_id": uid,
            "name": room["names"].get(uid, str(uid)),
            "hp": p.get("hp", 0),
            "max_hp": p.get("max_hp", 1),
            "alive": p.get("alive", True),
            "damage": room["total_damage"].get(uid, 0),
        }
        for uid, p in room["players"].items()
    ]

def _build_leaderboard(room: dict) -> list:
    items = [
        {"user_id": uid, "name": room["names"].get(uid, str(uid)), "damage": dmg}
        for uid, dmg in room["total_damage"].items()
    ]
    items.sort(key=lambda x: x["damage"], reverse=True)
    return items[:5]

async def _handle_raid_message(room: dict, user_id: int, raw: str):
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    action = data.get("action")
    if action == "raid_attack":
        await _handle_raid_attack(room, user_id, data)
    elif action == "raid_ping":
        ws = room["connections"].get(user_id)
        if ws and not ws.closed:
            await ws.send_json({"type": "raid_pong"})

async def _maybe_update_raid_message(room: dict):
    now = time.time()
    last_update = room.get("_last_msg_update", 0)
    if now - last_update < 10:
        return
    room["_last_msg_update"] = now

    msg_id = room.get("announcement_msg_id")
    if not msg_id or not _bot:
        return

    boss = room["boss"]
    top = sorted(room["total_damage"].items(), key=lambda x: x[1], reverse=True)[:5]
    top_lines = []
    for i, (uid, dmg) in enumerate(top):
        name = room["names"].get(uid, str(uid))
        medal = ["🥇", "🥈", "🥉", "4.", "5."][i]
        top_lines.append(f"{medal} {name} — {dmg:,} урона")

    hp_pct = boss["hp"] / boss["max_hp"] * 100
    text = (
        f"⚔️ <b>{boss['name']}</b>\n"
        f"❤️ HP: {boss['hp']:,} / {boss['max_hp']:,} ({hp_pct:.1f}%)\n"
        f"⚔️ ATK: {boss['atk']}  🛡 DEF: {boss['def']}\n\n"
        f"🏆 <b>Топ-5:</b>\n" + "\n".join(top_lines) +
        f"\n\nНажмите кнопку, чтобы вступить в бой:"
    )
    try:
        await _bot.edit_message_caption(
            chat_id=int(room["chat_id"]),
            message_id=msg_id,
            caption=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"⚔️ Войти в рейд (25⚡)", callback_data=f"raid_enter:{room['raid_id']}:energy")],
                [InlineKeyboardButton(text=f"💰 Войти за 50 🌟", callback_data=f"raid_enter:{room['raid_id']}:coins")],
            ])
        )
    except Exception:
        try:
            await _bot.edit_message_text(
                chat_id=int(room["chat_id"]),
                message_id=msg_id,
                text=text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"⚔️ Войти в рейд (25⚡)", callback_data=f"raid_enter:{room['raid_id']}:energy")],
                    [InlineKeyboardButton(text=f"💰 Войти за 50 🌟", callback_data=f"raid_enter:{room['raid_id']}:coins")],
                ])
            )
        except Exception:
            pass

async def _handle_raid_attack(room: dict, user_id: int, data: dict):
    if room.get("finished"):
        return

    player = room["players"].get(user_id)
    if player is None or not player.get("alive", True):
        ws = room["connections"].get(user_id)
        if ws and not ws.closed:
            await ws.send_json({"type": "error", "message": "Вы выбыли из боя."})
        return

    now = time.time()
    last_atk = room["cooldowns"].get(user_id, 0)
    cd_left = last_atk + RAID_ATTACK_COOLDOWN - now
    if cd_left > 0:
        ws = room["connections"].get(user_id)
        if ws and not ws.closed:
            await ws.send_json({"type": "error", "message": f"Кулдаун: ещё {cd_left:.1f} сек."})
        return

    atk_zone_str = data.get("atk_zone", "MIDDLE")
    blk_zone_str = data.get("blk_zone", "MIDDLE")
    if atk_zone_str not in ("TOP", "MIDDLE", "BOTTOM"):
        atk_zone_str = "MIDDLE"
    if blk_zone_str not in ("TOP", "MIDDLE", "BOTTOM"):
        blk_zone_str = "MIDDLE"

    room["cooldowns"][user_id] = now

    boss = room["boss"]
    atk_zone = Zone[atk_zone_str]
    blk_zone = Zone[blk_zone_str]
    boss_atk_eff, boss_def_eff, boss_dodge = boss_effective_stats(boss)

    player_atk_mod = 1.0
    player_def_mod = 1.0
    active_debuffs = []
    for d in player.get("debuffs", []):
        if d.get("until", 0) > now:
            active_debuffs.append(d)
            if d["type"] == "atk_def_down":
                player_atk_mod *= (1 - d["pct"] / 100)
                player_def_mod *= (1 - d["pct"] / 100)
            elif d["type"] == "def_down":
                player_def_mod *= (1 - d["pct"] / 100)
    player["debuffs"] = active_debuffs

    player_attacker = {
        "atk": max(1, int(player.get("atk", 20) * player_atk_mod)),
        "crit": player.get("crit", 5),
        "zone": atk_zone,
        "armor_pen": player.get("armor_pen", 0),
        "combo_chance": player.get("combo_chance", 0),
        "bleed_chance": player.get("bleed_chance", 0),
        "vampirism_pct": player.get("vampirism_pct", 0),
        "fury_threshold": player.get("fury_threshold", 0),
        "hp": player.get("hp", 1),
        "max_hp": player.get("max_hp", 1),
    }
    boss_defender = {
        "def": boss_def_eff,
        "dodge": boss_dodge,
        "hp": boss["hp"],
        "max_hp": boss["max_hp"],
        "bleed_stacks": 0,
    }
    boss_atk_zone = random.choice([Zone.TOP, Zone.MIDDLE, Zone.BOTTOM])
    boss_blk_zone = random.choice([Zone.TOP, Zone.MIDDLE, Zone.BOTTOM])

    p_dmg, p_blocked, p_event = calculate_damage(
        attacker=player_attacker, defender=boss_defender,
        block_zone=boss_blk_zone, attacker_zone=atk_zone
    )
    p_dmg = max(1, p_dmg)

    boss_attacker = {
        "atk": boss_atk_eff,
        "crit": 10,
        "zone": boss_atk_zone,
        "armor_pen": 5,
        "combo_chance": 0,
        "bleed_chance": 0,
        "vampirism_pct": 0,
        "fury_threshold": 0,
        "hp": boss["hp"],
        "max_hp": boss["max_hp"],
    }
    player_defender = {
        "def": max(1, int(player.get("def", 10) * player_def_mod)),
        "dodge": player.get("dodge", 5),
        "hp": player["hp"],
        "max_hp": player["max_hp"],
        "bleed_stacks": player.get("bleed_stacks", 0),
    }
    b_dmg, b_blocked, b_event = calculate_damage(
        attacker=boss_attacker, defender=player_defender,
        block_zone=blk_zone, attacker_zone=boss_atk_zone
    )
    b_dmg = max(1, b_dmg)

    boss["hp"] = max(0, boss["hp"] - p_dmg)
    player["hp"] = max(0, player["hp"] - b_dmg)

    player_died_from_hit = player["hp"] <= 0

    vamp = apply_vampirism(player_attacker, p_dmg)
    if vamp and not player_died_from_hit:
        player["hp"] = min(player["max_hp"], player["hp"] + vamp)

    bleed_dmg, _ = apply_bleed(player)
    if bleed_dmg:
        player["hp"] = max(0, player["hp"] - bleed_dmg)

    room["total_damage"][user_id] = room["total_damage"].get(user_id, 0) + p_dmg
    room["last_hit_uid"] = user_id

    already_exp = room["player_exp_earned"].get(user_id, 0)
    if already_exp < RAID_EXP_CAP:
        gained_exp = min(int(p_dmg * RAID_EXP_PER_DMG), RAID_EXP_CAP - already_exp)
        if gained_exp > 0:
            room["player_exp_earned"][user_id] = already_exp + gained_exp
            try:
                await add_hero_experience(user_id, gained_exp)
            except Exception as e:
                logger.error("Raid exp error uid=%s: %s", user_id, e)

    try:
        async with async_session() as session:
            await session.execute(
                text("UPDATE raid_bosses SET hp = :hp WHERE id = :id"),
                {"hp": boss["hp"], "id": room["raid_id"]}
            )
            await session.execute(
                text("""
                    INSERT INTO raid_participants (raid_id, user_id, damage, attacks, last_attack)
                    VALUES (:rid, :uid, :dmg, 1, NOW())
                    ON CONFLICT (raid_id, user_id)
                    DO UPDATE SET damage = raid_participants.damage + :dmg,
                                  attacks = raid_participants.attacks + 1,
                                  last_attack = NOW()
                """),
                {"rid": room["raid_id"], "uid": user_id, "dmg": p_dmg}
            )
            await session.commit()
    except Exception as e:
        logger.error("Raid DB update error: %s", e)

    name = room["names"].get(user_id, str(user_id))

    log_pos_before = len(room["log"])

    log_line = (
        f"⚔️ {name}: -{p_dmg} HP боссу"
        f"{' 💥КРИТ!' if p_event.get('crit') else ''}"
        f" | Босс: -{b_dmg}"
        f"{' (блок)' if b_blocked else ''}"
    )
    if bleed_dmg:
        log_line += f" | 🩸 -{bleed_dmg}"
    _add_log(room, log_line)
    if vamp and not player_died_from_hit:
        _add_log(room, f"🧛 Вампиризм {name}: +{vamp} HP")

    player_dead = player["hp"] <= 0
    boss_dead = boss["hp"] <= 0

    if player_dead:
        _kill_player(room, user_id)

    await _maybe_update_raid_message(room)

    await _persist_log_if_needed(room)

    new_log_lines = room["log"][log_pos_before:]

    ws = room["connections"].get(user_id)
    if ws and not ws.closed:
        await ws.send_json({
            "type": "raid_attack_result",
            "dmg_to_boss": p_dmg,
            "dmg_to_player": b_dmg,
            "crit": p_event.get("crit", False),
            "blocked": b_blocked,
            "vamp": vamp,
            "bleed": bleed_dmg,
            "my_hp": player["hp"],
            "my_max_hp": player["max_hp"],
            "boss_hp": boss["hp"],
            "boss_max_hp": boss["max_hp"],
            "alive": not player_dead,
            "attack_cooldown": RAID_ATTACK_COOLDOWN,
            "total_damage": room["total_damage"][user_id],
            "exp_earned": room["player_exp_earned"].get(user_id, 0),
            "rejoin_cooldown": RAID_REJOIN_COOLDOWN if player_dead else 0,
        })

    now_ts = time.time()
    next_ab_in = max(0, room["next_ability_at"] - now_ts)
    await _raid_broadcast_except(room, user_id, {
        "type": "raid_update",
        "boss_hp": boss["hp"],
        "boss_max_hp": boss["max_hp"],
        "players": _build_players_payload(room),
        "leaderboard": _build_leaderboard(room),
        "log": new_log_lines,
        "attacker": {"user_id": user_id, "name": name, "dmg": p_dmg},
        "next_ability_in": int(next_ab_in),
    })
    ws_self = room["connections"].get(user_id)
    if ws_self and not ws_self.closed:
        try:
            await ws_self.send_json({
                "type": "raid_update",
                "boss_hp": boss["hp"],
                "boss_max_hp": boss["max_hp"],
                "players": _build_players_payload(room),
                "leaderboard": _build_leaderboard(room),
                "next_ability_in": int(next_ab_in),
            })
        except Exception:
            pass

    if boss_dead:
        await _finish_raid(room, killed=True)
    elif player_dead:
        if ws and not ws.closed:
            await ws.send_json({
                "type": "attempt_expired",
                "rejoin_cooldown": RAID_REJOIN_COOLDOWN,
                "message": "Вы пали в бою. Войдите снова через 20 минут через кнопку в чате.",
            })

async def _attempt_timeout_task(room: dict, user_id: int):
    try:
        await asyncio.sleep(RAID_ATTEMPT_DURATION)
        player = room["players"].get(user_id)
        if player and player.get("alive", True) and not room.get("finished"):
            _kill_player(room, user_id)
            name = room["names"].get(user_id, str(user_id))
            _add_log(room, f"⏰ {name}: время попытки истекло")
            ws = room["connections"].get(user_id)
            if ws and not ws.closed:
                await ws.send_json({
                    "type": "attempt_expired",
                    "rejoin_cooldown": RAID_REJOIN_COOLDOWN,
                    "message": "Время вашей попытки истекло (20 мин). Войдите снова через кнопку в чате.",
                })
            await _raid_broadcast(room, {
                "type": "raid_update",
                "boss_hp": room["boss"]["hp"],
                "boss_max_hp": room["boss"]["max_hp"],
                "players": _build_players_payload(room),
                "leaderboard": _build_leaderboard(room),
                "log": room["log"][-3:],
            })
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Attempt timeout error uid=%s: %s", user_id, e)

async def _raid_global_ability_loop(room: dict):
    try:
        while not room.get("finished"):
            now = time.time()
            wait = room["next_ability_at"] - now
            if wait > 0:
                await asyncio.sleep(wait)
            if room.get("finished"):
                break

            boss = room["boss"]
            ability = random.choice(boss["abilities"])
            log_lines = _apply_global_ability(room, ability)

            for line in log_lines:
                _add_log(room, line)

            room["next_ability_at"] = time.time() + RAID_GLOBAL_ABILITY_INTERVAL

            await _persist_log_if_needed(room)

            await _raid_broadcast(room, {
                "type": "boss_ability",
                "ability": {
                    "id": ability["id"],
                    "name": ability["name"],
                    "icon": ability["icon"],
                    "desc": ability["desc"],
                },
                "log": [l for l in log_lines if l],
                "boss_hp": boss["hp"],
                "boss_max_hp": boss["max_hp"],
                "players": _build_players_payload(room),
                "next_ability_in": RAID_GLOBAL_ABILITY_INTERVAL,
            })

            just_killed_threshold = time.time() - 5
            for uid, p in list(room["players"].items()):
                if not p.get("alive", True) and p.get("death_time", 0) >= just_killed_threshold:
                    ws = room["connections"].get(uid)
                    if ws and not ws.closed:
                        await ws.send_json({
                            "type": "player_dead",
                            "respawn_in": RAID_REJOIN_COOLDOWN,
                            "message": "Вы пали от способности босса. Войдите снова через кнопку в чате.",
                        })

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Global ability loop error: %s", e)

async def _raid_timeout_task(room: dict):
    try:
        remaining = max(0, RAID_DURATION - int(time.time() - room["start_time"]))
        await asyncio.sleep(remaining)
        if not room.get("finished"):
            await _finish_raid(room, killed=False, timeout=True)
    except asyncio.CancelledError:
        pass

async def _finish_raid(room: dict, killed: bool = False, timeout: bool = False):
    if room.get("finished"):
        return
    room["finished"] = True

    boss = room["boss"]
    raid_id = room["raid_id"]
    chat_id = room["chat_id"]
    last_hit_uid = room.get("last_hit_uid")

    try:
        async with async_session() as session:
            await session.execute(
                text("UPDATE raid_bosses SET active = FALSE, hp = :hp WHERE id = :id"),
                {"hp": boss["hp"], "id": raid_id}
            )
            await session.commit()
    except Exception as e:
        logger.error("Raid deactivate error: %s", e)

    sorted_damage = sorted(room["total_damage"].items(), key=lambda x: x[1], reverse=True)

    rewards: dict[int, dict] = {}
    for rank_0, (uid, dmg) in enumerate(sorted_damage):
        rank = rank_0 + 1
        if dmg == 0:
            continue
        if killed:
            coins = int(dmg * RAID_COINS_PER_DMG)
            coins += RAID_TOP5_COINS.get(rank, 0)
        else:
            coins = 0
        rewards[uid] = {
            "coins": coins,
            "rank": rank,
            "last_hit": uid == last_hit_uid,
            "exp_earned": room["player_exp_earned"].get(uid, 0),
        }

    for uid, r in rewards.items():
        if r["coins"] > 0:
            try:
                await add_coins(uid, r["coins"])
            except Exception as e:
                logger.error("Raid coins error uid=%s: %s", uid, e)

    if not killed:
        for uid in room["total_damage"]:
            try:
                async with async_session() as session:
                    await session.execute(
                        text("UPDATE users SET energy = LEAST(max_energy, energy + 12) WHERE telegram_id = :uid"),
                        {"uid": uid}
                    )
                    await session.commit()
            except Exception as e:
                logger.error("Consolation energy error uid=%s: %s", uid, e)

    if _bot:
        for uid, r in rewards.items():
            dmg = room["total_damage"].get(uid, 0)
            try:
                last_hit_str = ""
                if r.get("last_hit"):
                    last_hit_str = "\n🗡️ Последний удар!"
                if killed:
                    rank_emoji = "🥇" if r['rank'] == 1 else "🥈" if r['rank'] == 2 else "🥉" if r['rank'] == 3 else f"#{r['rank']}"
                    top_bonus = RAID_TOP5_COINS.get(r['rank'], 0)
                    bonus_line = f"\n🎖️ Бонус топ-{r['rank']}: +{top_bonus} {MG_EMOJI}" if top_bonus else ""
                    personal_msg = (
                        f"{rank_emoji} <b>Рейд завершён!</b>\n\n"
                        f"💥 Нанесено урона: {dmg:,}\n"
                        f"✨ Опыт за рейд: {r['exp_earned']}/{RAID_EXP_CAP}"
                        f"{bonus_line}"
                        f"{last_hit_str}\n"
                        f"💰 Магатамы: +{r['coins']} {MG_EMOJI}"
                    )
                else:
                    personal_msg = (
                        f"💀 <b>Рейд провален!</b>\n\n"
                        f"💥 Ваш урон: {dmg:,}\n"
                        f"✨ Опыт за рейд: {r['exp_earned']}/{RAID_EXP_CAP}\n"
                        f"⚡ Возвращено 12 энергии."
                    )
                await _bot.send_message(uid, personal_msg, parse_mode="HTML")
            except Exception as e:
                logger.error("Personal raid result error uid=%s: %s", uid, e)

        try:
            if killed:
                top_lines = []
                for rank_0, (uid, dmg) in enumerate(sorted_damage[:10]):
                    rank = rank_0 + 1
                    name = room["names"].get(uid, str(uid))
                    r = rewards.get(uid, {})
                    medal = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else f"{rank}."
                    top_lines.append(f"{medal} {name} — {dmg:,} урона (+{r.get('coins', 0)} {MG_EMOJI})")
                last_hit_name = room["names"].get(last_hit_uid, "—") if last_hit_uid else "—"
                chat_msg = (
                    f"⚔️ <b>{boss['name']} ПОВЕРЖЕН!</b>\n\n"
                    f"🗡️ Последний удар: <b>{last_hit_name}</b>\n\n"
                    f"🏆 <b>Топ по урону:</b>\n" + "\n".join(top_lines)
                )
            else:
                chat_msg = (
                    f"🌑 <b>Мир поработил {boss['name']}!</b>\n\n"
                    "Никто не смог одолеть тёмного бога…\n"
                    "Всем участникам возвращено 12⚡ энергии."
                )
            await _bot.send_message(int(chat_id), chat_msg, parse_mode="HTML")
        except Exception as e:
            logger.error("Raid chat result error: %s", e)

    await _raid_broadcast(room, {
        "type": "raid_over",
        "killed": killed,
        "timeout": timeout,
        "boss_name": boss["name"],
        "leaderboard": [
            {
                "rank": rank_0 + 1,
                "user_id": uid,
                "name": room["names"].get(uid, str(uid)),
                "damage": dmg,
                "reward": rewards.get(uid, {}),
            }
            for rank_0, (uid, dmg) in enumerate(sorted_damage)
        ],
    })

    for ws in list(room["connections"].values()):
        if not ws.closed:
            try:
                await ws.close(code=1000)
            except Exception:
                pass

    async def _cleanup():
        await asyncio.sleep(300)
        active_raids.pop(raid_id, None)
    asyncio.create_task(_cleanup())

async def _raid_broadcast(room: dict, payload: dict):
    data = json.dumps(payload, ensure_ascii=False)
    for ws in list(room["connections"].values()):
        if not ws.closed:
            try:
                await ws.send_str(data)
            except Exception as e:
                logger.warning("Raid broadcast error: %s", e)

async def _raid_broadcast_except(room: dict, exclude_uid: int, payload: dict):
    data = json.dumps(payload, ensure_ascii=False)
    for uid, ws in list(room["connections"].items()):
        if uid == exclude_uid:
            continue
        if not ws.closed:
            try:
                await ws.send_str(data)
            except Exception as e:
                logger.warning("Raid broadcast_except error: %s", e)

async def create_raid_room(raid_id: int, chat_id: int, boss_id: int):
    async with async_session() as session:
        result = await session.execute(select(RaidBoss).where(RaidBoss.id == raid_id))
        raid_row = result.scalar_one_or_none()
    if not raid_row:
        return None

    boss_def = RAID_BOSS_DEFS.get(boss_id, RAID_BOSS_DEFS[1])
    start_ts = raid_row.created_at.timestamp() if raid_row.created_at else time.time()
    boss = {
        "boss_id": boss_id,
        "name": raid_row.name,
        "hp": raid_row.hp,
        "max_hp": raid_row.max_hp,
        "atk": raid_row.atk,
        "def": raid_row.def_val,
        "image": boss_def["image"],
        "color": boss_def["color"],
        "abilities": boss_def["abilities"],
        "atk_buff_until": 0, "atk_buff_pct": 0,
        "dodge_buff_until": 0, "dodge_buff_pct": 0,
        "def_buff_until": 0, "def_buff_pct": 0,
    }
    room = {
        "raid_id": raid_id,
        "chat_id": chat_id,
        "boss": boss,
        "players": {},
        "connections": {},
        "names": {},
        "cooldowns": {},
        "rejoin_cooldowns": {},
        "total_damage": {},
        "player_exp_earned": {},
        "log": [],
        "_log_seq": set(),
        "_log_last_persisted": 0,
        "finished": False,
        "start_time": start_ts,
        "next_ability_at": time.time() + RAID_GLOBAL_ABILITY_INTERVAL,
        "last_hit_uid": None,
    }

    async with async_session() as session:
        result = await session.execute(
            select(RaidParticipant).where(RaidParticipant.raid_id == raid_id)
        )
        for p in result.scalars().all():
            if p.damage:
                room["total_damage"][p.user_id] = p.damage

    active_raids[raid_id] = room
    asyncio.create_task(_raid_global_ability_loop(room))
    asyncio.create_task(_raid_timeout_task(room))
    return room

# ============================ PvP (исправленная версия) ============================
async def _handle_pvp(ws, battle_id, user_id_int, chat_id):
    pvp_key = f"pvp:{chat_id}:{battle_id}"
    b = await battle_storage.get_battle(pvp_key)
    if not b or user_id_int not in (b.get("challenger"), b.get("target")):
        await ws.close(code=4004, message="Not a participant")
        return

    if battle_id not in active_arena_battles:
        if not b.get("player_a") or not b.get("player_b"):
            await ws.close(code=4004, message="Battle data corrupted")
            return
        challenger_name = await get_user_display_name(b["challenger"])
        target_name = await get_user_display_name(b["target"])
        active_arena_battles[battle_id] = {
            "player_a": b["player_a"], "player_b": b["player_b"],
            "turn": 0,
            "a_atk_zone": None, "a_blk_zone": None,
            "b_atk_zone": None, "b_blk_zone": None,
            "challenger": b["challenger"], "target": b["target"],
            "stake": b["stake"], "battle_id": battle_id, "chat_id": chat_id,
            "connections": {},
            "names": {b["challenger"]: challenger_name, b["target"]: target_name},
            "log": [], "finished": False, "mode": "pvp",
            "lock": asyncio.Lock(),
            "refunded": False,
            "started": False,
            "_wait_timeout_task": None,
        }

    room = active_arena_battles[battle_id]
    real_name = await get_user_display_name(user_id_int)
    room["names"][user_id_int] = real_name

    old_ws = room["connections"].get(user_id_int)
    if old_ws and not old_ws.closed:
        try:
            await old_ws.close(code=1000)
        except Exception:
            pass
    room["connections"][user_id_int] = ws

    if len(room["connections"]) < 2:
        await ws.send_json({"type": "waiting", "message": "Ожидаем соперника…"})

        if room["_wait_timeout_task"] is None or room["_wait_timeout_task"].done():
            async def _wait_timeout():
                await asyncio.sleep(WAIT_TIMEOUT)
                async with room["lock"]:
                    if room.get("finished") or room.get("refunded") or room.get("started"):
                        return
                    if len(room["connections"]) < 2:
                        room["refunded"] = True
                        try:
                            await add_coins(b["challenger"], b["stake"])
                            await add_coins(b["target"], b["stake"])
                        except Exception as e:
                            logger.error("Timeout refund error: %s", e)
                        for ws_ in list(room["connections"].values()):
                            if not ws_.closed:
                                try:
                                    await ws_.send_json({"type": "error", "message": "Противник не явился."})
                                    await ws_.close(code=1000)
                                except Exception:
                                    pass
                        active_arena_battles.pop(battle_id, None)
                        await battle_storage.delete_battle(pvp_key)

            room["_wait_timeout_task"] = asyncio.create_task(_wait_timeout())
    elif not room.get("started"):
        room["started"] = True
        pa, pb = room["player_a"], room["player_b"]
        await _broadcast(room, {
            "type": "battle_start",
            "players": [
                {"user_id": room["challenger"], "name": room["names"][room["challenger"]], "hp": pa["hp"], "max_hp": pa["max_hp"]},
                {"user_id": room["target"], "name": room["names"][room["target"]], "hp": pb["hp"], "max_hp": pb["max_hp"]},
            ],
            "round_timeout": PVP_ROUND_TIMEOUT,
        })
        room["_round_timer_task"] = asyncio.create_task(_pvp_round_timer(room))

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                await _handle_pvp_message(room, user_id_int, msg.data)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        await asyncio.sleep(30)
        current_ws = room["connections"].get(user_id_int)
        if current_ws is ws:
            room["connections"].pop(user_id_int, None)



async def _pvp_round_timer(room: dict):
    try:
        await asyncio.sleep(PVP_ROUND_TIMEOUT)
        if room.get("finished"):
            return
        zones = [Zone.TOP, Zone.MIDDLE, Zone.BOTTOM]
        if room["a_atk_zone"] is None:
            room["a_atk_zone"] = random.choice(zones).value
        if room["a_blk_zone"] is None:
            room["a_blk_zone"] = random.choice(zones).value
        if room["b_atk_zone"] is None:
            room["b_atk_zone"] = random.choice(zones).value
        if room["b_blk_zone"] is None:
            room["b_blk_zone"] = random.choice(zones).value
        await _resolve_arena_round(room)
    except asyncio.CancelledError:
        pass
async def _handle_pvp_message(room: dict, user_id: int, raw: str):
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    action = data.get("action")
    zone = data.get("zone")
    if action not in ("atk", "blk") or zone not in ("TOP", "MIDDLE", "BOTTOM"):
        return

    if user_id == room["challenger"]:
        if action == "atk" and room["a_atk_zone"] is None:
            room["a_atk_zone"] = zone
        elif action == "blk" and room["a_blk_zone"] is None:
            room["a_blk_zone"] = zone
    elif user_id == room["target"]:
        if action == "atk" and room["b_atk_zone"] is None:
            room["b_atk_zone"] = zone
        elif action == "blk" and room["b_blk_zone"] is None:
            room["b_blk_zone"] = zone

    if None not in (room["a_atk_zone"], room["a_blk_zone"], room["b_atk_zone"], room["b_blk_zone"]):
        if room.get("_round_timer_task"):
            room["_round_timer_task"].cancel()
        await _resolve_arena_round(room)

async def _resolve_arena_round(room: dict):
    async with room["lock"]:
        if room.get("finished"):
            return
        pa, pb = room["player_a"], room["player_b"]
        round_num = room["turn"] + 1

        a_atk = Zone[room["a_atk_zone"]]
        a_blk = Zone[room["a_blk_zone"]]
        b_atk = Zone[room["b_atk_zone"]]
        b_blk = Zone[room["b_blk_zone"]]

        log_lines = [f"═══ Раунд {round_num} ═══"]

        a_dmg, a_blocked, a_event = calculate_damage(attacker=pa, defender=pb, block_zone=b_blk, attacker_zone=a_atk)
        pb["hp"] = max(0, pb["hp"] - a_dmg)
        vamp_a = apply_vampirism(pa, a_dmg)

        if a_event.get("type") == "DODGE":
            log_lines.append(f"🔵 {pa['hero_name']} → {pb['hero_name']}: уклонение!")
        elif a_event.get("type") == "full_block":
            log_lines.append(f"🔵 {pa['hero_name']} → {pb['hero_name']}: полный блок!")
        else:
            log_lines.append(
                f"🔵 {pa['hero_name']} атакует {pb['hero_name']}: {a_dmg} урона"
                f"{' (заблок.)' if a_blocked else ''}"
                f"{' 💥КРИТ!' if a_event.get('crit') else ''}"
                f" {format_combat_events(a_event)}".rstrip()
            )
        if vamp_a:
            pa["hp"] = min(pa["max_hp"], pa["hp"] + vamp_a)
            log_lines.append(f"  🧛 Вампиризм {pa['hero_name']}: +{vamp_a} HP")

        counter_dmg_on_a = 0
        if pb.get("counter_chance", 0) > 0 and a_blocked and random.randint(1, 100) <= pb["counter_chance"]:
            counter_dmg_on_a = max(1, int(pb["atk"] * 0.5))
            if random.randint(1, 100) <= pb.get("crit", 0):
                counter_dmg_on_a = int(counter_dmg_on_a * CRIT_MULTIPLIER)
                log_lines.append(f"  🔴 Контрудар {pb['hero_name']} с КРИТ: {counter_dmg_on_a}!")
            else:
                log_lines.append(f"  🔴 Контрудар {pb['hero_name']}: {counter_dmg_on_a}")
            pa["hp"] = max(0, pa["hp"] - counter_dmg_on_a)

        b_dmg, b_blocked, b_event = calculate_damage(attacker=pb, defender=pa, block_zone=a_blk, attacker_zone=b_atk)
        pa["hp"] = max(0, pa["hp"] - b_dmg)
        vamp_b = apply_vampirism(pb, b_dmg)

        if b_event.get("type") == "DODGE":
            log_lines.append(f"🔴 {pb['hero_name']} → {pa['hero_name']}: уклонение!")
        elif b_event.get("type") == "full_block":
            log_lines.append(f"🔴 {pb['hero_name']} → {pa['hero_name']}: полный блок!")
        else:
            log_lines.append(
                f"🔴 {pb['hero_name']} атакует {pa['hero_name']}: {b_dmg} урона"
                f"{' (заблок.)' if b_blocked else ''}"
                f"{' 💥КРИТ!' if b_event.get('crit') else ''}"
                f" {format_combat_events(b_event)}".rstrip()
            )
        if vamp_b:
            pb["hp"] = min(pb["max_hp"], pb["hp"] + vamp_b)
            log_lines.append(f"  🧛 Вампиризм {pb['hero_name']}: +{vamp_b} HP")

        counter_dmg_on_b = 0
        if pa.get("counter_chance", 0) > 0 and b_blocked and random.randint(1, 100) <= pa["counter_chance"]:
            counter_dmg_on_b = max(1, int(pa["atk"] * 0.5))
            if random.randint(1, 100) <= pa.get("crit", 0):
                counter_dmg_on_b = int(counter_dmg_on_b * CRIT_MULTIPLIER)
                log_lines.append(f"  🔵 Контрудар {pa['hero_name']} с КРИТ: {counter_dmg_on_b}!")
            else:
                log_lines.append(f"  🔵 Контрудар {pa['hero_name']}: {counter_dmg_on_b}")
            pb["hp"] = max(0, pb["hp"] - counter_dmg_on_b)

        for unit, label in [(pa, "🔵"), (pb, "🔴")]:
            bleed_dmg, _ = apply_bleed(unit)
            if bleed_dmg:
                log_lines.append(f"  {label} Кровотечение {unit['hero_name']}: -{bleed_dmg} HP")

        log_lines.append(
            f"HP: {pa['hero_name']} {pa['hp']}/{pa['max_hp']} | "
            f"{pb['hero_name']} {pb['hp']}/{pb['max_hp']}"
        )
        room.setdefault("log", []).extend(log_lines)

        await _broadcast(room, {
            "type": "round_result",
            "round": round_num,
            "log": log_lines,
            "hp": {str(room["challenger"]): pa["hp"], str(room["target"]): pb["hp"]},
            "combat": {
                str(room["challenger"]): {"dmg_dealt": a_dmg, "dmg_taken": b_dmg + counter_dmg_on_a, "blocked": a_blocked, "crit": a_event.get("crit", False), "vampirism": vamp_a, "debuffs": [f"🩸 Кровотечение x{pa.get('bleed_stacks',0)}"] if pa.get("bleed_stacks",0) > 0 else []},
                str(room["target"]):     {"dmg_dealt": b_dmg, "dmg_taken": a_dmg + counter_dmg_on_b, "blocked": b_blocked, "crit": b_event.get("crit", False), "vampirism": vamp_b, "debuffs": [f"🩸 Кровотечение x{pb.get('bleed_stacks',0)}"] if pb.get("bleed_stacks",0) > 0 else []},
            },
        })

        a_dead = pa["hp"] <= 0
        b_dead = pb["hp"] <= 0
        if a_dead or b_dead or round_num >= MAX_ROUNDS:
            await _finish_battle(room, a_dead, b_dead, round_num)
            return

        room["a_atk_zone"] = None; room["a_blk_zone"] = None
        room["b_atk_zone"] = None; room["b_blk_zone"] = None
        room["turn"] = round_num
        room["_round_timer_task"] = asyncio.create_task(_pvp_round_timer(room))

async def _finish_battle(room: dict, a_dead: bool, b_dead: bool, round_num: int):
    async with room["lock"]:
        if room.get("finished"):
            return
        room["finished"] = True

    pa, pb = room["player_a"], room["player_b"]
    stake = room["stake"]
    chat_id = room["chat_id"]
    challenger_id = room["challenger"]
    target_id = room["target"]
    names = room["names"]
    battle_key = f"pvp:{chat_id}:{room['battle_id']}"

    if a_dead and b_dead:
        winner_id = loser_id = None
    elif a_dead:
        winner_id, loser_id = target_id, challenger_id
    elif b_dead:
        winner_id, loser_id = challenger_id, target_id
    else:
        if pa["hp"] > pb["hp"]:
            winner_id, loser_id = challenger_id, target_id
        elif pb["hp"] > pa["hp"]:
            winner_id, loser_id = target_id, challenger_id
        else:
            winner_id = loser_id = None

    try:
        if winner_id:
            async with async_session() as session:
                result = await session.execute(
                    text("""
                        UPDATE active_battles_storage
                        SET reward_paid = TRUE
                        WHERE battle_key = :key AND reward_paid = FALSE
                        RETURNING reward_paid
                    """),
                    {"key": battle_key}
                )
                row = result.fetchone()
                if row:
                    await add_coins(winner_id, stake)
                    await add_coins(loser_id, -stake)
                    from ..handlers.pvp import _update_pvp_stats
                    await _update_pvp_stats(winner_id, loser_id, int(chat_id))
    except Exception as e:
        logger.error("Arena rewards error: %s", e)

    if _bot:
        try:
            if winner_id:
                chat_text = (
                    f"🏆 <b>Дуэль завершена!</b>\n\n"
                    f"🥇 Победитель: <b>{names.get(winner_id)}</b> <code>(+{stake} {MG_EMOJI})</code>\n"
                    f"💀 Проигравший: <b>{names.get(loser_id)}</b> <code>(-{stake} {MG_EMOJI})</code>\n"
                    f"⚔️ Раундов: {round_num}"
                )
            else:
                chat_text = (
                    f"🤝 <b>Дуэль — Ничья!</b>\n"
                    f"<b>{names.get(challenger_id)}</b> vs <b>{names.get(target_id)}</b>\n"
                    f"⚔️ Раундов: {round_num}"
                )
            log_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📋 Логи боя", callback_data=f"send_logs:{room['battle_id']}")
            ]])
            await _bot.send_message(int(chat_id), chat_text, parse_mode="HTML", reply_markup=log_kb)
        except Exception as e:
            logger.exception("PvP finish message error: %s", e)

    await _broadcast(room, {"type": "game_over", "winner_id": winner_id, "round": round_num, "stake": stake, "names": names})
    await battle_storage.delete_battle(f"pvp:{chat_id}:{room['battle_id']}")
    asyncio.get_event_loop().call_later(1800, lambda: active_arena_battles.pop(room["battle_id"], None))
    for ws in room["connections"].values():
        if not ws.closed:
            try:
                await ws.close(code=1000)
            except Exception:
                pass

# ============================ PvE ============================
async def _handle_pve(ws, battle_id, user_id_int, chat_id):
    pve_key = f"pve:{chat_id}:{battle_id}"
    battle_data = await battle_storage.get_battle(pve_key)
    if not battle_data or battle_data.get("user_id") != user_id_int:
        await ws.close(code=4004, message="Not your battle")
        return

    if pve_key not in active_arena_battles:
        hero = battle_data.get("hero")
        monster = battle_data.get("monster")
        if not hero or not monster:
            await ws.close(code=4004, message="Invalid battle data")
            return
        user_name = await get_user_display_name(user_id_int)
        hero["hero_name"] = hero.get("hero_name", user_name)
        max_energy = 100
        try:
            async with async_session() as session:
                result = await session.execute(
                    text("SELECT max_energy FROM users WHERE telegram_id = :uid"), {"uid": user_id_int}
                )
                row = result.fetchone()
                if row and row[0]:
                    max_energy = row[0]
        except Exception as e:
            logger.error("Failed to load max_energy: %s", e)
        active_arena_battles[pve_key] = {
            "hero": hero, "monster": monster, "turn": 0,
            "hero_atk_zone": None, "hero_blk_zone": None,
            "user_id": user_id_int, "chat_id": chat_id, "battle_id": battle_id,
            "connections": {user_id_int: ws},
            "names": {user_id_int: user_name}, "log": [], "finished": False,
            "hunt_finished": False, "energy_left": battle_data.get("energy_left", 0),
            "max_energy": max_energy, "reward_given": False, "mode": "pve",
            "hero_name": hero.get("hero_name", "Герой"), "monster_name": monster.get("name", "Монстр"),
            "total_exp_earned": 0, "total_coins_earned": 0,
            "battle_series_active": True, "battle_count": 1,
        }
    else:
        room = active_arena_battles[pve_key]
        real_name = await get_user_display_name(user_id_int)
        room["names"][user_id_int] = real_name
        room["connections"][user_id_int] = ws
        if not room.get("finished"):
            await _send_pve_state(room, ws)
            return

    room = active_arena_battles[pve_key]
    await _send_pve_state(room, ws)
    task = asyncio.create_task(_pve_round_timer(room))
    room['_round_timer_task'] = task

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                await _handle_pve_message(room, user_id_int, msg.data)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        if not room.get("finished"):
            asyncio.create_task(_handle_pve_disconnect(room, user_id_int))
        else:
            room["connections"].pop(user_id_int, None)

async def _send_pve_state(room: dict, ws=None):
    hero = room["hero"]
    monster = room["monster"]
    payload = {
        "type": "battle_start",
        "hero": {"name": room["hero_name"], "hp": hero["hp"], "max_hp": hero["max_hp"]},
        "monster": {"name": monster["name"], "hp": monster["hp"], "max_hp": monster["max_hp"]},
        "energy_left": room["energy_left"], "max_energy": room.get("max_energy", 100),
        "round_timeout": PVE_ROUND_TIMEOUT, "turn": room["turn"],
    }
    if ws:
        await ws.send_json(payload)
    else:
        await _broadcast(room, payload)

async def _handle_pve_message(room: dict, user_id: int, raw: str):
    if room.get("finished"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        action = data.get("action")
        if action == "next_battle":
            await _start_next_pve_battle(room, user_id)
        elif action == "finish_hunt":
            if not room.get("hunt_finished"):
                await _finish_pve_hunt(room)
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    action = data.get("action")
    if action == "confirm_turn":
        atk_zone = data.get("atk_zone")
        blk_zone = data.get("blk_zone")
        if atk_zone in ("TOP", "MIDDLE", "BOTTOM") and blk_zone in ("TOP", "MIDDLE", "BOTTOM"):
            room["hero_atk_zone"] = atk_zone
            room["hero_blk_zone"] = blk_zone
            if '_round_timer_task' in room:
                room['_round_timer_task'].cancel()
            await _resolve_pve_round(room)
    elif action == "next_battle":
        await _start_next_pve_battle(room, user_id)
    elif action == "finish_hunt":
        if not room.get("hunt_finished"):
            await _finish_pve_hunt(room)

async def _pve_round_timer(room: dict):
    try:
        await asyncio.sleep(PVE_ROUND_TIMEOUT)
        if not room.get("finished") and room["hero_atk_zone"] is None:
            zones = [Zone.TOP, Zone.MIDDLE, Zone.BOTTOM]
            room["hero_atk_zone"] = random.choice(zones).value
            room["hero_blk_zone"] = random.choice(zones).value
            await _resolve_pve_round(room)
    except asyncio.CancelledError:
        pass

async def _resolve_pve_round(room: dict):
    if room.get("finished"):
        return
    hero = room["hero"]
    monster = room["monster"]
    round_num = room["turn"] + 1

    hero_atk = Zone[room["hero_atk_zone"]]
    hero_blk = Zone[room["hero_blk_zone"]]
    monster_atk = random.choice([Zone.TOP, Zone.MIDDLE, Zone.BOTTOM])
    monster_blk = random.choice([Zone.TOP, Zone.MIDDLE, Zone.BOTTOM])

    log_lines = [f"═══ Раунд {round_num} ═══"]

    hero_dmg, hero_blocked, hero_event = calculate_damage(attacker=hero, defender=monster, block_zone=monster_blk, attacker_zone=hero_atk)
    monster["hp"] = max(0, monster["hp"] - hero_dmg)
    vamp_hero = apply_vampirism(hero, hero_dmg)

    if hero_event.get("type") == "DODGE":
        log_lines.append(f"🔵 {room['hero_name']} → {monster['name']}: уклонение!")
    elif hero_event.get("type") == "full_block":
        log_lines.append(f"🔵 {room['hero_name']} → {monster['name']}: полный блок!")
    else:
        log_lines.append(f"🔵 {room['hero_name']} атакует: {hero_dmg} урона{' (заблок.)' if hero_blocked else ''}{' 💥КРИТ!' if hero_event.get('crit') else ''} {format_combat_events(hero_event)}".rstrip())
    if vamp_hero:
        hero["hp"] = min(hero["max_hp"], hero["hp"] + vamp_hero)
        log_lines.append(f"  🧛 Вампиризм {room['hero_name']}: +{vamp_hero} HP")

    monster_dmg, monster_blocked, monster_event = calculate_damage(attacker=monster, defender=hero, block_zone=hero_blk, attacker_zone=monster_atk)
    hero["hp"] = max(0, hero["hp"] - monster_dmg)
    vamp_monster = apply_vampirism(monster, monster_dmg)

    if monster_event.get("type") == "DODGE":
        log_lines.append(f"🔴 {monster['name']} → {room['hero_name']}: уклонение!")
    elif monster_event.get("type") == "full_block":
        log_lines.append(f"🔴 {monster['name']} → {room['hero_name']}: полный блок!")
    else:
        log_lines.append(f"🔴 {monster['name']} атакует: {monster_dmg} урона{' (заблок.)' if monster_blocked else ''}{' 💥КРИТ!' if monster_event.get('crit') else ''} {format_combat_events(monster_event)}".rstrip())
    if vamp_monster:
        monster["hp"] = min(monster["max_hp"], monster["hp"] + vamp_monster)
        log_lines.append(f"  🧛 Вампиризм {monster['name']}: +{vamp_monster} HP")

    for unit, label in [(hero, "🔵"), (monster, "🔴")]:
        bleed_dmg, _ = apply_bleed(unit)
        if bleed_dmg:
            log_lines.append(f"  {label} Кровотечение {unit.get('hero_name', unit.get('name', ''))}: -{bleed_dmg} HP")

    log_lines.append(f"HP: {room['hero_name']} {hero['hp']}/{hero['max_hp']} | {monster['name']} {monster['hp']}/{monster['max_hp']}")
    room.setdefault("log", []).extend(log_lines)

    await _broadcast(room, {
        "type": "round_result", "round": round_num, "log": log_lines,
        "hero_hp": hero["hp"], "monster_hp": monster["hp"],
        "combat": {
            "hero": {"dmg_dealt": hero_dmg, "dmg_taken": monster_dmg, "blocked": hero_blocked, "crit": hero_event.get("crit", False), "vampirism": vamp_hero, "debuffs": [f"🩸 Кровотечение x{hero.get('bleed_stacks',0)}"] if hero.get("bleed_stacks",0) > 0 else []},
            "monster": {"dmg_dealt": monster_dmg, "dmg_taken": hero_dmg, "blocked": monster_blocked, "crit": monster_event.get("crit", False), "vampirism": vamp_monster, "debuffs": [f"🩸 Кровотечение x{monster.get('bleed_stacks',0)}"] if monster.get("bleed_stacks",0) > 0 else []},
        },
        "energy_left": room["energy_left"], "max_energy": room.get("max_energy", 100),
    })

    if hero["hp"] <= 0 or monster["hp"] <= 0 or round_num >= MAX_ROUNDS:
        await _end_pve_battle(room, hero["hp"] <= 0, monster["hp"] <= 0, round_num)
    else:
        room["hero_atk_zone"] = None; room["hero_blk_zone"] = None
        room["turn"] = round_num
        task = asyncio.create_task(_pve_round_timer(room))
        room['_round_timer_task'] = task

async def _end_pve_battle(room: dict, hero_dead: bool, monster_dead: bool, round_num: int):
    if room.get("finished"):
        return
    room["finished"] = True
    monster = room["monster"]
    hero = room["hero"]
    exp_earned = coins_earned = 0
    if not room.get("reward_given"):
        if monster.get("gnome"):
            exp_earned = monster.get("gnome_reward_exp", 0)
            coins_earned = monster.get("gnome_reward_coins", 0)
        else:
            if monster_dead and not hero_dead:
                exp_earned = monster.get("reward_exp", 0)
                coins_earned = monster.get("reward_coins", 0)
            elif not (hero_dead and not monster_dead):
                exp_earned = int(monster.get("reward_exp", 0) / 2)
                coins_earned = int(monster.get("reward_coins", 0) / 2)
        if exp_earned > 0 or coins_earned > 0:
            try:
                await add_hero_experience(room["user_id"], exp_earned)
                await add_coins(room["user_id"], coins_earned)
            except Exception as e:
                logger.error("PvE reward error: %s", e)
        room["total_exp_earned"] = room.get("total_exp_earned", 0) + exp_earned
        room["total_coins_earned"] = room.get("total_coins_earned", 0) + coins_earned
        room["reward_given"] = True

    outcome = "victory" if monster_dead and not hero_dead else ("defeat" if hero_dead else "draw")
    await _broadcast(room, {
        "type": "game_over", "outcome": outcome, "hero_hp": hero["hp"], "monster_hp": monster["hp"],
        "round": round_num, "exp_earned": exp_earned, "coins_earned": coins_earned,
        "energy_left": room["energy_left"], "max_energy": room.get("max_energy", 100),
        "can_continue": room["energy_left"] >= 10,
        "total_exp": room.get("total_exp_earned", 0), "total_coins": room.get("total_coins_earned", 0),
    })
    if room["energy_left"] >= 10:
        asyncio.create_task(_pve_continue_timer(room))

async def _start_next_pve_battle(room: dict, user_id: int):
    if room.get("hunt_finished"):
        await _broadcast(room, {"type": "error", "message": "Охота уже завершена"})
        return
    if room["energy_left"] < 10:
        await _broadcast(room, {"type": "error", "message": "Недостаточно энергии"})
        return
    async with async_session() as session:
        result = await session.execute(
            text("UPDATE users SET energy = energy - 10 WHERE telegram_id = :uid AND energy >= 10 RETURNING energy"),
            {"uid": user_id}
        )
        new_energy = result.scalar_one_or_none()
        if new_energy is None:
            await _broadcast(room, {"type": "error", "message": "Не удалось списать энергию"})
            return
        await session.commit()
    room["energy_left"] = new_energy
    hero = room["hero"]
    hero["hp"] = hero["max_hp"]
    hero["bleed_stacks"] = 0
    monster = get_random_monster(hero["level"])
    monster = scale_monster_to_hero(monster, hero)
    room["monster"] = monster
    room["hero_atk_zone"] = None; room["hero_blk_zone"] = None
    room["turn"] = 0; room["finished"] = False; room["reward_given"] = False
    room["monster_name"] = monster["name"]
    room["battle_count"] = room.get("battle_count", 0) + 1
    await _broadcast(room, {
        "type": "battle_start",
        "hero": {"name": room["hero_name"], "hp": hero["hp"], "max_hp": hero["max_hp"]},
        "monster": {"name": monster["name"], "hp": monster["hp"], "max_hp": monster["max_hp"]},
        "energy_left": room["energy_left"], "max_energy": room.get("max_energy", 100),
        "round_timeout": PVE_ROUND_TIMEOUT, "turn": 0,
    })
    task = asyncio.create_task(_pve_round_timer(room))
    room['_round_timer_task'] = task

async def _finish_pve_hunt(room: dict):
    if room.get("hunt_finished"):
        return
    room["hunt_finished"] = True
    room["battle_series_active"] = False
    user_name = room["names"].get(room["user_id"], str(room["user_id"]))
    chat_id = int(room["chat_id"])
    total_exp = room.get("total_exp_earned", 0)
    total_coins = room.get("total_coins_earned", 0)
    battle_count = room.get("battle_count", 1)
    if _bot:
        try:
            msg_text = (
                f"🎯 Охота завершена!\n👤 {user_name}\n"
                f"💰 Всего: +{total_exp} опыта, +{total_coins} {MG_EMOJI}\n"
                f"⚡ Энергия: {room['energy_left']}/{room.get('max_energy', 100)}"
            )
            log_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📋 Логи боя", callback_data=f"send_logs:{room['battle_id']}:{room['chat_id']}")
            ]])
            await _bot.send_message(chat_id, msg_text, parse_mode="HTML", reply_markup=log_kb)
        except Exception as e:
            logger.exception("Final hunt message error")
    if _bot:
        try:
            user_id = room["user_id"]
            total_rounds = sum(1 for line in room.get("log", []) if line.startswith("═══"))
            summary = (
                f"📊 <b>Статистика охоты</b>\n⚔️ Боёв: {battle_count}\n"
                f"🔄 Раундов: {total_rounds}\n💰 +{total_exp} опыта, +{total_coins} {MG_EMOJI}\n\n📋 <b>Лог:</b>\n"
            )
            full_log = summary + "\n".join(room.get("log", []))
            for i in range(0, len(full_log), 4000):
                await _bot.send_message(user_id, full_log[i:i+4000], parse_mode="HTML")
        except Exception as e:
            logger.exception("Failed to send hunt logs to user")
    for ws in list(room["connections"].values()):
        if not ws.closed:
            try:
                await ws.send_json({"type": "hunt_finished", "message": "Охота завершена."})
                await ws.close(code=1000)
            except Exception:
                pass
    pve_key = f"pve:{room['chat_id']}:{room['battle_id']}"
    await battle_storage.delete_battle(pve_key)
    async def _delayed_cleanup():
        await asyncio.sleep(PVE_LOG_TTL)
        active_arena_battles.pop(pve_key, None)
    asyncio.create_task(_delayed_cleanup())

async def _pve_continue_timer(room: dict):
    try:
        await asyncio.sleep(PVE_CONTINUE_TIMEOUT)
        if room.get("hunt_finished") or not room.get("battle_series_active"):
            return
        await _finish_pve_hunt(room)
    except asyncio.CancelledError:
        pass

async def _handle_pve_disconnect(room: dict, user_id: int):
    room["connections"].pop(user_id, None)
    await asyncio.sleep(30)
    if user_id in room.get("connections", {}):
        return
    if room.get("finished"):
        return
    while not room.get("finished"):
        zones = [Zone.TOP, Zone.MIDDLE, Zone.BOTTOM]
        room["hero_atk_zone"] = random.choice(zones).value
        room["hero_blk_zone"] = random.choice(zones).value
        await _resolve_pve_round(room)

async def _broadcast(room: dict, payload: dict):
    data = json.dumps(payload, ensure_ascii=False)
    for ws in list(room["connections"].values()):
        if not ws.closed:
            try:
                await ws.send_str(data)
            except Exception as e:
                logger.warning("Broadcast error: %s", e)
