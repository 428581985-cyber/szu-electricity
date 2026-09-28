"""SIMS 校园用电信息查询爬虫（开源版）。

目标系统：智能集中式电能计量系统 (cgcSims)，通常部署在校园内网，
例如 http://<ip>:9090/cgcSims/selectList.do

接口事实（来自浏览器开发者工具抓包）：
- 登录：GET  /cgcSims/login.do?task=station&client=<client_ip>  取 JSESSIONID
- 查询：POST /cgcSims/selectList.do  (application/x-www-form-urlencoded)
        参数：beginTime, endTime, type(2=用电记录), client, roomid,
              roomName, building, hiddenType=0, isHost=0
- 响应：text/html; charset=gb2312

本模块只负责"登录 + 查询 + 解析表格"，不依赖数据库，方便开源复用。
"""
from __future__ import annotations

import os
import re

import requests
from bs4 import BeautifulSoup

_HERE = os.path.dirname(os.path.abspath(__file__))

# 调试 dump 开关（默认关）：把每次查询的原始响应字节落盘，便于定位表格结构。
# 排查时设环境变量打开： set SIMS_DEBUG=1
_DEBUG = os.environ.get("SIMS_DEBUG") == "1"

# 楼栋表：(楼栋ID, 给学生看的名字)
#
# 2026-09-27 实测，结论推翻了之前"要两套名字"的错误认知：
#   登录时把 buildingName 传**空串**，服务端就能按 (buildingId, roomName) 查出 roomId。
#   也就是说，学生只需要会填「哪一栋 + 哪一间」，不需要知道任何内部名字。
#
# 之前以为"登录页显示的名字被拒、必须传服务端内部登录名"是错的。
# 真相是：那几次失败全因为房间号不在该层段（例如拿 4 层的房间号去撞 15-17 楼），
# 房间号对了之后，登录页上那些名字照样能用，空串更稳。
#
# 注意几栋楼被按层拆成了多段（如 2-10层 / 11-20层），每段是独立楼栋、房号不通用，
# 所以名字里必须带层段，不能只写「乔相」。
_BUILDINGS = [
    ("54", "山茶斋"), ("55", "红榴斋"), ("56", "米兰斋"),
    ("57", "海桐斋"), ("58", "桃李斋"), ("59", "凌霄斋"),
    ("61", "银桦斋"), ("63", "木犀轩"), ("64", "丹枫轩"),
    ("65", "紫檀轩"), ("66", "石楠轩"), ("67", "苏铁轩"),
    ("68", "芸香阁"), ("69", "丁香阁"), ("70", "文杏阁"),
    ("71", "海棠阁"), ("72", "疏影阁"), ("73", "杜衡阁"),
    ("74", "辛夷阁"), ("75", "韵竹阁"), ("76", "云杉轩"),
    ("77", "紫藤轩"), ("8147", "留学生公寓"),
    ("6363", "乔林11-12层"), ("6364", "乔木11-12层"),
    ("6121", "乔林阁1-10层"), ("6122", "乔木阁1-10层"),
    ("7724", "乔梧阁2-10层"), ("7725", "乔梧阁11-20"),
    ("6875", "春笛3-8楼"), ("6876", "夏筝3-17楼"),
    ("6877", "秋瑟3-8楼"), ("6878", "冬筑3-6楼"),
    # 冬筑整栋 17 层，学校拆成 4 段，下拉里都有。下面这几段是实测能查通的：
    # 8240/701、8241/1101、8242/1501、7119/901、7828/901（房号是测试时随手取的样本）。
    # 关键：房间号必须落在该段的层段内（拿 4 层的房间号去撞 15-17 楼必然查不到）。
    ("8240", "冬筑7-10楼"), ("8241", "冬筑11-14楼"), ("8242", "冬筑15-17楼"),
    ("7119", "春笛9-17楼"), ("7828", "秋瑟9-17楼"),
]


def get_building_tree():
    """返回楼栋下拉选项：[{name, id}, ...]，name 是学生在校园网上能认出来的名字。"""
    return [{"name": shown, "id": bid} for bid, shown in _BUILDINGS]


