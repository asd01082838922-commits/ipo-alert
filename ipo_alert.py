#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KRX KIND IPO 알림 봇
--------------------
Task 1 (invstg): 상장예비심사 신청/결과 알림
    - 새 청구(상장신청) 건, 심사결과 확정(승인/철회/미승인 등) 감지 -> 텔레그램
    - 스팩 IPO(스팩 신규상장)는 제외, 스팩합병(소멸/존속)은 포함
Task 2 (listing): 코스닥/코스피 신규상장 전 영업일 알림
    - 다음 영업일에 상장 예정인 종목을 그 전 영업일 아침에 알림

데이터 출처: https://kind.krx.co.kr  (공개 공시 데이터)

환경변수:
    TELEGRAM_BOT_TOKEN   텔레그램 봇 토큰
    TELEGRAM_CHAT_ID     알림 받을 chat_id

사용:
    python ipo_alert.py invstg     # 예비심사 청구/결과 체크
    python ipo_alert.py listing    # 신규상장 전일 체크
    python ipo_alert.py both       # 둘 다 (Task2는 아침 시간대에만 발송)
    python ipo_alert.py test       # 텔레그램 연결 테스트 메시지 1건
    옵션: --dry-run  (텔레그램 전송 대신 콘솔 출력)
          --force-listing (시간대 무시하고 상장 알림 체크)
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    import holidays as _holidays
except Exception:  # pragma: no cover
    _holidays = None

# --------------------------------------------------------------------------
# 설정
# --------------------------------------------------------------------------
BASE = "https://kind.krx.co.kr"
INVSTG_URL = BASE + "/listinvstg/listinvstgcom.do"
OFFERING_URL = BASE + "/listinvstg/pubofrprogcom.do"   # 공모기업 진행현황 (상장예정일 포함)
DETAIL_URL = BASE + "/listinvstg/listinvstgcom.do?method=searchListInvstgCorpDetail&bizProcNo={}"

STATE_DIR = Path(__file__).resolve().parent / "state"
SEEN_INVSTG = STATE_DIR / "seen_invstg.json"
NOTIFIED_LISTING = STATE_DIR / "notified_listing.json"

KST = dt.timezone(dt.timedelta(hours=9))

# 신규상장 전일 알림을 실제로 "발송"하는 KST 시간대 (아침). 이 창 안의 실행에서만 상장 알림을 보냄.
LISTING_HOUR_START = 8   # 08:00 KST
LISTING_HOUR_END = 10    # 09:59 KST 까지

# 시장 필터: 예비심사/상장 모두 코스닥·유가증권(코스피)만. (코넥스 제외)
ALLOWED_MARKETS = {"코스닥", "유가증권"}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}


# --------------------------------------------------------------------------
# 공통 유틸
# --------------------------------------------------------------------------
def now_kst() -> dt.datetime:
    return dt.datetime.now(tz=KST)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


WARM_URL = BASE + "/listinvstg/listinvstgcom.do?method=searchListInvstgCorpMain"


def _warm(session: requests.Session):
    try:
        session.get(WARM_URL, timeout=20)
    except Exception:
        pass


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    _warm(s)   # 세션 쿠키(JSESSIONID) 확보
    return s


def post_html(session: requests.Session, url: str, body: dict) -> str:
    """KIND POST. 간헐적 세션/Akamai 실패에 대비해 세션 재-워밍 후 재시도."""
    last = ""
    for attempt in range(3):
        try:
            r = session.post(url, data=body, timeout=30, allow_redirects=False)
            if r.status_code == 200 and "<html" not in r.text[:200].lower():
                r.encoding = "utf-8"
                return r.text
            last = f"status={r.status_code} len={len(r.text)}"
        except Exception as e:
            last = str(e)
        time.sleep(2)
        _warm(session)   # 재시도 전 세션 재확보
    print(f"[!] POST 실패({url}): {last}", file=sys.stderr)
    return ""


# --------------------------------------------------------------------------
# 텔레그램
# --------------------------------------------------------------------------
class Notifier:
    def __init__(self, dry_run=False):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.dry_run = dry_run
        if not self.dry_run and (not self.token or not self.chat_id):
            print("[!] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 설정되지 않았습니다.",
                  file=sys.stderr)

    def send(self, text: str):
        if self.dry_run:
            print("----- (dry-run) 텔레그램 메시지 -----")
            print(text)
            print("------------------------------------")
            return True
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(3):
            try:
                r = requests.post(url, data=payload, timeout=20)
                if r.status_code == 200:
                    return True
                print(f"[!] 텔레그램 전송 실패 {r.status_code}: {r.text[:200]}",
                      file=sys.stderr)
            except Exception as e:
                print(f"[!] 텔레그램 예외: {e}", file=sys.stderr)
            time.sleep(2)
        return False


