"""本地缓存：把最近一次查询结果存到 data/cache.json，页面打开先秒开再后台刷新。

缓存按「宿舍 + 日期区间 + 类型」分 key，换宿舍或改日期区间各存各的，互不串。
"""
from __future__ import annotations

import datetime
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE, "data")
CACHE_PATH = os.path.join(CACHE_DIR, "cache.json")

# 超过这个时长就当过期，前端仍可秒开但会立刻后台刷新
CACHE_TTL_SECONDS = 12 * 3600


def _read():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(data):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def get_room(key: str):
    """读取某个房间的缓存，返回 (payload, updated_at_dt) 或 (None, None)。"""
    data = _read()
    slot = data.get("rooms", {}).get(key)
    if not slot:
        return None, None
    try:
        ts = datetime.datetime.fromisoformat(slot.get("updated_at", ""))
    except ValueError:
        return None, None
    return slot, ts


def is_fresh(ts) -> bool:
    if not ts:
        return False
    return (datetime.datetime.now() - ts).total_seconds() <= CACHE_TTL_SECONDS


def save_room(key: str, trend=None, purchase=None) -> str:
    """写入（局部更新）并返回更新时间字符串。trend / purchase 可只传一个。"""
    data = _read()
    data.setdefault("rooms", {})
    slot = data["rooms"].get(key, {})
    if trend is not None:
        slot["trend"] = trend
    if purchase is not None:
        slot["purchase"] = purchase
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    slot["updated_at"] = now
    data["rooms"][key] = slot
    _write(data)
    return now