# 校区兜底表：值是学校登录页里那个 `client` 下拉的固定编号。
# 正常走 /api/detect 从页面上现取（换了校区、学校改了地址都能跟上），
# 这张表只在**探测不到服务器**时当兜底，免得连校区都选不了。
_CLIENTS = [
    ("192.168.84.1", "北校区"),
    ("192.168.84.110", "南校区"),
    ("172.21.101.11", "西丽校区"),
    ("192.168.84.87", "深大新斋区"),
]


def get_client_tree():
    """校区下拉选项：[{value, name}, ...]。"""
    return [{"value": v, "name": n} for v, n in _CLIENTS]


def client_name_of(value: str):
    """校区编号 -> 校区名（兜底表里没有返回 None）。"""
    for v, n in _CLIENTS:
        if v == (value or "").strip():
            return n
    return None


def display_name_of(bid):
    """楼栋 ID -> 给学生看的名字；表里没有返回 None。"""
    for b_id, shown in _BUILDINGS:
        if b_id == bid:
            return shown
    return None


def id_of(building: str):
    """给学生看的名字（如「冬筑3-6楼」）-> 楼栋 ID，找不到返回 None。"""
    for bid, shown in _BUILDINGS:
        if shown == building:
            return bid
    return None


def resolve_dorm(building: str, room: str, building_id: str = ""):
    """把「楼栋名 + 房号」解析成服务端需要的 (buildingId, roomName)。

    楼栋名必须跟下拉里的一致（如「冬筑3-6楼」）；传楼栋 ID 也认。
    `building_id` 来自配置（由页面上的下拉直接带过来），有它就以它为准 ——
    这样即使楼栋名不在内置表里（学校新加了楼栋、或者换了个校区），也能正常查询。
    """
    building = (building or "").strip()
    room = (room or "").strip()
    building_id = str(building_id or "").strip()
    if not room:
        raise ValueError("请填写房间号")
    if building_id:
        if not building_id.isdigit():
            raise ValueError(f"楼栋编号格式不对（现在是「{building_id}」）")
        return building_id, room
    if not building:
        raise ValueError("请先选择楼栋")
    bid = building if building.isdigit() else id_of(building)
    if bid is None:
        raise ValueError(f"没有找到楼栋「{building}」，请从下拉列表里选一个")
    return bid, room


# ── 校园网自检：探测服务器 + 拉校区/楼栋列表（给"小白一键配置"用）─────────────

def fetch_login_html(server: str, client: str = "", timeout: int = 8) -> str:
    """取登录页 HTML（不需要登录）。

    学校登录页本身带两张关键下拉：
      · `client`  —— 校区（北校区 / 南校区 / 西丽校区 / 深大新斋区）
      · 楼栋      —— **按校区不同而不同**，所以必须带 client 再取一次
    """
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; szu-electricity/1.0)"})
    params = {"task": "station"}
    if client:
        params["client"] = client
    r = s.get(f"{server.rstrip('/')}/login.do", params=params, timeout=timeout)
    r.raise_for_status()
    return ElectricityScraper._decode(r)


def parse_clients(html: str):
    """从登录页解析校区下拉，返回 [{'value': '192.168.84.110', 'name': '南校区'}, ...]。

    这张表是固定的（深大四个校区/片区），但它决定了楼栋列表，所以必须由页面提供而不是硬编码。
    """
    m = re.search(
        r'<select[^>]*name=["\']client["\'][^>]*>([\s\S]*?)</select>', html, re.I
    )
    if not m:
        return []
    out = []
    for om in re.finditer(r"<option([^>]*)>([^<]*)</option>", m.group(1)):
        val = re.search(r'value=["\']([^"\']*)["\']', om.group(1))
        name = om.group(2).strip()
        if val and val.group(1).strip() and name:
            out.append({"value": val.group(1).strip(), "name": name})
    return out

_NUM_RE = re.compile(r"[-+]?\d*\.?\d+")
_DATE_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})")