# --------------------------------------------------------------------------
# Task 1 : 상장예비심사 신청/결과
# --------------------------------------------------------------------------
def fetch_invstg_rows(session: requests.Session):
    """예비심사 목록 전체를 페이지네이션으로 수집."""
    rows = []
    page = 1
    page_size = 100
    while True:
        body = {
            "method": "searchListInvstgCorpSub",
            "currentPageSize": str(page_size),
            "pageIndex": str(page),
            "orderMode": "2",       # 청구일 기준
            "orderStat": "D",       # 내림차순
            "forward": "listinvstgcom_sub",
            "marketType": "",
            "searchCorpName": "",
            "fromData": "",
            "toDate": "",
        }
        html = post_html(session, INVSTG_URL, body)
        page_rows = parse_invstg_html(html)
        rows.extend(page_rows)
        if len(page_rows) < page_size:
            break
        page += 1
        if page > 20:  # 안전장치
            break
        time.sleep(0.5)
    return rows


def parse_invstg_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("tbody tr"):
        onclick = tr.get("onclick", "") or ""
        m = re.search(r"fnDetailView\('(\d+)'\)", onclick)
        if not m:
            # onclick 이 td/anchor에 있을 수도 있음
            m = re.search(r"fnDetailView\('(\d+)'\)", str(tr))
            if not m:
                continue
        bizno = m.group(1)
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        name_td = tds[0]
        name = (name_td.get("title") or name_td.get_text(strip=True)).strip()
        img = name_td.find("img")
        market = (img.get("alt", "").strip() if img else "")
        out.append({
            "bizno": bizno,
            "name": name,
            "market": market,
            "type": tds[1].get_text(strip=True),
            "apply_date": tds[2].get_text(strip=True),
            "result_date": tds[3].get_text(strip=True),
            "status": tds[4].get_text(strip=True),
            "underwriter": tds[5].get_text(strip=True),
        })
    return out


def is_spac_ipo(name: str, list_type: str) -> bool:
    """스팩 자체의 신규상장(스팩 IPO)인가? -> 제외 대상.
    스팩합병(소멸/존속)은 상장유형에 '합병'이 포함되어 여기서 False."""
    if "합병" in list_type:
        return False
    return "스팩" in name


def passes_invstg_filter(row) -> bool:
    if row["market"] not in ALLOWED_MARKETS:
        return False
    if is_spac_ipo(row["name"], row["type"]):
        return False
    return True


RESULT_EMOJI = {
    "승인": "✅",
    "심사승인": "✅",
    "철회": "⚠️",
    "심사철회": "⚠️",
    "미승인": "❌",
    "심사미승인": "❌",
    "기각": "❌",
    "부결": "❌",
}


def result_icon(status: str) -> str:
    for k, v in RESULT_EMOJI.items():
        if k in status:
            return v
    return "📄"


def is_final_result(status: str, result_date: str) -> bool:
    """결과확정(청구서 접수/심사중이 아닌 최종 상태)인지."""
    if result_date.strip():
        return True
    pending = ("접수", "심사중", "진행")
    return not any(p in status for p in pending)


def run_invstg(session, notifier: Notifier):
    rows = fetch_invstg_rows(session)
    if not rows:
        print("[invstg] 수집된 행이 없습니다. (사이트 응답 확인 필요)", file=sys.stderr)
        return
    print(f"[invstg] 총 {len(rows)}건 수집")

    state = load_json(SEEN_INVSTG, {})
    bootstrap = len(state) == 0

    new_apps = []
    new_results = []

    for row in rows:
        if not passes_invstg_filter(row):
            continue
        key = row["bizno"]
        prev = state.get(key)
        cur = {
            "name": row["name"],
            "market": row["market"],
            "type": row["type"],
            "apply_date": row["apply_date"],
            "result_date": row["result_date"],
            "status": row["status"],
        }
        if prev is None:
            new_apps.append(row)
        else:
            # 상태 또는 결과확정일 변화 -> 결과 업데이트
            changed = (prev.get("status") != cur["status"]
                       or prev.get("result_date") != cur["result_date"])
            if changed and is_final_result(cur["status"], cur["result_date"]):
                new_results.append(row)
        state[key] = cur

    save_json(SEEN_INVSTG, state)

    if bootstrap:
        notifier.send(
            "🟢 <b>IPO 예비심사 모니터링을 시작합니다.</b>\n"
            f"현재 진행 목록 {len(state)}건을 기준선으로 등록했습니다. "
            "이후 새 청구·심사결과가 생기면 알려드릴게요."
        )
        print(f"[invstg] bootstrap: {len(state)}건 등록, 개별 알림 생략")
        return

    for row in new_apps:
        notifier.send(fmt_new_app(row))
    for row in new_results:
        notifier.send(fmt_result(row))

    print(f"[invstg] 신규 청구 {len(new_apps)}건, 결과확정 {len(new_results)}건 알림")


