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

try:
    # 크롬 TLS 지문 위장 (KIND 상세페이지 봇차단 우회용)
    from curl_cffi import requests as cffi_requests
except Exception:  # pragma: no cover
    cffi_requests = None

# --------------------------------------------------------------------------
# 설정
# --------------------------------------------------------------------------
BASE = "https://kind.krx.co.kr"
INVSTG_URL = BASE + "/listinvstg/listinvstgcom.do"
OFFERING_URL = BASE + "/listinvstg/pubofrprogcom.do"   # 공모기업 진행현황 (상장예정일 포함)
OFFERING_DETAIL_URL = BASE + "/listinvstg/pubofrprogcomdetail.do?method=searchProgComDetailMain&bzProcsNo={}"
DETAIL_URL = BASE + "/listinvstg/listinvstgcom.do?method=searchListInvstgCorpDetail&bizProcNo={}"

STATE_DIR = Path(__file__).resolve().parent / "state"
SEEN_INVSTG = STATE_DIR / "seen_invstg.json"
NOTIFIED_LISTING = STATE_DIR / "notified_listing.json"
NOTIFIED_DART = STATE_DIR / "notified_dart.json"

# DART 전자공시 (증권신고서 감시)
DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_VIEW_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"

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
        bm = re.search(r"fnDetailView\('(\d+)'\)", str(tr))
        out.append({
            "name": name,
            "market": market,
            "listing_date": listing_date,
            "offer_price": tds[5].get_text(strip=True),   # 확정공모가
            "underwriter": tds[8].get_text(strip=True),
            "bizno": bm.group(1) if bm else "",
        })
    return out


def _num(s: str) -> int:
    """'28,400' -> 28400. 숫자 없으면 0."""
    return int(re.sub(r"[^\d]", "", s or "") or 0)


def fetch_total_shares(bizno: str):
    """공모진행 상세(pubofrprogcomdetail)에서 상장주식수 추출.
    이 페이지는 KIND 봇차단(TLS지문) 대상이라 curl_cffi(크롬 위장)로 접근."""
    if not bizno or cffi_requests is None:
        return None
    url = OFFERING_DETAIL_URL.format(bizno)
    for imp in ("chrome131", "chrome124", "chrome"):
        try:
            r = cffi_requests.get(url, impersonate=imp,
                                  headers={"Referer": OFFERING_URL,
                                           "Accept-Language": "ko-KR,ko;q=0.9"},
                                  timeout=30)
            html = r.text
            if "상장주식수" in html:
                m = re.search(r"상장주식수[\s\S]{0,300}?([\d,]{4,})", html)
                if m:
                    return _num(m.group(1))
        except Exception as e:
            print(f"[listing] 상세 조회 실패({imp}): {e}", file=sys.stderr)
    return None


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
        # 상장 시가총액 = 확정공모가 x 상장주식수 (억원)
        shares = fetch_total_shares(e.get("bizno"))
        price = _num(e.get("offer_price"))
        e["mcap_eok"] = round(price * shares / 1e8) if (shares and price) else None
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
    price = (e.get("offer_price") or "").strip()
    price_str = f"{price}원" if price else "-"
    mcap = e.get("mcap_eok")
    mcap_str = f"약 {mcap:,}억원" if mcap else "확인 필요"
    return (
        "🔔 <b>신규상장 예정 (내일)</b>\n"
        f"• 회사: <b>{e['name']}</b> ({e['market']})\n"
        f"• 상장예정일: {target.isoformat()} ({weekday_ko})\n"
        f"• 확정공모가: {price_str}\n"
        f"• 상장 시가총액: {mcap_str}\n"
        f"• 주선인: {e['underwriter']}\n"
        "※ 상장 전 영업일 알림"
    )


# --------------------------------------------------------------------------
# Task 3 : DART 증권신고서 (심사승인 기업 대상, 상장 후 제외, 정정 포함)
# --------------------------------------------------------------------------
def _norm_name(s: str) -> str:
    s = s or ""
    for x in ("주식회사", "(주)", "㈜", " ", "\t"):
        s = s.replace(x, "")
    return s.strip()


def approved_names(state: dict) -> set:
    """심사승인 기업명 집합 (미승인/철회 제외)."""
    out = set()
    for v in state.values():
        st = v.get("status", "") or ""
        if "승인" in st and "미승인" not in st:
            out.add(_norm_name(v.get("name", "")))
    out.discard("")
    return out


