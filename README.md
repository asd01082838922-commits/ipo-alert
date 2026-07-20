# IPO 알림 봇 (KRX KIND → 텔레그램)

한국거래소 KIND 공시 데이터를 주기적으로 확인해서 **텔레그램**으로 알림을 보냅니다.

## 무엇을 알려주나요?

**① 상장예비심사 신청·결과** (`invstg`)
- 새 **상장예비심사 청구(신청)** 건이 뜨면 알림
- **심사결과 확정**(심사승인 / 심사철회 / 심사미승인 등)이 뜨면 알림
- **스팩 IPO(스팩 자체의 신규상장)는 제외**, **스팩합병(스팩 소멸/존속합병)은 포함**
- 대상 시장: 코스닥 · 유가증권(코스피)

**② 신규상장 전 영업일 알림** (`listing`)
- 코스닥/코스피에 **다음 영업일 상장 예정**인 종목을, 그 **전 영업일 아침(KST 08시경)** 에 알림
- 스팩 IPO 상장은 제외

> 데이터 출처: <https://kind.krx.co.kr> (공개 공시 데이터). 개인적/참고용으로만 사용하세요.

---

## 설치 & 실행 (GitHub Actions, 무료·PC 안 켜도 됨)

### 1단계. 텔레그램 봇 만들기 → 토큰 받기
1. 텔레그램에서 **@BotFather** 를 검색해 대화 시작
2. `/newbot` 입력 → 봇 이름, 사용자명(끝이 `bot`) 지정
3. BotFather가 주는 **토큰**을 복사 (예: `123456789:AAab...`) → 이게 `TELEGRAM_BOT_TOKEN`

### 2단계. 내 chat_id 알아내기
1. 방금 만든 봇과의 대화창을 열고 아무 메시지(`안녕` 등)를 보냄
2. 브라우저에서 아래 주소 열기 (토큰 부분 교체):
   `https://api.telegram.org/bot<토큰>/getUpdates`
3. 결과 JSON에서 `"chat":{"id":123456789 ...` 의 숫자가 **`TELEGRAM_CHAT_ID`**
   - (그룹으로 받고 싶으면 봇을 그룹에 초대 후 같은 방법으로 그룹 chat_id 확인. 그룹 id는 보통 `-100...`)

### 3단계. GitHub 저장소 만들기
1. GitHub에서 새 저장소 생성 (이름 예: `ipo-alert`)
   - **Public** 권장 → Actions 실행 시간 **무제한 무료** (Private은 월 2000분 한도)
   - 공개되는 건 이 코드와 상태파일(회사명 등, 어차피 공개 데이터)뿐입니다. **토큰은 코드에 없고 Secrets에 암호화 저장됩니다.**
2. 이 폴더(`ipo-alert`)를 그 저장소에 push (아래 "코드 올리기" 참고)

### 4단계. Secrets 등록 (토큰은 여기에만!)
저장소 → **Settings → Secrets and variables → Actions → New repository secret** 에서 2개 등록:
- `TELEGRAM_BOT_TOKEN` = 1단계 토큰
- `TELEGRAM_CHAT_ID` = 2단계 숫자

### 5단계. 동작 확인
1. 저장소 → **Actions** 탭 → (Actions 사용 허용 클릭)
2. 왼쪽 **IPO Alert** 워크플로 → **Run workflow** (수동 실행)
3. 첫 실행: 현재 진행중인 예비심사 목록을 "기준선"으로 저장하고 **시작 메시지 1건**만 옵니다.
   (기존 목록을 전부 알림으로 쏟아내지 않도록 설계됨)
4. 이후 30분마다 자동 실행되며, **새 청구/심사결과**가 생기면 그때 알림이 옵니다.
5. 신규상장 전일 알림은 **평일 아침 08시대** 실행에서 다음 영업일 상장 종목이 있으면 발송됩니다.

---

## 코드 올리기 (git push)

이 폴더에서 (GitHub 저장소 URL은 본인 것으로 교체):

```bash
git init
git add .
git commit -m "IPO alert bot"
git branch -M main
git remote add origin https://github.com/<사용자명>/ipo-alert.git
git push -u origin main
```

---

## 동작 방식 / 설정 바꾸기

- **실행 주기**: `.github/workflows/ipo-alert.yml` 의 `cron: "*/30 * * * *"` (30분마다).
  더 자주/드물게 하려면 이 값을 수정. (GitHub 무료 최소 주기는 5분, 부하에 따라 지연될 수 있음)
- **신규상장 알림 발송 시각**: `ipo_alert.py` 의 `LISTING_HOUR_START/END` (기본 KST 08~09시).
- **상태 저장**: `state/seen_invstg.json`(예비심사 추적), `state/notified_listing.json`(상장 알림 중복방지).
  매 실행 후 GitHub Actions가 자동으로 커밋합니다.
- **영업일/공휴일**: `holidays` 라이브러리(한국)로 판단 + 12/31 휴장 처리.

### 로컬에서 테스트하려면 (선택, Python 필요)
```bash
pip install -r requirements.txt
set TELEGRAM_BOT_TOKEN=... &  set TELEGRAM_CHAT_ID=...      # Windows CMD
python ipo_alert.py test                 # 연결 테스트 메시지
python ipo_alert.py invstg --dry-run     # 전송 없이 콘솔 출력
python ipo_alert.py listing --force-listing --dry-run
```

## 주의
- GitHub은 **60일간 저장소 활동이 없으면 예약 워크플로를 자동 비활성화**합니다.
  본 봇은 상태 파일을 자동 커밋하므로 대개 유지되지만, 오래 알림이 없으면 가끔 Actions 탭에서 한 번 실행해 주세요.
- 사이트 구조가 바뀌면 파싱이 실패할 수 있습니다. Actions 로그에 "수집된 행이 없습니다" 가 뜨면 알려주세요.