def fmt_new_app(row) -> str:
    link = DETAIL_URL.format(row["bizno"])
    return (
        "🆕 <b>상장예비심사 청구</b>\n"
        f"• 회사: <b>{row['name']}</b> ({row['market']})\n"
        f"• 유형: {row['type']}\n"
        f"• 청구일: {row['apply_date']}\n"
        f"• 주선인: {row['underwriter']}\n"
        f'<a href="{link}">상세보기</a>'
    )


def fmt_result(row) -> str:
    link = DETAIL_URL.format(row["bizno"])
    icon = result_icon(row["status"])
    return (
        f"{icon} <b>심사결과 확정: {row['status']}</b>\n"
        f"• 회사: <b>{row['name']}</b> ({row['market']})\n"
        f"• 유형: {row['type']}\n"
        f"• 청구일: {row['apply_date']} → 확정일: {row['result_date'] or '-'}\n"
        f"• 주선인: {row['underwriter']}\n"
        f'<a href="{link}">상세보기</a>'
    )


# --------------------------------------------------------------------------
# Task 2 : 신규상장 전 영업일 알림
# --------------------------------------------------------------------------
def kr_holiday_set(years):
    if _holidays is None:
        return set()
    try:
        return set(_holidays.country_holidays("KR", years=list(years)).keys())
    except Exception:
        return set()


def is_business_day(d: dt.date, hol: set) -> bool:
    if d.weekday() >= 5:          # 토(5)/일(6)
        return False
    if d in hol:
        return False
    if (d.month, d.day) == (12, 31):  # KRX 연말 휴장
        return False
    return True


def next_business_day(d: dt.date, hol: set) -> dt.date:
    nd = d + dt.timedelta(days=1)
    while not is_business_day(nd, hol):
        nd += dt.timedelta(days=1)
    return nd


DATE_RE = re.compile(r"(20\d\d)-(\d\d)-(\d\d)")


def fetch_offering_rows(session: requests.Session):
    """공모기업 진행현황(상장예정일 포함) 최신순 수집."""
    body = {
        "method": "searchPubofrProgComSub",
        "currentPageSize": "100",
        "pageIndex": "1",
        "orderMode": "1",    # 신고서제출일 기준
        "orderStat": "D",    # 내림차순(최신순)
    }
    html = post_html(session, OFFERING_URL, body)
    return parse_offering_html(html)