def listed_names(offering_rows, today: dt.date) -> set:
    """상장예정일이 이미 지난(=상장한) 기업명 집합 → DART 알림에서 제외."""
    out = set()
    for r in offering_rows:
        d = r.get("listing_date")
        if d and d < today:
            out.add(_norm_name(r.get("name", "")))
    out.discard("")
    return out


def fetch_dart_filings(key: str, days: int = 7):
    """최근 발행공시(pblntf_ty=C) 목록 조회 (페이지네이션)."""
    today = now_kst().date()
    bgn = (today - dt.timedelta(days=days)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    out = []
    page = 1
    while page <= 5:
        params = {"crtfc_key": key, "bgn_de": bgn, "end_de": end,
                  "pblntf_ty": "C", "page_no": str(page), "page_count": "100"}
        try:
            r = requests.get(DART_LIST_URL, params=params, timeout=20)
            data = r.json()
        except Exception as e:
            print(f"[dart] 조회 실패: {e}", file=sys.stderr)
            break
        status = data.get("status")
        if status != "000":
            # 013=데이터없음. 그 외는 키/파라미터 오류 등
            if status != "013":
                print(f"[dart] 응답 {status}: {data.get('message')}", file=sys.stderr)
            break
        out.extend(data.get("list", []))
        if page >= int(data.get("total_page", 1)):
            break
        page += 1
        time.sleep(0.3)
    return out


def fmt_dart(f: dict) -> str:
    link = DART_VIEW_URL.format(f.get("rcept_no", ""))
    market = {"Y": "유가증권", "K": "코스닥", "N": "코넥스", "E": "기타"}.get(f.get("corp_cls", ""), "")
    rdt = f.get("rcept_dt", "")
    rdt_fmt = f"{rdt[:4]}-{rdt[4:6]}-{rdt[6:]}" if len(rdt) == 8 else rdt
    mk = f" ({market})" if market else ""
    return (
        "📄 <b>증권신고서 제출 (DART)</b>\n"
        f"• 회사: <b>{f.get('corp_name', '')}</b>{mk}\n"
        f"• 문서: {f.get('report_nm', '')}\n"
        f"• 제출일: {rdt_fmt}\n"
        f'<a href="{link}">📎 증권신고서 원문 보기</a>'
    )


def is_ipo_regstmt(f: dict, approved: set, listed: set) -> bool:
    """IPO(신규상장) 증권신고서인가? 유상증자·채무증권·상장사 필터링.
    - 지분증권 증권신고서만 (채무증권 제외)
    - 심사승인 기업만
    - 종목코드 있으면 상장사 → 제외 (유상증자)
    - 공모진행상 이미 상장예정일 지난 곳 → 제외"""
    rn = f.get("report_nm", "") or ""
    if "증권신고서" not in rn or "지분증권" not in rn:
        return False
    nm = _norm_name(f.get("corp_name", ""))
    if nm not in approved:
        return False
    if (f.get("stock_code") or "").strip():   # 상장사(종목코드 보유) → 제외
        return False
    if nm in listed:
        return False
    return True


def run_dart(session, notifier: Notifier):
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        print("[dart] DART_API_KEY 미설정 → 건너뜀", file=sys.stderr)
        return

    approved = approved_names(load_json(SEEN_INVSTG, {}))
    if not approved:
        print("[dart] 심사승인 기업 없음 → 건너뜀")
        return

    today = now_kst().date()
    try:
        offering = fetch_offering_rows(session)
    except Exception:
        offering = []
    listed = listed_names(offering, today)

    filings = fetch_dart_filings(key)
    notified = load_json(NOTIFIED_DART, {})
    bootstrap = len(notified) == 0

    new = []
    for f in filings:
        if not is_ipo_regstmt(f, approved, listed):
            continue
        rno = f.get("rcept_no")
        if not rno or rno in notified:
            continue
        notified[rno] = now_kst().isoformat()
        new.append(f)

    # 오래된 기록 정리(180일)
    cutoff = (today - dt.timedelta(days=180)).strftime("%Y%m%d")
    notified = {k: v for k, v in notified.items() if k[:8] >= cutoff}
    save_json(NOTIFIED_DART, notified)

    if bootstrap:
        print(f"[dart] bootstrap: {len(new)}건 기준선 등록, 개별 알림 생략")
        return

    for f in new:
        notifier.send(fmt_dart(f))
    print(f"[dart] 신규 증권신고서 {len(new)}건 알림 (승인 {len(approved)}곳, 상장제외 {len(listed)}곳)")


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
    # 0) 이 토큰이 어떤 봇 것인지 먼저 확인 (getMe)
    try:
        me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=20).json()
    except Exception as e:
        print(f"[!] 텔레그램 호출 실패: {e}")
        print("=" * 50)
        return
    if not me.get("ok"):
        print(f"[!] 토큰이 잘못되었습니다 (getMe 실패). 응답: {me}")
        print("    BotFather > /mybots > 봇선택 > API Token 에서 다시 복사하세요.")
        print("=" * 50)
        return
    bot_username = me["result"].get("username", "")
    print(f"이 토큰의 봇: @{bot_username}")
    print(f"  → 반드시 텔레그램에서 '@{bot_username}' 이 봇에게 메시지를 보내야 합니다!")
    print("-" * 50)

    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
        data = r.json()
    except Exception as e:
        print(f"[!] 텔레그램 호출 실패: {e}")
        print("=" * 50)
        return

    if not data.get("ok"):
        print(f"[!] getUpdates 실패. 텔레그램 응답: {data}")
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
        print(f"[!] @{bot_username} 봇에게 온 메시지가 없습니다.")
        print(f"    → 텔레그램 검색창에 '@{bot_username}' 을 정확히 입력해 그 봇을 열고,")
        print("      START(또는 아무 메시지)를 보낸 뒤 이 워크플로를 다시 Run 하세요.")
        print(f"    ※ 다른 봇 말고 반드시 @{bot_username} 에게 보내야 합니다.")
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
    ap.add_argument("task", choices=["invstg", "listing", "dart", "both",
                                     "test", "chatid", "testmcap", "testdart"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-listing", action="store_true",
                    help="시간대/영업일 체크 무시하고 상장 알림 실행")
    args = ap.parse_args()

    if args.task == "chatid":
        cmd_chatid()
        return

    if args.task == "testmcap":
        # curl_cffi로 KIND 상세(상장주식수) 접근 가능한지 검증
        print(f"curl_cffi 설치됨: {cffi_requests is not None}")
        s = make_session()
        rows = [r for r in fetch_offering_rows(s) if r.get("listing_date")]
        print(f"공모진행 {len(rows)}건 수집, 상위 5건 상세 조회 테스트:")
        for r in rows[:5]:
            shares = fetch_total_shares(r.get("bizno"))
            price = _num(r.get("offer_price"))
            mcap = round(price * shares / 1e8) if (shares and price) else None
            print(f"  - {r['name']}({r['market']}) 공모가={r.get('offer_price') or '-'} "
                  f"상장주식수={shares if shares else '실패'} "
                  f"시총={f'{mcap:,}억' if mcap else '-'} 상장일={r['listing_date']}")
        return

    if args.task == "testdart":
        key = os.environ.get("DART_API_KEY", "").strip()
        print(f"DART_API_KEY 설정됨: {bool(key)}")
        s = make_session()
        approved = approved_names(load_json(SEEN_INVSTG, {}))
        print(f"심사승인 기업: {len(approved)}곳")
        today = now_kst().date()
        listed = listed_names(fetch_offering_rows(s), today)
        print(f"이미 상장(제외): {len(listed)}곳")
        filings = fetch_dart_filings(key)
        print(f"최근 발행공시(7일): {len(filings)}건 조회")
        print("--- 승인기업의 증권신고서(진단: stock_code 표시) ---")
        for f in filings:
            rn = f.get("report_nm", "") or ""
            if "증권신고서" not in rn:
                continue
            nm = _norm_name(f.get("corp_name", ""))
            if nm not in approved:
                continue
            keep = is_ipo_regstmt(f, approved, listed)
            print(f"  [{'알림✓' if keep else '제외 '}] {f.get('corp_name')} "
                  f"code={f.get('stock_code') or '없음'} cls={f.get('corp_cls')} | {rn} | {f.get('rcept_dt')}")
        cnt = sum(1 for f in filings if is_ipo_regstmt(f, approved, listed))
        print(f"→ 최종 알림 대상(IPO 지분증권·미상장) {cnt}건")
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

    if args.task in ("dart", "both"):
        try:
            run_dart(session, notifier)
        except Exception as e:
            print(f"[dart] 오류: {e}", file=sys.stderr)
            raise


if __name__ == "__main__":
    main()
