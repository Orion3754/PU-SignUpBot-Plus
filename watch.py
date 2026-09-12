# -*- coding: utf-8 -*-
"""
watch.py —— 常驻监控模式：自动发现新活动 + 到点秒抢
====================================================

抢报名规则（按用户设定）：
  * 只抢「劳动教育类」
  * 全校范围，不限院系 / 年级 / 学分 / 是否审核
  * 余位为 0 也照样尝试
  * 符合条件的有几个报几个，不设上限

用法：
  python watch.py                  # 常驻监控（默认每 30 秒扫描一次）
  python watch.py --interval 15    # 改成 15 秒扫描一次（发现更快，风控风险略高）
  python watch.py --once           # 只扫描一轮就退出（手动查有没有新活动）
  python watch.py --dry-run        # 只检测和打印，不真的报名（验证规则用）
  python watch.py --join-existing  # 首次运行就把当前已有活动也纳入报名

状态文件：watch_state.json   记录已见过的活动，避免重复报名
日志文件：logs/watch-YYYY-MM-DD.log

工作流程：
  1. 每 N 秒查询一次「劳动教育类」的活动列表（未开始 + 进行中）
  2. 发现没见过的活动 → 拉详情判断报名窗口
  3. 报名还没开放 → 交给 ActivityBot 挂线程，到点精确秒抢
     报名已开放   → 立刻多线程开抢
  4. 结果写日志 + 发邮件（config.py 里 ENABLE_EMAIL_NOTIFICATION 控制）
  5. 抢失败的活动按 RETRY_INTERVAL 间隔重试，直到报名截止或达到次数上限

注意：首次运行默认「只建立基线」，把当前已存在的活动记录下来但不报名，
      之后新出现的活动才会自动抢（想连已有的也抢，加 --join-existing）。
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

import requests
from loguru import logger

from utils.headers import HEADERS_ACTIVITY
from utils.tools import get_token, get_activity_type, get_info
from utils.activity_bot import ActivityBot

# ==================== 可调参数 ====================
CATEGORY_NAME = "劳动教育类"   # 只抢这个分类（改成 "全部" 表示不限制）
POLL_INTERVAL = 30             # 轮询间隔（秒）
PAGE_LIMIT = 50                # 每页拉取条数
MAX_PAGES = 3                  # 每个状态最多翻几页
STATUSES = (1, 2)              # 1=活动未开始 2=活动进行中（报名可能仍开放）
RETRY_INTERVAL = 300           # 抢失败后间隔多少秒重试一次
MAX_ATTEMPTS = 24              # 单个活动最多尝试抢几次（24×5分钟≈2小时；
                               # PU 允许报名后 60 分钟内取消，中途可能放出空位）

STATE_FILE = "watch_state.json"
LIST_URL = "https://apis.pocketuni.net/apis/activity/list"
TIME_FMTS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S")


# ==================== 基础工具 ====================
def setup_logging() -> None:
    logger.remove()
    os.makedirs("logs", exist_ok=True)
    logger.add(
        "logs/watch-{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="30 days",
        compression="zip",
        enqueue=True,
        encoding="utf-8",
        level="INFO",
    )
    logger.add(
        sys.stdout,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
        level="INFO",
    )


def parse_dt(value) -> Optional[datetime]:
    """把接口返回的时间字符串转成 datetime，失败返回 None"""
    if not value:
        return None
    text = str(value).strip()
    for fmt in TIME_FMTS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def load_user() -> Dict:
    path = "user_data.json"
    if not os.path.exists(path):
        logger.error("找不到 user_data.json，请先运行 python main.py 录入账号")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not data:
        logger.error("user_data.json 里没有账号，请先运行 python main.py 录入账号")
        sys.exit(1)
    return data[0]


def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
            # 上次程序中途退出留下的 pending，转成 failed 以便重试
            for rec in state.get("seen", {}).values():
                if rec.get("action") == "pending":
                    rec["action"] = "failed"
            return state
        except Exception as e:
            logger.warning(f"状态文件读取失败，将重建：{e}")
    return {"baseline_done": False, "seen": {}}


def save_state(state: Dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# ==================== PU 接口客户端 ====================
class PuClient:
    """负责登录态维护 + 活动列表/详情查询"""

    def __init__(self, user: Dict):
        self.user = user
        self.token = user.get("token") or ""
        if not self.token:
            self.refresh()

    def refresh(self) -> bool:
        token = get_token(self.user)
        if token:
            self.token = token
            self.user["token"] = token
            logger.info("登录态已刷新")
            return True
        logger.error("重新登录失败，请检查账号密码")
        return False

    def _headers(self) -> Dict:
        headers = HEADERS_ACTIVITY.copy()
        headers["Authorization"] = f"Bearer {self.token}:{self.user.get('sid')}"
        return headers

    def post(self, url: str, payload: Dict) -> Optional[requests.Response]:
        """带一次 401 自动重登的 POST"""
        for _ in range(2):
            try:
                resp = requests.post(url, headers=self._headers(), json=payload, timeout=15)
            except requests.RequestException as e:
                logger.error(f"请求失败 {url}：{e}")
                return None
            if resp.status_code == 401:
                logger.warning("登录态失效，正在重新登录…")
                if not self.refresh():
                    return None
                continue
            return resp
        return None

    def find_category_id(self) -> Optional[int]:
        """在学校的筛选选项里找出「劳动教育类」的 id"""
        types = get_activity_type(self.token, self.user.get("sid")) or []
        for activity_type in types:
            if activity_type.get("name") != "活动分类":
                continue
            for info in activity_type.get("infoList", []):
                if info.get("name") == CATEGORY_NAME:
                    return info.get("id")
        return None

    def list_activities(self, category_id: Optional[int]) -> Dict[str, Dict]:
        """拉取目标分类下的活动，返回 {活动id: 列表项}"""
        found: Dict[str, Dict] = {}
        for status in STATUSES:
            for page in range(1, MAX_PAGES + 1):
                payload = {
                    "page": page,
                    "limit": PAGE_LIMIT,
                    "sort": 0,
                    "puType": 0,
                    "status": status,
                }
                if category_id:
                    payload["categorys"] = [category_id]
                resp = self.post(LIST_URL, payload)
                if resp is None or resp.status_code != 200:
                    break
                try:
                    items = (resp.json().get("data") or {}).get("list") or []
                except ValueError:
                    break
                for item in items:
                    if item.get("id"):
                        found[str(item["id"])] = item
                if len(items) < PAGE_LIMIT:
                    break
        return found

    def detail(self, activity_id: str) -> Dict:
        return get_info(activity_id, self.token, self.user.get("sid")) or {}


# ==================== 判断逻辑（纯函数，方便测试） ====================
def decide(detail: Dict, now: datetime) -> Tuple[str, str]:
    """
    判断某个活动要不要抢。
    :return: (action, reason)，action ∈ {"join", "skip"}
    """
    if not detail:
        return "skip", "拉不到活动详情"

    category = detail.get("categoryName")
    if CATEGORY_NAME != "全部" and category != CATEGORY_NAME:
        return "skip", f"分类是「{category}」，不是「{CATEGORY_NAME}」"

    join_end = parse_dt(detail.get("joinEndTime"))
    if join_end and now > join_end:
        return "skip", f"报名已于 {join_end} 截止"

    join_start = parse_dt(detail.get("joinStartTime"))
    if join_start and join_start > now:
        return "join", f"报名将于 {join_start} 开放，挂线程到点秒抢"

    return "join", "报名已开放，立即开抢"


# ==================== 抢报名 ====================
def _join_worker(user: Dict, activity_id: int, key: str, name: str,
                 inflight: set, state: Dict, lock: threading.Lock) -> None:
    """activity_id 必须是数字（PU 接口只认数字 id，传字符串会返回 code=500 空数据）；
    key 是状态表 / 队列里用的字符串键。"""
    ok = False
    try:
        logger.info(f"▶ 开始处理：{name}（id={activity_id}）")
        bot = ActivityBot(user)
        bot.sync_server_time(activity_id)
        bot.signup(activity_id)          # 内部完成：等待 / 对时 / 多线程秒抢 / 邮件通知
        ok = bool(bot.signup_flags.get(activity_id))
    except Exception as e:
        logger.exception(f"抢报名线程异常 {name}：{e}")
    finally:
        inflight.discard(key)

    with lock:
        rec = state["seen"].setdefault(key, {})
        rec["action"] = "joined" if ok else "failed"
        rec["last_result"] = "报名成功" if ok else "未抢到"
        rec["attempts"] = int(rec.get("attempts", 0)) + 1
        rec["last_attempt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)

    if ok:
        logger.success(f"✅ 报名成功：{name}（id={activity_id}）")
    else:
        logger.error(f"❌ 报名失败：{name}（id={activity_id}）"
                     f"—— 已尝试 {state['seen'][key]['attempts']} 次")


_THREADS: list = []   # 记录所有抢报名线程，退出时等它们收尾


def wait_threads(timeout: float = 5.0) -> None:
    """等待抢报名线程收尾；超时就不等（未完成的活动下次启动会自动重试）"""
    alive = [t for t in _THREADS if t.is_alive()]
    if not alive:
        return
    logger.info(f"等待 {len(alive)} 个抢报名任务收尾（最多 {timeout:.0f} 秒）…")
    deadline = time.time() + timeout
    for t in alive:
        t.join(max(0.1, deadline - time.time()))


def spawn_join(user: Dict, activity_id, name: str,
               inflight: set, state: Dict, lock: threading.Lock) -> None:
    """activity_id 统一转成数字再交给引擎（PU 接口只认数字 id）；
    状态表 / 队列用字符串键，避免重启后从 JSON 读回来的键类型不一致。"""
    try:
        aid = int(activity_id)
    except (TypeError, ValueError):
        logger.error(f"活动 id 不是数字，跳过：{activity_id!r}")
        return
    key = str(aid)
    if key in inflight:
        logger.info(f"该活动已在抢报名队列中，跳过：{name}")
        return
    inflight.add(key)
    t = threading.Thread(
        target=_join_worker,
        args=(user, aid, key, name, inflight, state, lock),
        daemon=True,
        name=f"join-{aid}",
    )
    t.start()
    _THREADS.append(t)


# ==================== 主循环 ====================
def build_record(item: Dict, detail: Dict, action: str, reason: str) -> Dict:
    total = detail.get("allowUserCount")
    joined = detail.get("joinUserCount")
    left = None
    if isinstance(total, int) and isinstance(joined, int):
        left = total - joined
    return {
        "name": detail.get("name") or item.get("name"),
        "category": detail.get("categoryName"),
        "joinStartTime": detail.get("joinStartTime"),
        "joinEndTime": detail.get("joinEndTime"),
        "activityStartTime": detail.get("startTime"),
        "credit": detail.get("credit"),
        "quota": f"{joined}/{total}" if total is not None else None,
        "slots_left": left,
        "address": detail.get("address"),
        "action": "pending" if action == "join" else "skip",
        "attempts": 0,
        "reason": reason,
        "first_seen": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="PU 活动常驻监控 + 自动抢报名")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL, help="轮询间隔秒数")
    parser.add_argument("--once", action="store_true", help="只扫描一轮就退出")
    parser.add_argument("--dry-run", action="store_true", help="只检测不报名")
    parser.add_argument("--join-existing", action="store_true",
                        help="首次运行也把当前已有活动纳入报名")
    args = parser.parse_args()

    setup_logging()
    user = load_user()
    logger.info("=" * 60)
    logger.info(f"账号：{user.get('userName')} | 学校 sid：{user.get('sid')}")
    logger.info(f"规则：只抢「{CATEGORY_NAME}」，全校，不限年级/学分/审核，余位 0 也抢")
    logger.info(f"轮询间隔：{args.interval} 秒 | dry-run：{args.dry_run} | once：{args.once}")
    logger.info("=" * 60)

    client = PuClient(user)
    category_id = client.find_category_id()
    if category_id is None:
        logger.warning(f"没找到「{CATEGORY_NAME}」分类 id，改为逐条拉详情判断分类")
    else:
        logger.info(f"「{CATEGORY_NAME}」分类 id = {category_id}")

    state = load_state()
    lock = threading.Lock()
    inflight: set = set()

    while True:
        cycle_start = datetime.now()
        try:
            items = client.list_activities(category_id)
        except Exception as e:
            logger.exception(f"拉取活动列表异常：{e}")
            items = {}

        new_ids = [aid for aid in items if aid not in state["seen"]]
        logger.info(f"本轮扫描到 {len(items)} 个「{CATEGORY_NAME}」活动，新活动 {len(new_ids)} 个")

        baseline_mode = (not state.get("baseline_done")) and (not args.join_existing)

        if baseline_mode:
            for aid, item in items.items():
                state["seen"][aid] = {
                    "name": item.get("name"),
                    "action": "baseline",
                    "reason": "首次运行建立基线，不报名",
                    "first_seen": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            state["baseline_done"] = True
            save_state(state)
            logger.info(f"【首次运行】已把当前 {len(items)} 个活动记为基线（不报名）。")
            logger.info("从现在开始新出现的活动才会自动抢。")
        else:
            state["baseline_done"] = True
            for aid in new_ids:
                item = items[aid]
                detail = client.detail(aid) or item
                action, reason = decide(detail, datetime.now())
                record = build_record(item, detail, action, reason)
                with lock:
                    state["seen"][aid] = record
                    save_state(state)
                logger.info(
                    f"🆕 新活动：{record['name']}（id={aid}）\n"
                    f"   分类={record['category']} | 学分={record['credit']} | 名额={record['quota']}"
                    f"（余 {record['slots_left']}）\n"
                    f"   报名 {record['joinStartTime']} ~ {record['joinEndTime']} | 地点={record['address']}\n"
                    f"   判定：{reason}"
                )
                if action == "join":
                    if args.dry_run:
                        logger.info(f"[DRY-RUN] 本应报名：{record['name']}")
                    else:
                        spawn_join(user, aid, record["name"], inflight, state, lock)
                else:
                    logger.info(f"⏭ 跳过：{record['name']} —— {reason}")

        # ---- 失败重试 ----
        if not args.dry_run:
            now = datetime.now()
            for aid, rec in list(state["seen"].items()):
                if rec.get("action") != "failed":
                    continue
                if int(rec.get("attempts", 0)) >= MAX_ATTEMPTS:
                    continue
                last = parse_dt(rec.get("last_attempt"))
                if last and (now - last).total_seconds() < RETRY_INTERVAL:
                    continue
                join_end = parse_dt(rec.get("joinEndTime"))
                if join_end and now > join_end:
                    with lock:
                        rec["action"] = "expired"
                        rec["reason"] = "报名已截止，放弃重试"
                        save_state(state)
                    continue
                logger.info(f"🔁 重试抢报名：{rec.get('name')}"
                            f"（第 {int(rec.get('attempts', 0)) + 1} 次）")
                spawn_join(user, aid, rec.get("name") or aid, inflight, state, lock)

        if args.once:
            logger.info("--once 模式：本轮结束，退出。")
            wait_threads(10.0)
            break

        elapsed = (datetime.now() - cycle_start).total_seconds()
        time.sleep(max(1.0, args.interval - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.warning("收到停止信号（Ctrl+C），正在收尾…")
        wait_threads(5.0)
        logger.warning("监控已退出。没抢完的活动已记在 watch_state.json，下次启动会自动重试。")