def parse_offering_html(html: str):
    """행별로 (name, market, listing_date, underwriter) 추출.
    컬럼: 0회사명 1신고서제출일 2수요예측 3청약 4납입일 5확정공모가 6공모금액 7상장예정일 8주선인"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("tbody tr"):
        if "fnDetailView" not in str(tr):
            continue
        tds = tr.find_all("td")
        if len(tds) < 9:
            continue
        name_td = tds[0]
        name = (name_td.get("title") or name_td.get_text(strip=True)).strip()
        img = name_td.find("img")
        market = (img.get("alt", "").strip() if img else "")
        m = DATE_RE.search(tds[7].get_text(" ", strip=True))
        listing_date = None
        if m:
            try:
                listing_date = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                listing_date = None
        out.append({
            "name": name,
            "market": market,
            "listing_date": listing_date,
            "underwriter": tds[8].get_text(strip=True),
        })
    return out


def run_listing(session, notifier: Notifier, force=False):
    now = now_kst()
    today = now.date()
    hol = kr_holiday_set(range(today.year - 1, today.year + 2))

    # 오늘이 영업일이 아니면 (주말/공휴일) 상장 전일 알림을 보내지 않음
    if not is_business_day(today, hol) and not force:
        print(f"[listing] {today} 는 영업일이 아니어서 건너뜀")
        return

    # 발송 시간대(아침) 아니면 건너뜀 (중복 실행 방지는 notified 상태로 처리)
    if not force and not (LISTING_HOUR_START <= now.hour < LISTING_HOUR_END):
        print(f"[listing] 발송 시간대(KST {LISTING_HOUR_START}-{LISTING_HOUR_END}시) 아님, 건너뜀")
        return

    target = next_business_day(today, hol)
    print(f"[listing] 오늘 {today} → 다음 영업일(상장 예정일) {target}")

    rows = fetch_offering_rows(session)
    hits = [e for e in rows
            if e["listing_date"] == target
            and e["market"] in ALLOWED_MARKETS
            and "스팩" not in e["name"]]   # 스팩 IPO 상장 제외

    notified = load_json(NOTIFIED_LISTING, {})

    sent = 0
    for e in hits:
        key = f"{target.isoformat()}|{e['name']}"
        if key in notified:
            continue
        notifier.send(fmt_listing(e, target))
        notified[key] = now.isoformat()
        sent += 1

    # 오래된 알림 기록 정리(90일)
    cutoff = (today - dt.timedelta(days=90)).isoformat()
    notified = {k: v for k, v in notified.items()
                if k.split("|", 1)[0] >= cutoff}
    save_json(NOTIFIED_LISTING, notified)

    print(f"[listing] 대상 {len(hits)}건 중 신규 알림 {sent}건")


def fmt_listing(e, target: dt.date) -> str:
    weekday_ko = "월화수목금토일"[target.weekday()]
    return (
        "🔔 <b>신규상장 예정 (내일)</b>\n"
        f"• 회사: <b>{e['name']}</b> ({e['market']})\n"
        f"• 상장예정일: {target.isoformat()} ({weekday_ko})\n"
        f"• 주선인: {e['underwriter']}\n"
        "※ 상장 전 영업일 알림"
    )


# --------------------------------------------------------------------------
# chat_id 자동 탐색
# --------------------------------------------------------------------------
def cmd_chatid():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    print("=" * 50)
    if not token:
        print("[!] TELEGRAM_BOT_TOKEN secret 이 설정되지 않았습니다.")
        print("    먼저 저장소 Settings > Secrets 에 봇 토큰을 등록하세요.")
        print("=" * 50)
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
        data = r.json()
    except Exception as e:
        print(f"[!] 텔레그램 호출 실패: {e}")
        print("=" * 50)
        return

    if not data.get("ok"):
        print(f"[!] 토큰이 잘못된 것 같습니다. 텔레그램 응답: {data}")
        print("=" * 50)
        return

    chats = {}
    for upd in data.get("result", []):
        msg = (upd.get("message") or upd.get("edited_message")
               or upd.get("channel_post") or {})
        chat = msg.get("chat")
        if chat:
            label = (chat.get("title")
                     or " ".join(x for x in [chat.get("first_name"),
                                             chat.get("last_name")] if x)
                     or chat.get("username") or "")
            chats[chat["id"]] = label

    if not chats:
        print("[!] 아직 메시지 기록이 없습니다.")
        print("    → 텔레그램에서 '내가 만든 봇' 대화창을 열고 START(또는 아무 메시지)를")
        print("      보낸 뒤, 이 워크플로를 다시 Run 하세요.")
        print("=" * 50)
        return

    print("✅ chat_id 를 찾았습니다! 아래 숫자를 복사하세요:")
    print("")
    for cid, name in chats.items():
        print(f"   ★  TELEGRAM_CHAT_ID = {cid}    ({name})")
    print("")
    print("이 숫자를 저장소 Settings > Secrets 에 TELEGRAM_CHAT_ID 로 등록하세요.")
    print("=" * 50)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["invstg", "listing", "both", "test", "chatid"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-listing", action="store_true",
                    help="시간대/영업일 체크 무시하고 상장 알림 실행")
    args = ap.parse_args()

    if args.task == "chatid":
        cmd_chatid()
        return

    notifier = Notifier(dry_run=args.dry_run)

    if args.task == "test":
        ok = notifier.send("✅ IPO 알림 봇 연결 테스트 성공! 이 메시지가 보이면 설정 완료입니다.")
        print("test 전송:", "성공" if ok else "실패")
        return

    session = make_session()

    if args.task in ("invstg", "both"):
        try:
            run_invstg(session, notifier)
        except Exception as e:
            print(f"[invstg] 오류: {e}", file=sys.stderr)
            raise

    if args.task in ("listing", "both"):
        try:
            run_listing(session, notifier, force=args.force_listing)
        except Exception as e:
            print(f"[listing] 오류: {e}", file=sys.stderr)
            raise


if __name__ == "__main__":
    main()
