"""校园用电查询 Web 服务（开源版）。

前端页面：static/index.html
后端 API：
  GET  /api/config               读取当前房间配置
  PUT  /api/config               更新房间配置（写回 config.json）
  GET  /api/cached?room=          读本地缓存（页面秒开用，不访问学校接口）
  GET  /api/records?begin=&end=&type=   查询原始记录（type=2 用电 / 1 购电）
  GET  /api/trend?days=&end=             计算余额趋势与统计
  GET  /api/purchase?days=&end=          查询购电记录（通用解析，返回表头+行）

运行：python app.py  ->  http://127.0.0.1:8788
"""
from __future__ import annotations

import datetime
import json
import os
import re

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

import store

try:
    import qrcode  # noqa: PLC0415  二维码生成器，只用于生成 SVG，缺了不影响主功能
except ImportError:  # pragma: no cover
    qrcode = None

from scraper import (
    ElectricityError,
    ElectricityScraper,
    client_name_of,
    display_name_of,
    fetch_login_html,
    get_building_tree,
    get_client_tree,
    id_of,
    parse_buildings,
    parse_clients,
    resolve_dorm,
)

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
STATIC = os.path.join(BASE, "static")

# 后端接口版本。前端 `static/app.js` 里有同名的 EXPECT_VERSION。
#
# 为什么需要这个东西：静态页面每次请求都从磁盘读，改了立刻生效；但 Python 进程
# 不重启就一直跑旧代码。于是会出现"页面是新的、接口是旧的"这种诡异状态 ——
# 症状是新按钮报 `请求失败（HTTP 404）`。有了版本号，前端能自己认出旧进程并提示
# 「关掉旧的命令行窗口再重新双击 start.bat」，不用靠用户猜。
APP_VERSION = "1.1"

app = Flask(__name__, static_folder=STATIC)

CONFIG_KEYS = [
    "server", "client", "clientName", "building", "buildingId", "room",
    "defaultRangeDays", "timeout", "purchaseUrl",
]

# 深圳大学的电费系统地址（固定）。换成别的学校就把这里改掉，
# 或者在页面上「高级设置 → 服务器地址」里手填。
DEFAULT_SERVERS = [
    "http://192.168.84.3:9090/cgcSims",
]

DEFAULT_CONFIG = {
    "server": "",          # 学校电费系统地址，如 http://192.168.84.3:9090/cgcSims
    "client": "",          # 校区编号（登录页「校区」下拉的值，如 192.168.84.110 = 南校区）
    "clientName": "",      # 校区名字，只用于显示
    "building": "",        # 楼栋名，原样取自下拉列表
    "buildingId": "",      # 楼栋编号（下拉的值），查询以它为准
    "room": "",            # 房间号（门牌号）
    "defaultRangeDays": 30,
    "timeout": 10,
    "purchaseUrl": "",     # 选填：微信缴费页，留空则顶栏不显示「扫码购电」
}


def load_config():
    """读取 config.json。

    配置只描述「哪个楼栋 + 哪个房间号」，roomid 这类内部 ID 一律不写给用户 ——
    服务端是按房间名匹配的，roomid 由登录页自动提取，手填只会填错。
    旧版本 rooms[]/active/roomid/roomName 的配置会自动收敛成 {building, room}。

    config.json 不进仓库（里面是宿舍信息），所以新克隆下来第一次运行会**自动生成**
    一份空配置，去页面上「修改宿舍」填一次就好。
    """
    if not os.path.exists(CONFIG_PATH):
        cfg = dict(DEFAULT_CONFIG)
        save_config(cfg)
        return cfg

    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    # roomid 是派生值，不进配置：留着只会让换宿舍后查到上一个房间的数据
    cfg.pop("roomid", None)

    # 旧配置（多房间）收敛成单个宿舍：people 只关心自己那间，不需要切换列表
    migrated = False
    if cfg.get("rooms"):
        rooms = cfg["rooms"]
        idx = int(cfg.get("active", 0) or 0)
        cur = rooms[idx] if 0 <= idx < len(rooms) else rooms[0]
        cfg["building"] = cur.get("building") or cur.get("name") or ""
        cfg["room"] = cur.get("roomName") or cur.get("room") or ""
        for k in ("rooms", "active", "roomid", "roomName", "name"):
            cfg.pop(k, None)
        migrated = True

    # 楼栋名统一成"给学生看的名字"：配置里可能存的是楼栋 ID（"6878"），
    # 换算成下拉里显示的名字，否则前端拿到名字对不上选项，会显示成空白。
    b = str(cfg.get("building", "")).strip()
    if b:
        if b.isdigit():
            # 老配置里只存了编号：补上显示名，同时把编号也留下来
            if not str(cfg.get("buildingId") or "").strip():
                cfg["buildingId"] = b
                migrated = True
            shown = display_name_of(b)
            if shown and b != shown:
                cfg["building"] = shown
                migrated = True
        elif not str(cfg.get("buildingId") or "").strip():
            bid = id_of(b)
            if bid:
                cfg["buildingId"] = bid
                migrated = True

    # 校区名只是给人看的，缺了就用兜底表补上（值本身就是学校页面的固定编号）
    if str(cfg.get("client") or "").strip() and not str(cfg.get("clientName") or "").strip():
        name = client_name_of(cfg["client"])
        if name:
            cfg["clientName"] = name
            migrated = True

    # 迁移结果必须落盘，否则服务一重启又读回老格式，白改。
    if migrated:
        save_config(cfg)

    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def dorm_label(cfg):
    """页面上显示的宿舍名，如「乔相11-20层 301」。"""
    building = cfg.get("building") or ""
    room = cfg.get("room") or ""
    return " ".join(x for x in (building, room) if x) or "未配置"