def parse_buildings(html: str):
    """从登录页 HTML 解析楼栋下拉的 id 列表，返回 [(id, 名称), ...]；解析不到返回 [].

    登录页里有多个 select（client / buildingId 等），哪个是楼栋下拉要看选项数：
    取"有效选项（value 全数字且有文本）最多"的那个。

    注意：南校区默认只列这一组楼栋，其它校区的要切「校区」下拉才出现。
    这里拿到的 id 用来做体检——学校若新增楼栋，会出现本表没有的 id。
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001
        return []

    best, seen = [], set()
    for sel in soup.find_all("select"):
        opts = []
        for tag in sel.find_all("option"):
            value = (tag.get("value") or "").strip()
            name = tag.get_text(strip=True)
            if value.isdigit() and name:
                if value not in seen:
                    seen.add(value)
                    opts.append((value, name))
        if len(opts) > len(best):
            best = opts
    return best


def _normalize_date(text):
    """从学校表格的日期字符串中提取 YYYY-MM-DD。"""
    if not text:
        return None
    m = _DATE_RE.search(str(text))
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _to_float(text):
    if text is None:
        return None
    m = _NUM_RE.search(str(text))
    return float(m.group()) if m else None


class ElectricityError(Exception):
    """查询或解析过程中可预期的错误（网络/未登录/结构变化）。"""


class ElectricityScraper:
    def __init__(self, server, client, roomid, roomName, building="", timeout=10):
        self.server = server.rstrip("/")
        self.client = client
        self.roomid = str(roomid)
        self.roomName = str(roomName)
        self.building = building or ""
        self.timeout = int(timeout)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; szu-electricity/1.0)",
                "Referer": f"{self.server}/login.do?task=station&client={self.client}",
            }
        )
        # 校内服务器必须直连，绝不走系统/Clash 代理
        self.session.proxies = {"http": None, "https": None}
        self.session.trust_env = False
        self._logged = False

    def _print_cookies(self, label):
        jar = self.session.cookies
        names = [c.name for c in jar]
        print(f"[scraper debug] cookies after {label}: {names}")
        for c in jar:
            print(f"    {c.name}={c.value[:40] if len(c.value) > 40 else c.value} domain={c.domain} path={c.path}")

    def login(self):
        # 1) 拉登录页，建立会话、拿到 JSESSIONID（空 session）
        r0 = self.session.get(
            f"{self.server}/login.do",
            params={"task": "station", "client": self.client},
            timeout=self.timeout,
        )
        r0.raise_for_status()

        # 1.5) 登录页体检：看看学校有没有新增本表不认识的楼栋（只报警告，不参与登录）
        live = parse_buildings(self._decode(r0))
        if live:
            known = {bid for bid, _ in _BUILDINGS}
            unknown = [bid for bid, _ in live if bid not in known]
            # 登录页默认下拉只列南校区这一组，未收录的 id 多半属于别的校区，
            # 所以这里只做信息输出，不报 WARN，避免制造假警报。
            print(
                f"[scraper debug] login page shows {len(live)} building options, "
                f"{len(unknown)} not in built-in table (unknown ids: {unknown})"
            )

        # 楼栋候选。**只要 self.building 有值，就只试它一个，失败直接报错。**
        # 为什么不能"失败后继续遍历"：乔相被拆成 2-10层(6877) 和 11-20层(6878) 两个独立楼栋，
        # 房号不通用。旧逻辑会在 6877 失败后接着用 6878 登录成功，静默返回另一个层段的数据 ——
        # 学生选了一个层段里的房号，拿到的是另一个层段的表，数字看着正常，其实是错的。
        # 查不到就是跟宿舍信息对不上，宁可报错也不给错数据。
        if self.building:
            tried = [self.building]
        else:
            # 老配置/兜底：没有指定楼栋，按表依次试
            tried = [bid for bid, _ in _BUILDINGS]
            tried.append("")

        total = len(tried)
        for idx, bid in enumerate(tried, 1):
            # buildingName 固定传空。2026-09-27 实测：登录页那个隐藏字段学生在页面上
            # 根本不用填，服务端靠 (buildingId, roomName) 就能定位到房间。
            # 之前传页面名（冬筑3-6楼）反而被拒，传空最省事也最稳。
            bname = ""
            # 楼栋下拉的 onchange 会 GET login.do?...&buildingId= 来切换校区楼栋上下文，
            # 这里忠实复刻：先 GET 暖一下该楼栋的 session，再 POST 登录
            if bid:
                try:
                    self.session.get(
                        f"{self.server}/login.do",
                        params={
                            "task": "station",
                            "client": self.client,
                            "buildingId": bid,
                        },
                        timeout=self.timeout,
                    )
                except requests.exceptions.RequestException:
                    pass
            data = {
                "client": self.client,
                "buildingId": bid,
                "buildingName": bname,
                "roomName": self.roomName,
                "select": " 查询 ",
            }
            try:
                r1 = self.session.post(
                    f"{self.server}/login.do", data=data, timeout=self.timeout
                )
            except requests.exceptions.RequestException as e:
                print(f"[scraper debug] login POST #{idx} error: {e}")
                continue
            txt = self._decode(r1)
            # 成功页特征：无 loginForm，且含 selectListForm + roomId 隐藏字段
            ok = ("loginForm" not in txt) and ("selectListForm" in txt) and ("roomId" in txt)
            print(
                f"[scraper debug] login try #{idx}/{total} buildingId={bid!r} "
                f"bname={bname!r} -> {'OK' if ok else 'still login page'}"
            )
            if ok:
                self.building = bid
                self._logged = True
                self._extract_roomid(txt)
                if _DEBUG:
                    try:
                        with open(os.path.join(_HERE, "login_response.html"), "wb") as f:
                            f.write(r1.content)
                    except OSError:
                        pass
                # roomid 是派生值：服务端按房间名匹配，登录页隐藏字段里的才是真正内部 id，
                # 不写回配置（换宿舍后旧 roomid 会让你查到上一个房间，且查错了还不报错）。
                # 把 Referer 设成登录成功页 URL（带 buildingId），selectList.do 拦截器据此放行
                self.session.headers.update(
                    {
                        "Referer": f"{self.server}/login.do?task=station&"
                        f"client={self.client}&buildingId={bid}"
                    }
                )
                self._print_cookies("login OK")
                print(
                    f"[scraper debug] login OK with buildingId={bid!r} "
                    f"buildingName={bname!r} roomid={self.roomid!r}"
                )
                return
        # 都失败了
        self._logged = True
        if self.building:
            # 报错要给学生在校园网上认得出的名字（秋瑟3-8楼），不是内部 id
            bname = display_name_of(self.building) or self.building
            raise ElectricityError(
                f"查不到：楼栋「{bname}」里没有房间 {self.roomName}。\n"
                f"同名楼常按层拆成多段（如 2-10层 / 11-20层），房号不通用 —— "
                f"请回「修改宿舍」重新选准确的那一段。\n"
                f"房间号请照门禁牌/宿管登记写（3~4 位，如 301）；"
                f"如果这一栋被拆成几段，也要选对所在那一段。"
            )
        raise ElectricityError(
            "登录失败：遍历全部候选楼栋后仍停留在登录页，请确认 roomName（房间号）是否正确、是否连在校内网"
        )

    def _extract_roomid(self, html):
        m = re.search(r'name=["\']roomId["\'][^>]*value=["\']([^"\']+)["\']', html)
        if not m:
            m = re.search(r'name=["\']roomid["\'][^>]*value=["\']([^"\']+)["\']', html)
        if m:
            self.roomid = m.group(1)
            print(f"[scraper debug] extracted roomid={self.roomid!r} from login page")
        else:
            print(
                f"[scraper debug] roomid not found in page, keep config roomid={self.roomid!r}"
            )

    @staticmethod
    def _decode(resp: requests.Response) -> str:
        ctype = resp.headers.get("Content-Type", "")
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        enc = m.group(1).lower() if m else "gbk"
        if enc in ("iso-8859-1", "latin1"):
            enc = "gbk"
        try:
            return resp.content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            return resp.content.decode("gbk", errors="ignore")

    def query(self, begin: str, end: str, record_type: int = 2):
        if not self._logged:
            self.login()
        records = self._do_select(begin, end, record_type)
        if records is None:
            # 被踢回登录页：登录态不稳定，重登一次再试（仅重试一次，避免死循环）
            print("[scraper debug] selectList 返回登录页，触发一次重新登录并重试")
            self._logged = False
            self.login()
            records = self._do_select(begin, end, record_type)
            if records is None:
                raise ElectricityError(
                    "查询被重定向回登录页，登录态不稳定，请稍后重试或更换网络后重试"
                )
        if record_type == 2:
            records = self._dedup_usage(records)
        return records

    @staticmethod
    def _dedup_usage(records):
        """翻页合并后按日期去重（页边界可能重复行），并确保按日期升序。"""
        seen = {}
        for r in records:
            d = r.get("date")
            if d:
                seen[d] = r  # 后者覆盖前者，保留最后一次
        return [seen[d] for d in sorted(seen)]

    def query_purchase(self, begin: str, end: str):
        """查询购电记录（type=1）。

        返回：表头 + 数据行(最新在前)。不记录/不统计任何购电人信息。

        没有「账户余电」列：那个值只能靠"当日总购电量 - 当日总用电量"算，而服务端每天
        只在 23:59 存一次快照，所以**当天**的购电行永远算不出余电（2026-09-28 实测：
        09-28 早上连着三笔购电，余电全是"—"，而 09-26、09-22 的历史行有值）。
        一列半空的占位没意义，整列不做 —— 和「今日用电」一个道理：拿不到就不显示。
        """
        if not self._logged:
            self.login()
        raw = self._do_select(begin, end, 1, parser=self._parse_purchase)
        if raw is None:
            print("[scraper debug] 购电查询返回登录页，触发一次重新登录并重试")
            self._logged = False
            self.login()
            raw = self._do_select(begin, end, 1, parser=self._parse_purchase)
            if raw is None:
                raise ElectricityError(
                    "购电记录查询被重定向回登录页，登录态不稳定，请稍后重试或更换网络后重试"
                )

        headers = raw["headers"]
        rows = raw["rows"]
        # 翻页合并去重：整行相同的视为重复（页边界）
        _seen = set()
        _deduped = []
        for r in rows:
            key = tuple(r)
            if key in _seen:
                continue
            _seen.add(key)
            _deduped.append(r)
        rows = _deduped

        date_i = next(
            (i for i, h in enumerate(headers) if any(k in h for k in ("购买日期", "日期"))),
            None,
        )

        # 按完整日期时间升序整理（最老在前），再整体反转成最新在前
        enriched = []
        for row in rows:
            dstr = row[date_i] if date_i is not None else ""
            enriched.append((dstr, row))
        enriched.sort(key=lambda x: x[0] or "", reverse=False)
        out_rows = [row for _, row in enriched]
        out_rows.reverse()

        return {"headers": headers, "rows": out_rows}

    # 学校 selectList.do 每页固定 19 条，翻页通过 URL ?pageNo=N（POST 同样的表单字段）
    # 页面文本形如：共找到&nbsp;89&nbsp;条数据,&nbsp;每页显示19条记录,&nbsp;当前页:&nbsp;1 / 5
    _PAGE_RE = re.compile(
        r"每页显示\D*(\d+)\D*条.*?当前页\D*(\d+)\D*/\D*(\d+)", re.S
    )

    def _post_page(self, begin, end, record_type, page_no, dump_name=None):
        """POST 一次 selectList.do（可指定 pageNo），返回解码后的 HTML 或 None(被踢回登录页)。"""
        url = f"{self.server}/selectList.do"
        if page_no and page_no > 1:
            url += f"?pageNo={page_no}"
        data = {
            "hiddenType": "0",
            "isHost": "0",
            "beginTime": begin,
            "endTime": end,
            "type": str(record_type),
            "client": self.client,
            "roomId": self.roomid,  # 表单真实字段名是 roomId（大写 I）
            "roomName": self.roomName,
            "building": "",  # 与登录成功页隐藏字段一致：building 留空
        }
        print(f"[scraper debug] selectList payload={data} pageNo={page_no}")
        r = self.session.post(url, data=data, timeout=self.timeout)
        print(f"[scraper debug] selectList status={r.status_code} url={r.url}")
        r.raise_for_status()
        if dump_name and _DEBUG:
            try:
                with open(os.path.join(_HERE, dump_name), "wb") as f:
                    f.write(r.content)
            except OSError as e:
                print(f"[scraper debug] dump failed: {e}")
        html = self._decode(r)
        if "loginForm" in html:
            print("[scraper debug] WARN: selectList.do 返回了登录页，说明 session 仍未认证")
            return None
        return html

    @classmethod
    def _page_meta(cls, html):
        """从响应里解析分页信息，返回 (每页条数, 当前页, 总页数)。"""
        m = cls._PAGE_RE.search(html)
        if not m:
            return None
        return int(m.group(1)), int(m.group(2)), int(m.group(3))

    def _fetch_all_pages(self, begin, end, record_type, parser, dump_name):
        """抓取所有分页并合并结果。

        返回 (merged, None) 或 (None, 'login') 表示被踢回登录页。
        - parser 决定单页返回结构：list（用电）或 dict{headers,rows}（购电）。
        """
        html = self._post_page(begin, end, record_type, 1, dump_name)
        if html is None:
            return None, "login"

        meta = self._page_meta(html)
        total_pages = meta[2] if meta else 1
        print(f"[scraper debug] pagination meta={meta} -> total_pages={total_pages}")

        first = parser(html)
        if total_pages <= 1:
            return first, None

        # 后续页
        if isinstance(first, dict):
            merged = {"headers": first.get("headers", []), "rows": list(first.get("rows", []))}
            for p in range(2, total_pages + 1):
                h = self._post_page(begin, end, record_type, p)
                if h is None:
                    print(f"[scraper debug] page {p} 被踢回登录页，停止翻页")
                    break
                part = parser(h)
                merged["rows"].extend(part.get("rows", []))
            return merged, None

        # list 型（用电记录）
        merged = list(first)
        for p in range(2, total_pages + 1):
            h = self._post_page(begin, end, record_type, p)
            if h is None:
                print(f"[scraper debug] page {p} 被踢回登录页，停止翻页")
                break
            merged.extend(parser(h))
        return merged, None

    def _do_select(self, begin: str, end: str, record_type: int, parser=None):
        dump_name = "purchase_response.html" if record_type == 1 else "last_response.html"
        result, err = self._fetch_all_pages(
            begin, end, record_type, parser or self._parse, dump_name
        )
        if err == "login":
            return None
        return result

    @staticmethod
    def _parse(html: str):
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        print(f"[scraper debug] total <table> count={len(tables)}")
        chosen = None
        for ti, t in enumerate(tables):
            txt = t.get_text(" ", strip=True)
            hit = any(
                k in txt
                for k in ("剩余电量", "总用电量", "剩余", "电量", "余额", "房名", "房间", "日期", "用电")
            )
            if ti < 8:
                print(
                    f"[scraper debug] table[{ti}] rows={len(t.find_all('tr'))} "
                    f"hit={hit} head={txt[:100]!r}"
                )
            if chosen is None and hit:
                chosen = t
        table = chosen or (tables[0] if tables else None)
        if table is None:
            raise ElectricityError("返回页面未找到任何 <table>，可能未登录或页面结构已变化")

        rows = table.find_all("tr")
        header = None
        header_idx = -1
        for i, row in enumerate(rows):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            joined = " ".join(cells)
            if any(
                k in joined
                for k in ("剩余电量", "总用电量", "房名", "日期", "剩余", "电量", "余额", "用电", "房间")
            ):
                header = cells
                header_idx = i
                break

        col = {}
        if header:
            for idx, name in enumerate(header):
                if "剩余电量" in name or "剩余" in name or "余额" in name:
                    col["remain"] = idx
                elif "总用电量" in name or "总用电" in name or "用电量" in name or "用电" in name:
                    col["total_use"] = idx
                elif "总购电量" in name or "总购" in name or "购电" in name:
                    col["total_buy"] = idx
                elif "房名" in name or "房间" in name:
                    col["room"] = idx
                elif "日期" in name:
                    col["date"] = idx
        # 找不到表头时，使用已知表结构固定索引（序号,房名,剩余,总用,总购,日期）
        if "remain" not in col:
            col = {"remain": 2, "total_use": 3, "total_buy": 4, "room": 1, "date": 5}

        records = []
        data_rows = rows[header_idx + 1:] if header else rows
        for row in data_rows:
            cells = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cells) < max(col.values()) + 1:
                continue
            raw_date = cells[col.get("date", len(cells) - 1)]
            date = _normalize_date(raw_date)
            if not date:
                continue
            records.append(
                {
                    "date": date,
                    "room": cells[col["room"]] if "room" in col else None,
                    "remain": _to_float(cells[col["remain"]]),
                    "total_use": _to_float(cells[col["total_use"]]),
                    "total_buy": _to_float(cells[col["total_buy"]]),
                }
            )
        if not records and data_rows:
            sample = [c.get_text(strip=True) for c in data_rows[0].find_all("td")]
            print(f"[scraper debug] header={header}")
            print(f"[scraper debug] col map={col}")
            print(f"[scraper debug] first data row={sample}")
        print(f"[scraper] parsed {len(records)} records")
        return records

    @staticmethod
    def _parse_purchase(html: str):
        """购电记录解析：定位 id="oTable" 的数据表格，返回表头与数据行。"""
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        print(f"[scraper debug] purchase total <table> count={len(tables)}")

        # 优先选真正的数据表 id="oTable"
        chosen = soup.find("table", id="oTable")
        if chosen is None:
            # 次优：表头同时出现 序号 + 购买电量 的表
            for ti, t in enumerate(tables):
                txt = t.get_text(" ", strip=True)
                if "购电" in txt and "序号" in txt and "购买电量" in txt:
                    chosen = t
                    break
        if chosen is None:
            # 兜底：关键词匹配
            for t in tables:
                txt = t.get_text(" ", strip=True)
                if any(k in txt for k in ("购电", "金额", "充值")):
                    chosen = t
                    break
        if chosen is None:
            raise ElectricityError("未找到购电记录表格，可能该区间无购电记录或页面结构已变化")

        rows = [r for r in chosen.find_all("tr")]
        headers = []
        data_rows = []
        for r in rows:
            cells = [c.get_text(strip=True) for c in r.find_all(["td", "th"])]
            if not cells:
                continue
            if not headers:
                headers = cells
                continue
            # 只保留真正的数据行：首列是整数序号，且列数不少于表头
            if len(cells) < len(headers):
                continue
            if re.match(r"\d+$", cells[0]):
                data_rows.append(cells)
        print(
            f"[scraper debug] purchase parsed headers={headers} rows={len(data_rows)}"
        )

        # 尽力识别汇总列
        summary = {}
        amount_idx = next(
            (i for i, h in enumerate(headers) if any(k in h for k in ("金额", "元", "费用", "总价", "实收"))),
            None,
        )
        deg_idx = next(
            (i for i, h in enumerate(headers) if any(k in h for k in ("购电", "度数", "电量", "本次", "量"))),
            None,
        )
        if amount_idx is not None:
            total = 0.0
            for row in data_rows:
                if amount_idx < len(row):
                    v = _to_float(row[amount_idx])
                    if v is not None:
                        total += v
            summary["amount"] = round(total, 2)
        if deg_idx is not None:
            total = 0.0
            for row in data_rows:
                if deg_idx < len(row):
                    v = _to_float(row[deg_idx])
                    if v is not None:
                        total += v
            summary["degree"] = round(total, 2)
        print(f"[scraper debug] purchase summary={summary}")
        return {"headers": headers, "rows": data_rows, "summary": summary}
