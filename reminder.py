"""电量告警：余额低于阈值时发一条企业微信群机器人消息。

设计要点：
- **只跑一次就退出**，由 Windows 任务计划程序定时调用（schtasks），
  不用 time.sleep 常驻 —— 笔记本休眠时 sleep 不计入，会滞留。
- 同一天只推一次，记录在 data/last_alert_date。
- webhook key 只从 .env 读，**绝不写进 config.json / 不提交 GitHub**。
- 任何异常都吞掉并打日志，绝不让定时任务因为抛错而"看起来失败"。

配置（.env）：
    SIMS_WEBHOOK_KEY=<企业微信机器人 key>
    SIMS_ALERT_THRESHOLD=20

运行：
    python reminder.py
"""
from __future__ import annotations

import datetime
import os
import sys

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from app import load_config, query_records, compute_trend  # noqa: E402

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
STATE_DIR = os.path.join(BASE, "data")
SENT_DATE_FILE = os.path.join(STATE_DIR, "last_alert_date")


def read_env(path=None):
    """极简 .env 解析（不依赖 python-dotenv）。"""
    env = {}
    path = path or os.path.join(BASE, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    # 系统环境变量优先级更高，方便 CI 注入
    for k in ("SIMS_WEBHOOK_KEY", "SIMS_ALERT_THRESHOLD"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def already_sent_today():
    try:
        with open(SENT_DATE_FILE, encoding="utf-8") as f:
            return f.read().strip() == datetime.date.today().isoformat()
    except OSError:
        return False


def mark_sent_today():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(SENT_DATE_FILE, "w", encoding="utf-8") as f:
            f.write(datetime.date.today().isoformat())
    except OSError as e:
        print(f"[reminder] 写入发送日期失败（不影响发送）: {e}")


def send_webhook(key: str, text: str) -> bool:
    try:
        r = requests.post(
            f"{WEBHOOK}?key={key}",
            json={"msgtype": "markdown", "markdown": {"content": text}},
            timeout=15,
        )
        body = r.text or ""
        ok = r.status_code == 200 and '"errcode":0' in body
        print(f"[reminder] webhook 返回 {r.status_code} {body[:120]}")
        return ok
    except requests.RequestException as e:
        print(f"[reminder] 发送失败: {type(e).__name__} {e}")
        return False


def main() -> int:
    env = read_env()
    key = env.get("SIMS_WEBHOOK_KEY", "")
    if not key:
        print("[reminder] 未配置 SIMS_WEBHOOK_KEY，跳过。")
        print("           企业微信建群 → 群设置 → 添加群机器人 → 复制 key，写到本目录的 .env 里。")
        return 0

    try:
        threshold = float(env.get("SIMS_ALERT_THRESHOLD", "20"))
    except ValueError:
        threshold = 20.0

    # 查询窗口取最近 7 天而不是只查今天：学校每天凌晨才结算当天的用电记录，
    # 白天查"今天"必然 0 条拿不到余额；取 7 天还能让日均/预计天数有足够差分点。
    end = datetime.date.today()
    begin = end - datetime.timedelta(days=6)
    try:
        cfg = load_config()
        room_name = f"{cfg.get('building', '')} {cfg.get('room', '')}".strip() or "宿舍"
        records = query_records(cfg, begin.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), 2)
        trend = compute_trend(records)
        remain = trend.get("current_remain")
        avg = trend.get("avg_daily") or 0
        est = trend.get("estimated_days")
    except Exception as e:  # noqa: BLE001 —— 定时脚本不许因为异常"假失败"
        print(f"[reminder] 查询失败：{type(e).__name__} {e}")
        return 0

    if remain is None:
        print("[reminder] 未拿到余额，跳过。")
        return 0

    print(f"[reminder] 房间 {room_name} 余额 {remain} 度（阈值 {threshold}），"
          f"日均 {avg}，预计可用 {est} 天")

    if remain >= threshold:
        print("[reminder] 余额充足，不发送。")
        return 0

    if already_sent_today():
        print("[reminder] 今天已经提醒过了，跳过。")
        return 0

    days = f"{est:g} 天" if est else "未知"
    content = (
        f"⚡ {room_name} 电量告警\n"
        f"> 当前余额：**{remain:.2f} 度**（阈值 {threshold:.0f} 度）\n"
        f"> 日均用电 {avg:.2f} 度，预计还能用 {days}\n"
        f"> 查询时间 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    if send_webhook(key, content):
        mark_sent_today()
        print("[reminder] 已发送。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