def make_scraper(cfg):
    bid, room_name = resolve_dorm(
        cfg.get("building", ""), cfg.get("room", ""), cfg.get("buildingId", "")
    )
    return ElectricityScraper(
        server=cfg["server"],
        client=cfg["client"],
        # 永远把 roomid 留空，让登录页自己吐出正确的内部 ID。
        # 之前试过"配置里有就用配置里的"，结果换宿舍后旧 roomid 会让查询落到上一个房间，
        # 而且查出来是错的还不报错。roomid 是派生值，不是给用户填的。
        roomid="",
        roomName=room_name,
        building=bid,
        timeout=int(cfg.get("timeout", 10)),
    )


def query_records(cfg, begin, end, record_type=2):
    return make_scraper(cfg).query(begin, end, record_type)


def query_purchase(cfg, begin, end):
    return make_scraper(cfg).query_purchase(begin, end)


def compute_trend(records):
    records = [r for r in records if r["remain"] is not None]
    records.sort(key=lambda r: r["date"])
    dates = [r["date"] for r in records]
    remain = [r["remain"] for r in records]

    # 每日用电：优先用"总用电量"差分，否则用"剩余电量"差分（剩余下降量）
    # 索引 i 对应 dates[i] = 当天用电量。
    # 第一格没有"前一天"可比，用电量未知，填 None 而不是 0 —— 填 0 会拉低日均。
    daily = [None]
    for i in range(1, len(records)):
        if records[i].get("total_use") is not None and records[i - 1].get("total_use") is not None:
            d = records[i]["total_use"] - records[i - 1]["total_use"]
        elif records[i]["remain"] is not None and records[i - 1]["remain"] is not None:
            d = records[i - 1]["remain"] - records[i]["remain"]
        else:
            d = 0.0
        # 差分会留下浮点尾巴（12.419999999998254），磨到分，别让界面上出现这种数
        daily.append(round(max(d, 0.0), 2))

    valid_daily = [d for d in daily if d is not None and d >= 0]
    avg = sum(valid_daily) / len(valid_daily) if valid_daily else 0.0
    current = remain[-1] if remain else None
    latest_date = dates[-1] if dates else None

    # 「今日用电」= 今天 0 点至今的用电量，取值条件只有一条：服务端真的产出了今天的记录。
    #
    # 服务端每天只在 23:59 存一次快照，所以今天 23:59 之前，数据表里根本没有今天这一行
    # （实测查 09-27~09-27 返回 0 条；整个系统只有 login.do / selectList.do 两个接口，
    #  没有实时读数）。这个量此刻无解，所以 today_use 就是 None。
    # 前端拿到 None 会直接把整张卡片藏起来 —— 不显示、不猜、不拿昨天那格补位。
    # 绝不为了"卡片上有个数字"而伪造，那是这个字段历史上出过的错。
    today_date = datetime.date.today().isoformat()
    today_use = daily[-1] if (daily and latest_date and latest_date >= today_date) else None
    est = (current / avg) if (current and avg > 0) else None
    return {
        "dates": dates,
        "remain": remain,
        "daily_use": daily,
        "current_remain": current,
        "today_use": today_use,
        "latest_date": latest_date,
        "avg_daily": round(avg, 2),
        "estimated_days": round(est, 1) if est else None,
        "count": len(records),
    }


def cache_key(cfg, begin, end, kind="trend"):
    """缓存 key 含宿舍 + 日期区间 + 类型，避免把 30 天的数据当成 7 天的用。"""
    return f"{cfg.get('building', '')}/{cfg.get('room', '')}|{begin}~{end}|{kind}"


def is_forced():
    return request.args.get("force") in ("1", "true", "True")


def yesterday_iso():
    """昨天（YYYY-MM-DD）。

    用电记录每天只在 23:59 结算一次，今天这一行在当天不存在，所以「用电」相关查询的
    默认/最大结束日期都取昨天（购电是实时的，仍然可以查到今天，见 /api/purchase）。
    """
    return (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")


def cached_or_none(cfg, begin, end, kind="trend", fresh_only=False):
    """命中缓存就返回响应体，否则返回 None。"""
    slot, ts = store.get_room(cache_key(cfg, begin, end, kind))
    if not slot or not slot.get(kind):
        return None
    if fresh_only and not store.is_fresh(ts):
        return None
    out = dict(slot[kind])
    out["ok"] = True
    out["cached"] = True
    out["updated_at"] = slot.get("updated_at")
    return out


def conn_error(cfg):
    return jsonify(
        {"ok": False, "error": f"无法连接电费服务器，请确认在校园网内且能访问 {cfg['server']}"}
    ), 502


@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


# 「学校原页面」的中转页：不是链接到 login.do 让用户自己填楼栋房号，
# 而是直接替他把登录表单 POST 出去。
#
# 为什么必须绕这一下：学校那套 cgcSims 的登录只能靠 **POST login.do**
# （实测 GET 带全部参数照样返回登录页），而且查询接口 selectList.do 只认登录后的会话。
# 所以浏览器里必须真正发生过一次 POST，才能在看学校页面时保持已登录。
# 把 POST 放在我们自己这个页面上自动提交，用户点一下就落在「自己那间宿舍」的页面上。
#
# 实测（2026-09-28）：
#   · 全新会话直接 POST login.do（不先 GET）就能成功，返回页含 selectListForm + roomId 隐藏字段
#   · buildingName 传空串即可，服务端按 (buildingId, roomName) 定位房间
#   · Referer 完全不参与校验，跨源提交（本机 127.0.0.1 → 校内服务器）不受影响
_SCHOOL_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>正在进入学校原页面…</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font:15px/1.7 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background:#f5f6f8; color:#1f2328; }
  .card { max-width:430px; margin:16px; padding:28px 30px; border:1px solid #e3e6ea;
          border-radius:14px; background:#fff; text-align:center; }
  .spin { width:26px; height:26px; margin:0 auto 14px; border:3px solid rgba(128,128,128,.25);
          border-top-color:#3b82f6; border-radius:50%; animation:sp .8s linear infinite; }
  @keyframes sp { to { transform: rotate(360deg); } }
  h1 { font-size:16px; margin:0 0 8px; }
  p { margin:6px 0; font-size:13px; opacity:.72; }
  .dorm { font-weight:600; opacity:1; }
  .btn { display:inline-block; margin-top:14px; padding:8px 18px; border-radius:8px;
         background:#3b82f6; color:#fff; text-decoration:none; font-size:13px; }
  .plain { display:block; margin-top:12px; font-size:12px; opacity:.5; color:inherit; }
  @media (prefers-color-scheme: dark) {
    body { background:#15171b; color:#e8eaed; }
    .card { background:#1e2126; border-color:#2c3038; }
  }
</style>
</head>
<body>
  <div class="card">
    <div class="spin"></div>
    <h1>正在进入学校原页面…</h1>
    <p>已自动带上你的宿舍 <span class="dorm">__DORM__</span>，不用再选楼栋、填房间号。</p>
    <form id="sf" method="post" action="__ACTION__" accept-charset="gb2312">
      __FIELDS__
      <a class="btn" href="#" onclick="document.getElementById('sf').submit();return false;">没反应？手动进入</a>
    </form>
    <a class="plain" href="__PLAIN__">还是想自己登录 → 打开学校登录页</a>
  </div>
<script>document.getElementById("sf").submit();</script>
</body>
</html>
"""


@app.route("/school")
def school_entry():
    """点一下直接进「你所在的宿舍」的学校原页面，不用重复登录。

    做法：把学校登录表单的三个关键字段（client / buildingId / roomName）从
    本地配置拼好，在浏览器里自动 POST 到 login.do。浏览器由此拿到已登录会话，
    落在学校自己的页面上（不是我们伪造的页面）。
    """
    cfg = load_config()
    server = (cfg.get("server") or "").rstrip("/")
    client = (cfg.get("client") or "").strip()
    try:
        bid, room_name = resolve_dorm(
            cfg.get("building", ""), cfg.get("room", ""), cfg.get("buildingId", "")
        )
    except ValueError as e:
        return (
            f"<meta charset='utf-8'><p style='font:15px sans-serif;padding:24px'>"
            f"还不能跳转：{e}。请回主页点「⚙ 修改宿舍」先把楼栋和房间号填好。</p>"
        ), 400
    if not server or not client:
        return (
            "<meta charset='utf-8'><p style='font:15px sans-serif;padding:24px'>"
            "还不能跳转：服务器地址或校区没配。请回主页点「⚙ 修改宿舍」补上。"
            "（点里面的「自动检测」最快）</p>"
        ), 400

    fields = "".join(
        f'<input type="hidden" name="{k}" value="{v}">'
        for k, v in (
            ("client", client),
            ("buildingId", bid),
            ("buildingName", ""),   # 实测传空最稳，服务端靠 (buildingId, roomName) 定位房间
            ("roomName", room_name),
            ("select", " 查询 "),
        )
    )
    label = " ".join(x for x in (cfg.get("building", ""), room_name) if x)
    html = (
        _SCHOOL_PAGE.replace("__DORM__", label)
        .replace("__ACTION__", f"{server}/login.do")
        .replace("__FIELDS__", fields)
        .replace("__PLAIN__", f"{server}/login.do?task=station&client={client}")
    )
    return Response(html, mimetype="text/html")


@app.route("/api/config", methods=["GET"])
def get_config():
    """读配置。顺带带上后端版本号，让前端能发现"进程还是旧的"。"""
    cfg = load_config()
    cfg["version"] = APP_VERSION
    return jsonify(cfg)


@app.route("/api/detect")
def api_detect():
    """「自动检测」：探到学校服务器 → 列出校区 → 列出该校区的楼栋。

    给不想看文档的人用：连上校园网点一下，server / 校区 / 楼栋三样都替你填好。

    为什么楼栋要跟着校区走：登录页的楼栋下拉**是按校区不同的**（南区是新斋那批、
    北区是乔字头那批），先选错校区就会拿到一份不属于自己的楼栋表。

    参数：`client`（校区编号，缺省用配置里的）
    返回：{ok, server, clients:[{value,name}], client, buildings:[{id,name}], source}
    """
    cfg = load_config()
    # 服务器候选：配置里填过的排前面，然后是内置的学校地址
    candidates = []
    for s in [cfg.get("server", "")] + DEFAULT_SERVERS:
        s = (s or "").strip().rstrip("/")
        if s and s not in candidates:
            candidates.append(s)
    if not candidates:
        return jsonify({"ok": False, "error": "没有可用的服务器地址"}), 400

    client = (request.args.get("client") or cfg.get("client") or "").strip()
    last_err = ""
    for server in candidates:
        try:
            html = fetch_login_html(server, client, timeout=int(cfg.get("timeout", 8)))
        except requests.exceptions.RequestException as e:
            last_err = f"{type(e).__name__}"
            continue
        if "loginForm" not in html:
            last_err = "返回的不是登录页"
            continue

        clients = parse_clients(html)
        buildings = [
            {"id": bid, "name": name} for bid, name in parse_buildings(html)
        ]
        # 没指定校区时，取登录页第一个校区作为默认（页面上的默认就是它）
        if not client and clients:
            client = clients[0]["value"]
        client_name = next((c["name"] for c in clients if c["value"] == client), "")
        return jsonify(
            {
                "ok": True,
                "server": server,
                "clients": clients,
                "client": client,
                "clientName": client_name,
                "buildings": buildings,
                "source": "live" if buildings else "builtin",
            }
        )
    return jsonify(
        {
            "ok": False,
            "error": "连不上学校电费服务器 —— 先确认电脑已经连上校园网"
            f"（WiFi 或网线），再点一次自动检测。{('诊断：' + last_err) if last_err else ''}",
            "tried": candidates,
        }
    ), 502


def validate_config(cfg):
    """校验配置，返回错误信息字符串；合法返回 None。保存前拦住手抖填错的值。"""
    server = (cfg.get("server") or "").strip()
    if not server:
        return "服务器地址不能为空"
    if not server.startswith(("http://", "https://")):
        return f"服务器地址要以 http:// 或 https:// 开头（现在是「{server}」）"

    client = (cfg.get("client") or "").strip()
    if not client:
        return "请选择校区（点「自动检测」会自动列出可选校区）"
    if not re.match(r"^[\w.\-]+$", client):
        return f"校区编号格式不对（现在是「{client}」），点「自动检测」重新取一次"

    try:
        days = int(cfg.get("defaultRangeDays", 30))
    except (TypeError, ValueError):
        return "默认天数必须是一个整数"
    if not 1 <= days <= 365:
        return f"默认天数要在 1~365 之间（现在是 {days}）"
    cfg["defaultRangeDays"] = days

    try:
        cfg["timeout"] = max(1, min(60, int(cfg.get("timeout", 10))))
    except (TypeError, ValueError):
        cfg["timeout"] = 10

    try:
        resolve_dorm(cfg.get("building", ""), cfg.get("room", ""), cfg.get("buildingId", ""))
    except ValueError as e:
        return str(e)

    # 购电网址选填：留空表示顶栏不显示「去购电」按钮；
    # 填了就必须是完整链接，免得手抖写一半出来个坏按钮。
    purl = (cfg.get("purchaseUrl") or "").strip()
    if purl and not purl.startswith(("http://", "https://")):
        return f"购电网址要以 http:// 或 https:// 开头（现在是「{purl}」）"
    cfg["purchaseUrl"] = purl

    return None


@app.route("/api/dorm-options")
def api_dorm_options():
    """下拉选项：楼栋表 + 校区兜底表。前端不硬编码，改这里即可扩展。"""
    return jsonify({"buildings": get_building_tree(), "clients": get_client_tree()})


@app.route("/api/qrcode.svg")
def api_qrcode():
    """把任意链接（默认用配置的购电网址）渲染成二维码 SVG。

    为什么放在后端：前端不引任何二维码库，保持"零依赖、校园内网可跑"。
    为什么输出 SVG：纯文本，不需要 Pillow，不会出现"装了个库才能用"。
    """
    url = request.args.get("url") or load_config().get("purchaseUrl", "")
    url = (url or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "还没有配置购电网址"}), 400
    if qrcode is None:
        return jsonify({"ok": False, "error": "服务端缺 qrcode 库：pip install qrcode"}), 500

    qr = qrcode.QRCode(
        box_size=1,
        border=2,                 # 留白，否则扫不出
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    # 同一行里连成一片的黑块合成一个 rect，能少画一半以上
    rects = []
    for y, row in enumerate(matrix):
        x = 0
        while x < len(row):
            if row[x]:
                x0 = x
                while x < len(row) and row[x]:
                    x += 1
                rects.append(f'<rect x="{x0}" y="{y}" width="{x - x0}" height="1"/>')
            else:
                x += 1
    n = len(matrix)

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{n}" height="{n}" '
        f'viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
        f'<rect width="{n}" height="{n}" fill="#ffffff"/>'
        f'<g fill="#000000">{"".join(rects)}</g></svg>'
    )
    return Response(svg, mimetype="image/svg+xml")


@app.route("/api/config", methods=["PUT"])
def put_config():
    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    for k in CONFIG_KEYS:
        if k in data and data[k] is not None:
            cfg[k] = str(data[k]).strip()
    err = validate_config(cfg)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    save_config(cfg)
    return jsonify(cfg)


@app.route("/api/records")
def api_records():
    cfg = load_config()
    begin = request.args.get("begin")
    end = request.args.get("end") or datetime.date.today().strftime("%Y-%m-%d")
    rtype = int(request.args.get("type", "2"))
    if not begin:
        return jsonify({"ok": False, "error": "缺少 begin 参数（格式 YYYY-MM-DD）"}), 400
    try:
        records = query_records(cfg, begin, end, rtype)
        return jsonify({"ok": True, "room": cfg.get("room", ""), "records": records})
    except requests.exceptions.ConnectionError:
        return conn_error(cfg)
    except requests.exceptions.Timeout:
        return jsonify({"ok": False, "error": "连接电费服务器超时"}), 504
    except ElectricityError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"查询失败：{e}"}), 500


@app.route("/api/cached")
def api_cached():
    """只读本地缓存，绝不访问学校接口 —— 页面打开时秒开用。"""
    cfg = load_config()
    begin = request.args.get("begin")
    end = request.args.get("end") or datetime.date.today().strftime("%Y-%m-%d")
    kind = request.args.get("kind", "trend")
    slot, ts = store.get_room(cache_key(cfg, begin, end, kind))
    if not slot:
        return jsonify({"ok": False, "error": "no-cache"})
    return jsonify(
        {
            "ok": True,
            "cached": True,
            "fresh": store.is_fresh(ts),
            "updated_at": slot.get("updated_at"),
            kind: slot.get(kind),
        }
    )


@app.route("/api/trend")
def api_trend():
    cfg = load_config()
    begin = request.args.get("begin")
    # 默认结束日期 = 昨天：今天的用电行要等 23:59 结算才存在，把今天算进来只会多一天空数据
    end = request.args.get("end") or yesterday_iso()
    if begin:
        try:
            datetime.date.fromisoformat(begin)
            datetime.date.fromisoformat(end)
        except ValueError:
            return jsonify({"ok": False, "error": "begin/end 参数格式应为 YYYY-MM-DD"}), 400
    else:
        days = int(request.args.get("days", cfg.get("defaultRangeDays", 30)))
        try:
            begin = (datetime.date.fromisoformat(end) - datetime.timedelta(days=days - 1)).strftime("%Y-%m-%d")
        except ValueError:
            return jsonify({"ok": False, "error": "end 参数格式应为 YYYY-MM-DD"}), 400

    # 未强制刷新时，本地有该房间该区间的缓存就直接返回（不打扰学校接口）
    if not is_forced():
        hit = cached_or_none(cfg, begin, end, "trend", fresh_only=True)
        if hit:
            return jsonify(hit)

    try:
        records = query_records(cfg, begin, end, 2)
        trend = compute_trend(records)
        trend["ok"] = True
        trend["room"] = cfg.get("room", "")
        trend["records"] = records  # 供历史表格展示原始字段
        trend["begin"] = begin
        trend["end"] = end
        store.save_room(cache_key(cfg, begin, end, "trend"), trend=trend)
        return jsonify(trend)
    except requests.exceptions.ConnectionError:
        return conn_error(cfg)
    except requests.exceptions.Timeout:
        return jsonify({"ok": False, "error": "连接电费服务器超时"}), 504
    except ElectricityError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"查询失败：{e}"}), 500


@app.route("/api/purchase")
def api_purchase():
    cfg = load_config()
    today = datetime.date.today().strftime("%Y-%m-%d")
    begin = request.args.get("begin")
    end = request.args.get("end") or today
    if begin:
        try:
            datetime.date.fromisoformat(begin)
            datetime.date.fromisoformat(end)
        except ValueError:
            return jsonify({"ok": False, "error": "begin/end 参数格式应为 YYYY-MM-DD"}), 400
    else:
        days = int(request.args.get("days", 180))
        try:
            begin = (datetime.date.fromisoformat(end) - datetime.timedelta(days=days - 1)).strftime("%Y-%m-%d")
        except ValueError:
            return jsonify({"ok": False, "error": "end 参数格式应为 YYYY-MM-DD"}), 400

    # 未强制刷新时，本地有该房间该区间的购电缓存就直接返回
    if not is_forced():
        hit = cached_or_none(cfg, begin, end, "purchase", fresh_only=True)
        if hit:
            return jsonify(hit)

    try:
        result = query_purchase(cfg, begin, end)
        result["ok"] = True
        result["room"] = cfg.get("room", "")
        result["begin"] = begin
        result["end"] = end
        store.save_room(cache_key(cfg, begin, end, "purchase"), purchase=result)
        return jsonify(result)
    except requests.exceptions.ConnectionError:
        return conn_error(cfg)
    except requests.exceptions.Timeout:
        return jsonify({"ok": False, "error": "连接电费服务器超时"}), 504
    except ElectricityError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"查询失败：{e}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8788))
    app.run(host="127.0.0.1", port=port, debug=False)

