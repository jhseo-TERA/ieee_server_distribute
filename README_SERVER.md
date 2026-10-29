# IEEE Paper Repository — 운영 가이드

수집한 IEEE/Optica 논문 메타데이터(Excel)를 MySQL에 적재하고, Flask로
검색·필터·PDF 뷰어가 가능한 웹사이트를 제공합니다.

## 구성

```
IEEE_Paper_Server/
├── paper_server/                 # 서비스 코드: ai, recommendation, serdes, parsers, accounts
├── docs/                         # operations, development, history 문서
├── ieee-pdf/                     # IEEE PDF 저장 (숫자키 <article_number>.pdf)
├── optica-pdf/                   # Optica PDF 저장 (uri키 <article_number>.pdf)
├── py_01_data/00_metadata/       # 원본 Excel (수집 결과)
├── web/
│   ├── app.py                    # Flask 서버 (SQLAlchemy + PyMySQL)
│   └── templates/index.html      # UI (다크 레드 테마, 자체 완결형)
├── scripts/
│   ├── schema.sql                # MySQL 스키마
│   ├── import_excel_to_db.py     # Excel → MySQL 적재
│   ├── download_pdfs.py          # 즐겨찾기 PDF 다운로드 (연세대 프록시)
│   ├── download_pdfs_backup.py   # 비활성화된 구형 호환 진입점
│   ├── export_zotero_ris.py      # 즐겨찾기 → Zotero RIS 내보내기
│   ├── sweep_ieee_journals.py    # 레거시 브라우저 진단용 (실패 증거 저장)
│   ├── sweep_ieee_conferences.py # 레거시 브라우저 진단용 (실패 증거 저장)
│   ├── run_metadata_update.py   # 설정 기반 Crossref 수집·검증·이번 실행만 적재
│   ├── update_ieee_weekly.bat    # IEEE Crossref 수집 + DB 적재 (스케줄 등록됨)
│   ├── fetch_optica_crossref.py  # Optica 저널 메타데이터 수집 (Crossref API, 캡차 없음)
│   ├── fetch_ojssc_crossref.py   # OJSSC 메타데이터 수집 (Crossref API, VPN 없음)
│   ├── fetch_ieee_crossref.py    # IEEE 저널·학회 메타데이터 수집 (Crossref API)
│   ├── update_optica_weekly.bat  # 위 수집 + DB 적재 묶음 배치 (스케줄 등록됨)
│   ├── fetch_nature_crossref.py  # Nature 3개 저널 메타데이터 수집 (Crossref API)
│   ├── update_nature_weekly.bat  # Nature 수집 + DB 적재 묶음 배치 (스케줄 등록됨)
│   ├── start_mysql.bat           # MySQL(포터블) 시작 — 재부팅 후 실행
│   ├── run_server.bat            # MySQL 확인 + Flask 실행 (더블클릭)
│   └── install_server_startup_task.ps1 # 로그인 후 서버 자동 시작 등록(공개는 선택)
├── logs/                         # IEEE / Optica / Nature 주간 업데이트 실행 로그
└── .env                          # DB 접속정보 등
```

## 환경 (이 PC에 설치된 상태)

- **MySQL 8.4.9** — 포터블 설치 (Windows 서비스 아님, 관리자 권한 불필요)
  - 위치: `%USERPROFILE%\mysql\mysql-8.4.9-winx64`
  - 데이터: `%USERPROFILE%\mysql\data`, 설정: `%USERPROFILE%\mysql\my.ini`
  - 앱 접속정보는 `.env`, 로컬 root 접속정보는 `%USERPROFILE%\mysql\admin.cnf`에만 저장
  - ⚠️ 서비스가 아니므로 **재부팅하면 꺼집니다** → `scripts\start_mysql.bat` 실행
- Python venv: `.venv` (Flask, SQLAlchemy, PyMySQL, pandas, openpyxl 설치됨)

## 실행 순서

### 처음 한 번만
```bat
REM 1) DB 시작
scripts\start_mysql.bat

REM 2) 스키마 생성
"%USERPROFILE%\mysql\mysql-8.4.9-winx64\bin\mysql.exe" ^
  --defaults-extra-file="%USERPROFILE%\mysql\admin.cnf" ^
  --execute="source scripts/schema.sql"

REM 3) Excel → MySQL 적재
.venv\Scripts\python.exe scripts\import_excel_to_db.py
```

### 매번 (웹사이트 켜기)
```bat
scripts\run_server.bat
```
→ 브라우저에서 http://127.0.0.1:5001 접속

### 재부팅 후 자동 시작

아래 명령을 한 번 실행하면 현재 Windows 사용자가 로그인한 뒤 15초 후
`MySQL → Waitress(127.0.0.1:5001)` 순서로 자동 시작합니다. 기본값은 이 PC에서만
접속 가능한 로컬 모드이며 관리자 권한은 필요하지 않습니다. 네트워크 준비가 늦어
실패하면 1분 간격으로 최대 3회 다시 시도합니다. 로컬 모드로 실행할 때 이 프로젝트가
이전에 띄운 Cloudflare 터널이 남아 있으면 안전하게 종료합니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_server_startup_task.ps1
```

등록과 동시에 시험 실행하려면 `-StartNow`를 붙입니다. 외부에서도 접속할 수 있도록
Cloudflare Quick Tunnel까지 자동 시작하려면 보안상 명시적으로 `-Public`을 붙여
재등록해야 합니다. 공개 주소는 인증 화면으로 보호되지만 인터넷에 노출되며, 서버를
새로 시작할 때마다 주소가 바뀔 수 있습니다.

```powershell
# 외부 공개까지 자동 시작하도록 명시적으로 등록
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_server_startup_task.ps1 -Public
```

자동 시작 기록은 `logs\server-startup.log`, 웹서버 오류는
`logs\paper-server.err.log`, 현재 공개 주소는 `.runtime\public-url.txt`에서 확인할 수
있습니다.

```powershell
# 상태 확인
Get-ScheduledTask -TaskName "IEEE_Paper_Server_AutoStart"
Get-ScheduledTaskInfo -TaskName "IEEE_Paper_Server_AutoStart"

# 자동 시작 해제
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_server_startup_task.ps1 -Uninstall
```

## 계정 로그인 현황 (`/account/activity`)

`admin`로 로그인하면 메인 메뉴의 **계정 로그인 현황**에서 계정별 최근 로그인,
최근 활동, 로그인 유지 세션 수와 최근 40개 세션 기록을 확인할 수 있습니다.
페이지와 API는 `admin`만 조회할 수 있습니다.

- 활동 중은 최근 5분 내 요청 기준입니다. 열린 화면이 보이는 동안 1분마다 활동 신호를 보냅니다.
- 로그인 유지 세션은 로그아웃·만료·계정 비활성화·비밀번호 변경 여부를 반영합니다.
  브라우저 종료만으로 세션 종료를 즉시 알 수는 없습니다.
- 기능 적용 이전의 로그인 시각은 추정하지 않습니다. 기존 쿠키는 첫 요청 시
  **기존 세션 확인**으로 기록하며, 다음 정상 로그인부터 로그인 시각을 기록합니다.
- 기록은 계정·시각·세션 상태이며, 조회 API에 쿠키·CSRF 토큰·비밀번호 정보는 포함하지 않습니다.
- 화면은 30초마다 갱신하며 시각은 한국 시간으로 표시합니다.

기존 DB에는 `scripts/migrations/016_account_activity.sql`을 적용한 뒤 서버를 재시작합니다.

```powershell
.venv\Scripts\python.exe scripts\migrate_account_activity.py --apply
```

`ACCOUNT_ACTIVITY_ENABLED=0`이면 기록 기능을 끕니다.

## DesignCon 즐겨찾기 선별

메인 논문 목록의 **DesignCon 전체** 탭에서 PDF를 열어 보고, 관심 논문의 ☆를 눌러
즐겨찾기에 추가합니다. **★ DesignCon 즐겨찾기** 탭에는 내가 고른 DesignCon만
표시되며, 탭의 건수도 현재 로그인 계정의 즐겨찾기를 따릅니다. 해당 논문은 매일
2편 개인 리뷰의 랜덤 후보에 들어갑니다.

- 전체 목록 바로가기: `/?collection=designcon`
- 선별한 목록 바로가기: `/?collection=designcon-favorites`
- 탭을 바꾸면 검색 조건을 초기화하고, 선택한 탭 안에서 제목·연도 등의 필터를 적용할 수 있습니다.

## 매일 2편 개인 리뷰 (`/daily-review`)

메인 화면의 **오늘의 2편 · 개인 리뷰**에서 내 즐겨찾기 중 완료하지 않은 논문을
하루 최대 2편 랜덤으로 추천받습니다. 한국 시간(Asia/Seoul) 날짜별로 선택을 DB에
저장하므로 새로고침하거나 서버를 다시 켜도 같은 날에는 같은 논문이 표시됩니다.
미완료 논문은 이전 추천 여부와 관계없이 계속 랜덤 후보에 남습니다.

- **Obsidian에서 리뷰하기**를 누르면 논문별 리뷰 노트를 만들거나 기존 노트를 엽니다.
  그림 붙여넣기, LaTeX 수식, 텍스트 리뷰는 Obsidian에서 편집합니다.
- **리뷰 완료 · 다음 추천에서 제외**를 체크하고 **완료 상태 저장**을 누르면 다음 날짜의
  추천에서 빠집니다. 체크를 해제해 저장하면 다시 후보가 됩니다.
- 지난 추천 기록에서도 같은 논문의 Obsidian 노트를 열 수 있습니다.
- 노트 생성·열기와 완료 체크 저장은 **`admin` 계정만** 허용하며 API에서도 확인합니다.
  기존 추천의 `검토 완료` 피드백과는 별개입니다.
- 후보가 0~1편이면 해당 수만 보여줍니다. 당일 선택 후 추가한 즐겨찾기는 다음 날부터
  반영됩니다. 화면을 열 때 추천을 생성하며 별도 예약 작업이나 알림은 필요하지 않습니다.

기존 DB에는 최초 한 번 추가 스키마를 적용하고 서버를 재시작합니다.

```powershell
.venv\Scripts\python.exe scripts\migrate_daily_review.py --apply
```

원본 SQL은 `scripts/migrations/015_daily_review.sql`입니다. 완료 상태는 버전 충돌을 검사하며,
기존 DB에 저장된 웹 메모도 그대로 보존합니다. 상태만 저장하는 요청은 메모를 변경하지 않습니다.

### Obsidian에서 개인 리뷰 편집

`.env`의 `OBSIDIAN_VAULT_PATH`를 기존 `LLM_Wiki` 보관함의 절대 경로로 설정합니다.
웹에서 **Obsidian에서 리뷰하기**를 처음 누를 때만 `wiki/sources/<학회> <연도> - <짧은 논문 제목>.md`를
생성하고 `obsidian://open`으로 엽니다. 파일명과 본문 제목에는 학회·연도와 축약한 논문명을 사용하고,
정식 논문명은 `title` 속성에 보존합니다. 논문 ID와 파일의 연결은 상태 파일로 관리하며
동명 논문은 구분합니다. 기존 숫자 기반 이름도 계속 열 수 있습니다. 같은 PC에 Obsidian이 설치되어 있어야 합니다.
브라우저가 앱 열기를 확인하면 Obsidian을 선택합니다.

- 상단 속성은 `title`·`venue`·`year`·`doi`만 넣고 빈 값은 생략합니다.
  본문은 **핵심 내용 / 그림 수식 / 총평 / 외부 편찬** 네 구역으로 시작합니다.
  외부 편찬에는 별도 확장 노트의 내부 링크를 모읍니다. 64MB 이내의 PDF는
  보관함의 `raw/assets/paper-reviews/`에도 내용별 사본을 둡니다.
  **Acrobat에서 PDF 열기**와 **원본 PDF**는 `file:///` 링크로 외부 기본 PDF 앱을 엽니다.
  현재 PC의 기본 앱은 Acrobat이며 PC 경로가 바뀌면 이 외부 링크를 확인해야 합니다.
  원본 파일과 기존 첨부는 수정하지 않습니다.
- 이미 존재하는 노트는 본문·제목·속성 전체를 그대로 엽니다. 기존 자동 구간 표시가 있어도
  자유롭게 편집할 수 있습니다. 재열기·완료 상태 저장·기존 반영 스크립트는 노트를 덮어쓰지 않습니다.
- 그림은 Obsidian 노트에 붙여넣고 수식은 `$…$` 또는 `$$…$$`로 작성합니다.
  Acrobat 주석을 쓰면 별도 PDF 사본에서 작업하고 Obsidian 노트에 연결해 관리합니다.
- 웹은 추천과 완료 여부만 관리합니다. Obsidian 본문을 DB로 가져오거나 웹에서 렌더링하지 않으며,
  완료 체크는 웹 DB에만 저장하며 노트에 상태 속성을 추가하지 않습니다.
- 이전 웹 메모가 있으면 새 노트를 처음 만들 때 기존 그림·주석 PDF와 함께 옮깁니다.
  기존 노트가 있으면 덮어쓰지 않으며 웹의 **이전에 웹에 저장한 메모**에서 복사할 수 있습니다.
- 노트 생성에 실패하면 원인을 표시하고 다시 시도할 수 있습니다. 완료 체크 저장과는 독립적입니다.

노트 목록은 `wiki/paper-reviews.md`, 연결 상태는 `work/paper-server-reviews.json`에 보관합니다.
보관함의 노트·첨부를 함께 백업하세요. 이전 웹 첨부 폴더 `py_01_data/06_personal_reviews/`와
`REVIEW_MEDIA_DIR` 설정은 기존 데이터 보존을 위해 유지합니다. 웹 페이지는 PDF 편집 도구나
Markdown·KaTeX 라이브러리를 불러오지 않습니다.

기존 웹 리뷰를 아직 노트가 없는 논문에 처음 반영하려면 다음 명령을 사용합니다.
기존 Obsidian 노트는 변경하지 않습니다.

```powershell
.venv\Scripts\python.exe scripts\sync_personal_reviews_obsidian.py
```

## 데이터 요약

- 총 **260,557편** — IEEE 99,066 / Optica 150,771 / Nature 10,720
- 출처: JSSC, TCAS-I/II, ISSCC, ISCAS, VLSI, RFIC, ESSCIRC, ASSCC …(IEEE)
  / OE, OL, JLT, PR, Optica (Optica — Crossref API로 수집)
  / NPHOTON(4,781) · NELECTRON(1,502, 필터 없음) · NCOMMS(4,437, 포토닉스+회로/전자
  키워드로 필터링해서 채택) — Nature 3개 저널, `scripts/fetch_nature_crossref.py`

## 퍼블리셔 구분 — `source_system` 컬럼 + `papers_ieee`/`papers_optica`/`papers_nature` VIEW

물리 테이블은 여전히 `papers` 하나뿐입니다. `source_system`(ieee/optica/nature) 컬럼이
퍼블리셔를 **명시적으로** 저장하고, `import_excel_to_db.py`가 URL 패턴(`extract_key()`)
으로 매 행마다 이 값을 채웁니다.

> 예전엔 `article_number`가 숫자면 IEEE, 아니면 Optica로 추론했는데, Nature 키
> (`s41566-...`)도 숫자가 아니라서 Optica와 충돌했음 — 그래서 컬럼을 명시적으로 뒀습니다.

`scripts/schema.sql`에 읽기 전용 VIEW 3개도 정의해둠:
```sql
papers_ieee    -- source_system = 'ieee'
papers_optica  -- source_system = 'optica'
papers_nature  -- source_system = 'nature'
```
- **기존 스크립트(`download_pdfs.py`/`download_optica_pdfs.py`/`download_nature_pdfs.py`,
  `export_zotero_ris.py`/`export_favorites_xlsx.py`, `check_favorite_pdfs.py`)는 전부
  `source_system` 컬럼으로 대상을 정확히 구분** — regex 추론 방식은 다 걷어냄.
- `/api/recommendations`는 출처당 최대 100편분으로 제한한 전체 즐겨찾기 프로필과 로컬 프로필을 혼합한다.
  개별 즐겨찾기와의 기술 용어·가중 유사도 기준을 통과한 후보 중 전체 최대 60편을 추천한다.
  `source_name`별 20편은 상한(`?per_source=N`, 1~20)이며 최소 할당은 없다.
  Correction·중복 표기·거의 같은 즐겨찾기 제목과 명시적으로 제외한 논문을 제거한다.
  Ollama `gpt-oss:20b`가 기본 64편을 평가하며, 완성된 AI 목록은 근거 검증 통과·70점 이상만 표시한다.
  `mode=match|explore|recent|survey|diverse`로 관심사·인접 분야·최신성·SerDes 보강·다양성을 선택한다.
  결과는 DB에 캐시하며, 24시간이 지난 뒤 화면 조회 시 백그라운드로 갱신한다.
  관리자만 `POST /api/recommendations/refresh`로 강제 갱신할 수 있다(CSRF 필수).
  AI 준비 중이거나 실패하면 SQL 추천을 제공한다. 상세 설정과 제한은 [LOCAL_AI_RESEARCH.md](docs/development/LOCAL_AI_RESEARCH.md) 참고.
  `011_recommendation_feedback.sql` 적용 후 관리자는 `관심 없음/검토 완료`로 제외하고 기록 목록에서 복원할 수 있다.
- 웹 UI 검색바에 "퍼블리셔" 드롭다운으로 IEEE/Optica/Nature 필터링 가능 (`/api/papers?publisher=nature` 등).
- VIEW를 되돌리려면 `DROP VIEW papers_ieee, papers_optica, papers_nature;` — 데이터 손실 없음.
  단 `source_system` 컬럼 자체는 여러 스크립트가 의존하므로 컬럼까지 지우려면 코드도 같이 되돌려야 함.

## Nature 자동화 (`scripts/fetch_nature_crossref.py`, `scripts/update_nature_weekly.bat`, `scripts/download_nature_pdfs.py`)

Nature Photonics(전체) + Nature Electronics(전체) + Nature Communications(제목 키워드 필터링, 기본 내장 목록)를
Crossref API로 수집 — IEEE/Optica와 마찬가지로 프록시/캡차 불필요. PDF는 두 저널 다
연세대 프록시로 다운로드(로그인 1번 후 `<기사URL>.pdf`를 requests로 바로 받는
IEEE 방식과 동일 — Optica처럼 논문마다 브라우저 재접속 필요 없음). PDF는 `nature-pdf/`
폴더에 저장.

메타데이터 품질 정책: 저자 정보가 전혀 없는 Editorial·In This Issue 등은 수집 및 DB
import 단계에서 제외한다. 기존 제외 레코드는 날짜가 붙은
`papers_quarantine_nature_no_authors_*` 테이블에 보존하므로 필요 시 복구할 수 있다.

```bat
REM 전체 이력
.venv\Scripts\python.exe scripts\fetch_nature_crossref.py --full

REM 2000년부터 현재까지 명시적으로 갱신
.venv\Scripts\python.exe scripts\fetch_nature_crossref.py --start-year 2026 --end-year 2000

REM 최근 구간만 (기본: 올해~작년)
.venv\Scripts\python.exe scripts\fetch_nature_crossref.py

REM 주간 배치 수동 실행
scripts\update_nature_weekly.bat

REM 즐겨찾기 PDF 다운로드(하루 누적 최대 30건)
.venv\Scripts\python.exe scripts\run_pdf_download_routine.py --provider nature --limit 30
```

**Windows 작업 스케줄러 등록** (주 1회, 매주 월요일 03:00):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_nature_weekly_task.ps1
```

작업 이름은 `IEEE_Paper_Server_NatureWeeklyUpdate`이며, 최근 2개년 메타데이터 수집과
MySQL 업서트만 수행한다. PDF 다운로드는 일일 작업으로 분리되어 있다.
- 확인: `schtasks /query /tn "IEEE_Paper_Server_NatureWeeklyUpdate" /v /fo LIST`
- 삭제: `schtasks /delete /tn "IEEE_Paper_Server_NatureWeeklyUpdate" /f`

## IEEE-only SerDes survey (`/serdes`)

논문 탐색 범위는 기존 IEEE 메타데이터의 넓은 후보군을 유지하고, 성능값은 별도
`serdes_*` 테이블에 저장합니다. 따라서 학회판/저널판 또는 여러 동작점 때문에 논문
목록의 페이지 수가 늘어나지 않으며, 차트만 logical implementation 단위로 중복을
제거합니다.

- Oregon State `WLink_survey_2025.xlsx`의 기준값은 `reference_xlsx` 근거로 저장합니다.
- 초록은 연세 VPN/프록시가 아니라 `.env`의 `IEEE_API_KEY`와 공식 IEEE Metadata API를
  사용해 저속·증분으로 캐시합니다. 웹 요청 중에는 외부 API를 호출하지 않습니다.
- Title / Abstract / Reference / PDF를 구분하고, 필드별 근거 문장·위치·검토 상태를
  `serdes_measurement_evidence`에 남깁니다.
- Optical / Electrical은 topology가 섞인 기존 `link_class`와 분리해
  `serdes_paper_link_media`에 3-state(`optical`, `electrical`, `unspecified`)와
  판정 근거·신뢰도·classifier version을 저장합니다.
- IEEE 원문 버튼은 DB에 예전 프록시 URL이 남아 있어도
  `https://ieeexplore.ieee.org/document/<article_number>/`로 표시합니다.

```powershell
# 기존 DB에 additive schema만 적용(반복 실행 안전)
.venv\Scripts\python.exe scripts\serdes_data_pipeline.py --migrate-only

# WLink 기준값 + 제목 단서 갱신
.venv\Scripts\python.exe scripts\serdes_data_pipeline.py `
  --import-wlink tmp\WLink_survey_2025.xlsx --extract-titles

# 공개 IEEE 초록 40건 증분 수집 후 수치 추출
.venv\Scripts\python.exe scripts\serdes_data_pipeline.py `
  --fetch-abstracts 40 --extract-abstracts

# 저장된 제목·현재 초록·Core 판정으로 link medium 전체 재생성
.venv\Scripts\python.exe scripts\serdes_data_pipeline.py --classify-media
```

`start_local_server.ps1`과 `start_public_server.ps1`은 MySQL 시작 후 migration을 자동으로
확인합니다. abstract에 여러 energy/bit 동작점이 있으면 최소값이나 첫 값을 임의로
대표값으로 정하지 않고 후보 근거로 보존합니다.

## API

| 엔드포인트 | 설명 |
|---|---|
| `GET /` | 메인 UI |
| `GET /api/meta` | 연도·출처·**퍼블리셔**(publishers) 목록, 전체/PDF/즐겨찾기 개수 |
| `GET /api/papers` | `q, year, publisher, source, type, pdf_only, fav_only, page, size` 로 검색/필터/페이징 (`publisher`: ieee/optica/nature) |
| `GET /api/recommendations` | 관련성 기준 통과 추천. `mode` 5종, 전체 최대 60편·출처당 최대 20편, AI 미준비 시 SQL 후보 |
| `POST /api/recommendations/refresh` | 관리자 전용 AI 추천 재생성(CSRF 필수), 기존 결과는 갱신 중에도 조회 가능 |
| `GET/POST /api/recommendations/feedback` | 공유 제외 기록 조회 및 관리자 관심 없음·검토 완료·복원(CSRF 필수) |
| `GET /api/serdes/meta` | IEEE 후보군과 성능/초록 커버리지 |
| `GET /api/serdes/papers` | IEEE-only SerDes 후보 논문 검색·필터·페이징(`medium=optical/electrical/unspecified`), 최선 성능 근거 포함 |
| `GET /api/serdes/performance` | logical implementation당 최선의 근거 연결 성능점 |
| `GET /api/serdes/papers/<번호>/evidence` | 해당 논문의 필드별 수치 근거와 출처 |
| `POST /api/favorite` | 즐겨찾기 토글. body `{article_number, favorite?}` (favorite 생략 시 반전) |
| `GET /pdf/<번호>` | 로컬 PDF 서빙 (IEEE 숫자키→ieee-pdf, Nature키(`s\d5-...`)→nature-pdf, 그외→optica-pdf, 경로조작 방지) |

**계정별 즐겨찾기(★)**: 논문·PDF·측정값은 공유하고 즐겨찾기·추천 제외 기록·AI 작업은
`account_id`로 분리합니다. 관리자와 일반(`member`) 계정은 자신의 즐겨찾기를 수정하며,
읽기 전용(`viewer`) 계정은 조회만 가능합니다. 공용 측정값 수정·승인은 관리자만 가능합니다.
계정 생성·이전·백업과 기존 자동화 호환은 [ACCOUNT_ISOLATION.md](docs/development/ACCOUNT_ISOLATION.md)를 참고하세요.

관리자 비밀번호를 새로 설정하거나 교체할 때는 아래 파일을 실행합니다.

```bat
scripts\setup_admin_auth.bat
```

비밀번호는 두 번 입력하며 평문은 저장하지 않습니다. 계정 이전 후에는 Werkzeug
해시를 DB에 저장하며 서버 재시작 없이 적용됩니다. 계정명은 유지됩니다.

기존 읽기 전용 계정(`AUTH_USERNAME`, `.env`에 명시)의 비밀번호를 교체할 때는
아래 파일을 실행합니다. 해당 읽기 전용 계정의 DB 비밀번호만 교체합니다.

```bat
scripts\setup_viewer_auth.bat
```

비밀번호를 두 번 입력하면 바로 적용됩니다. 임의 계정은
`scripts/setup_account_auth.py --username <계정명>`을 사용합니다.

## 외부 공개 (ngrok)

ngrok은 아직 설치되지 않았습니다(Windows Defender가 실행파일을 차단, 오탐).
아래 중 하나로 설치 후 사용하세요.

```bat
REM 방법 A: winget (권장)
winget install ngrok.ngrok

REM 방법 B: https://ngrok.com/download 에서 받아 Defender "허용" 후 사용
```
설치 후 (무료 가입해서 authtoken 발급 필요):
```bat
ngrok config add-authtoken <본인_authtoken>
ngrok http 5001
```
→ 출력되는 `https://xxxx.ngrok-free.app` 주소로 외부에서 접속.
   (Waitress 서버가 `127.0.0.1:5001`에서 실행 중이어야 함.)

## PDF 다운로드 (`scripts/download_pdfs.py`) — 즐겨찾기(★) 전용

**항상 즐겨찾기(★) 등록된 논문만 대상으로 합니다** (DB `is_favorite=1` 조건이 하드코딩되어 있음).
연세대 프록시로 로그인 → 세션 쿠키를 requests 로 이식 → `stamp.jsp`/`getPDF.jsp`
로 PDF를 받아 `ieee-pdf\<article_number>.pdf` 저장 + DB 갱신합니다.
(Selenium + Edge 브라우저 필요. IEEE 숫자 키만 대상 — Optica는 제외)
Optica PDF는 `optica-pdf\<uri키>.pdf` 로 직접 넣으면 뷰어에서 열립니다.

PDF 다운로더는 일간 routine 내부에서만 실행되며, 전체·무제한 다운로드는 지원하지 않습니다.
Optica는 매일 09:00에만 최대 13건을 처리합니다. 19:00 통합 작업에서는 Optica를
시도하지 않고 IEEE/Nature 및 복구 큐만 처리합니다.

```bat
REM IEEE만 1건 시험(같은 날짜의 누적 30건 예산에 포함)
.venv\Scripts\python.exe scripts\run_pdf_download_routine.py --provider ieee --limit 1
```

운영 진입점의 `--limit N`은 1~30만 허용되며, 같은 날짜의 누적 예산도 30건입니다.

- 이미 받은 파일은 자동 건너뜀(이어받기 가능), 세션 만료 시 자동 재로그인 시도.
- **첫 실행은 창을 띄운 채(기본) 소량으로** 돌려 로그인·다운로드가 정상인지 확인하세요.
  로그인 페이지 구조나 기관 구독 범위에 따라 일부는 접근 불가할 수 있습니다.
- 파일을 직접 `ieee-pdf\<번호>.pdf` (또는 Optica는 `optica-pdf\<키>.pdf`) 로 넣은 뒤
  `import_excel_to_db.py` 를 다시 돌려도 `pdf_available` 이 갱신됩니다.

## Zotero 즐겨찾기 단방향 동기화 (`scripts/sync_zotero_favorites.py`)

MySQL에서 `is_favorite=1`, `pdf_available=1`이고 로컬 PDF 실파일이 존재하는
항목만 원본으로 사용하고 Zotero 개인 라이브러리를 `PaperServerFavorite` 전용
태그와 `IEEE•Optica•Nature` 컬렉션으로 미러링한다.
Zotero에서 바꾼 상태는 DB로 가져오지 않는다.

`.env`에 쓰기 권한이 있는 `ZOTERO_USER_ID`, `ZOTERO_API_KEY`가 필요하다.

```bat
REM 변경 계획만 확인 — Zotero 쓰기 없음(기본)
.venv\Scripts\python.exe scripts\sync_zotero_favorites.py

REM 실제 반영
.venv\Scripts\python.exe scripts\sync_zotero_favorites.py --apply

REM 중복 정리까지 미리보기(쓰기 없음)
.venv\Scripts\python.exe scripts\sync_zotero_favorites.py --dedupe

REM 자식 항목을 보존하며 중복 부모를 휴지통으로 이동
.venv\Scripts\python.exe scripts\sync_zotero_favorites.py --apply --dedupe --max-changes 5000

REM MySQL 시작 + 실제 반영 + logs/ 로그 저장
scripts\sync_zotero_favorites.bat
```

- 실제 반영 전 `py_02_reports/zotero_sync_backup_*.json`에 DB 즐겨찾기와 변경 범위를
  자동 백업한다. `--dedupe` 반영 시에는 첨부·메모를 포함한 활성 항목 전체를 남긴다.
- 계획·검증 결과는 각각 `zotero_sync_plan_*.json`, `zotero_sync_verify_*.json`에 남긴다.
- 평상시 동기화는 DB의 실PDF 보유 즐겨찾기와 관리 태그·전용 컬렉션의 소속을
  일치시킨다.
- `--dedupe`를 지정하면 동일 article number의 대표 논문에 사용자 태그와 다른
  컬렉션 소속을 합치고, 중복 부모의 첨부·메모를 대표 논문에 재연결한 다음 중복
  부모만 복구 가능한 Zotero 휴지통으로 이동한다. 휴지통을 자동으로 비우지는 않는다.
- 신규 대상은 전용 컬렉션에 Zotero 메타데이터 항목으로 생성한다.
  `sync_zotero_pdf_attachments.py --apply --mode linked`가 Paper Server PDF 원본
  경로를 `linked_file` 자식 첨부로 연결한다. 기존 `PaperServerLinkedPDF` 첨부의
  절대 경로가 바뀌면 DB의 검증된 현재 파일 경로로 복구하며 첨부 키와 주석을
  보존한다. 사용자 첨부, 저장형 파일, `attachments:` 상대 경로는 변경하지 않는다.
  새 PC에서 저장형 첨부가 있는 논문도 로컬 원본으로 바로 열려면
  `--apply --mode linked --ensure-local-links`로 기존 첨부 옆에 로컬 링크를 추가한다.
- Zotero 데스크톱 앱에서 계정 동기화가 활성화돼 있어야 Web API 변경이 로컬로 내려온다.

두 Zotero 동기화는 별도 고빈도 작업으로 등록하지 않는다. 09:00 또는 19:00 PDF
작업이 성공한 경우에만 메타데이터/컬렉션 → linked PDF 첨부 → Obsidian ingest
순서로 실행된다.

### Zotero 라이브러리를 SQL 즐겨찾기 전용으로 엄격 정리

`scripts/reconcile_zotero_library.py`는 `My Library`의 상위 항목을 DB 즐겨찾기와
정확히 일치시키고, 모든 항목을 `IEEE•Optica•Nature` 컬렉션 하나에만 넣는다.
SQL 밖의 활성 항목은 휴지통으로 이동한다. 휴지통 영구 삭제는 별도 플래그가 있어야
실행되며, 적용 전 활성 항목·휴지통·컬렉션 메타데이터를 JSON으로 백업한다.

```bat
REM 계획만 확인
.venv\Scripts\python.exe scripts\reconcile_zotero_library.py

REM SQL 밖 활성 항목만 휴지통으로 이동
.venv\Scripts\python.exe scripts\reconcile_zotero_library.py --apply

REM SQL 밖 항목 정리 후 휴지통까지 영구 비우기
.venv\Scripts\python.exe scripts\reconcile_zotero_library.py --apply --empty-trash
```

Zotero의 `My Library`는 컬렉션을 포함하는 전체 보기이므로 0건이 되지 않는다. 정상
완료 상태는 `My Library = IEEE•Optica•Nature = DB 즐겨찾기 수`, `Unfiled Items = 0`,
`Trash = 0`이다.

## Zotero RIS 수동 내보내기 — 레거시 (`scripts/export_zotero_ris.py`)

즐겨찾기(★) 논문 전체를 RIS로 내보냅니다. PDF를 보유한 건은 로컬 파일이
`L1` 필드로 첨부되어, Zotero Import 시 메타데이터 + PDF가 함께 들어갑니다.

> 단방향 API 동기화를 사용하는 현재 라이브러리에 RIS를 다시 Import하면 중복이
> 추가될 수 있다. 신규 라이브러리로 일회성 이전할 때만 사용한다.

```bat
.venv\Scripts\python.exe scripts\export_zotero_ris.py
```
→ `py_02_reports\zotero_favorites.ris` 생성. Zotero에서
`File > Import... > "A file (BibTeX, RIS, Zotero RDF, ...)"` 로 가져오면 됩니다.

## 주간 메타데이터 자동 업데이트

현재 운영 기준은 [METADATA_UPDATE.md](docs/operations/METADATA_UPDATE.md)에 정리되어 있습니다.
모든 출처는 `config/metadata_sources.json`에서 관리하며, 세 주간 배치는
`scripts/run_metadata_update.py`를 공통으로 사용합니다.

- IEEE: 월요일 03:00. JSSC/TCAS-I/TCAS-II/TMTT, OJSSC, SSCL/PTL/MWTL/SSC-M,
  ISSCC/VLSI/RFIC/ESSERC/ISCAS/ASSCC/MWSCAS/APCCAS/NEWCAS/BCICTS/CICC/ICTA.
- Nature: 월요일 03:00. Nature Photonics/Electronics/Communications.
- Optica: 월요일 03:30. OE/OL/PR/Optica/JLT의 5개 저널과 OFC 학회.

기본 수집 범위는 올해와 전년도이며, 모든 주간 수집은 Crossref 공개 메타데이터 API를
사용합니다. IEEE 브라우저 스윕은 수동 진단용으로 유지됩니다. PDF 다운로드는 별도
일일 작업이 담당합니다.

실패·출처 전체 0건·연도별 건수 급감은 비정상 종료와 보고서로 드러나며, 해당
파이프라인의 적재를 차단합니다. 성공하면 이번 실행의 검증된 파일만 적재하고 기존
즐겨찾기와 PDF 상태를 보존합니다. 상세 결과는
`logs/metadata_updates/latest_<pipeline>.json`에서 확인합니다.

```powershell
# DB/네트워크를 사용하지 않는 설정·의존성 점검
scripts\update_ieee_weekly.bat --self-test
scripts\update_nature_weekly.bat --self-test
scripts\update_optica_weekly.bat --self-test

# 최근 구간 수집과 검증만 (DB 쓰기 없음)
.venv\Scripts\python.exe scripts\run_metadata_update.py --pipeline ieee --collect-only

# 정상 주간 수집·검증·적재
scripts\update_ieee_weekly.bat
scripts\update_nature_weekly.bat
scripts\update_optica_weekly.bat
```

2026-09-14 Windows 권한을 확보해 확인한 결과, 세 예약 작업은 모두 등록되어
현재 워크폴더를 실행합니다. 이전의 '미등록' 진단은 권한 거부를 오해한 결과입니다.
`StartWhenAvailable`, 중복 실행 방지와 Interactive 로그온 설정이 적용되어 있습니다.
설치 스크립트는 최초 설치/설정 복구 때만 사용합니다.

과거 수집 이력의 참고 사항:

- JLT는 인쇄 ISSN `0733-8724`와 DOI 기반 안정 키를 사용하고 Optica 소속을 유지합니다.
- MWCL은 2006–2022년, MWTL은 2023년 이후로 별도 출처를 유지합니다.
- SSC-M은 인쇄 ISSN `1943-0582`를 사용합니다.
- VLSI-Circuits는 2021년까지이며 통합 학회는 `VLSI-Tech`로 저장합니다.
  2025년 `10.23919`와 2026년 `10.1109` DOI 접두사를 모두 지원합니다.
- ESSCIRC는 2023년까지, 후속 ESSERC는 2024년부터입니다.
- OFC의 Crossref 수집 범위는 2009년 이후, OJSSC는 2021년 이후입니다.
- 제목·저자·출판사 URL을 검증하며, 보존 중인 과거 레코드나 Crossref에 없는 레코드를
  수집 결과에 없다는 이유로 삭제하지 않습니다.

### Optica 오후 확보 실험 (2026-09-30)

오전 최대 13건을 유지하고, 19:00 통합 작업에서 Optica 최대 8건을 추가 처리합니다. 오후 항목 간격은 1,500~2,400초 범위의 난수(random.uniform)이며 분 단위 반올림 없이 대기합니다. Optica 하루 누적 시도 상한은 15건(실패 포함), 전체 상한은 30건입니다. 오전에 13건을 시도하면 오후에는 최대 2건만 처리합니다. (2026-10-05 변경) 오후 Optica 예산은 앞선 IEEE/JLT 큐보다 먼저 확보하며, CAPTCHA/속도제한 쿨다운은 유지합니다. 작업 제한 시간은 8시간입니다.

source_name=JLT이고 저장 URL이 IEEE document인 항목은 jlt_ieee 큐로 분리합니다. 별도 로그인·쿨다운·시도 집계를 사용하며 Optica 분류와 저장 키는 유지합니다. Optica URL만 있는 JLT의 DOI→IEEE 매핑은 아직 포함하지 않습니다.

## Connection 논문 지도

`/connection`은 기존 MySQL 논문을 검색·선택해 독립적으로 계산한 연구 지도를 표시합니다.
저장소·SerDes 메뉴와 각 논문의 `지도` 링크에서 접근하며, 기존 로그인과 계정별
즐겨찾기·보유 PDF 조회 권한을 그대로 사용합니다. SQL 원본과 스키마는 변경하지 않습니다.

- 제목·저자·DOI·논문 식별자로 검색하고 기준 논문을 선택합니다.
- 탐색 화면은 왼쪽 Paper List, 중앙 Graph, 오른쪽 Selected Paper로 구성됩니다.
  왼쪽 목록은 지도 속 논문 선택과 SQL 저장소 검색을 전환하며, 그래프·시간축 계보도·
  List View·Prior Works·Derivative Works가 같은 상세 패널을 공유합니다.
- Filters는 제목·저자, 연도 범위, 최소 인용 수, 보유 여부, PDF 여부로 이미 수집한
  논문을 거릅니다. 기준 논문은 항상 표시하며 필터·보기·정렬 전환은 API를 호출하지 않습니다.
- 논문별 인용 수는 그래프 점 옆 숫자, Paper List, List View, 선행·후속 목록과 상세에
  표시합니다. 인용 모드는 OpenAlex 집계와 수집 날짜를 사용하며, OpenAlex 값이 없을
  때만 SQL의 저장된 IEEE/Crossref 값을 출처와 날짜 그대로 보완합니다. SQL 검색과
  제목·초록 모드는 저장된 집계를 사용합니다. 미확인과 0회를 구분하고, 집계 출처별
  차이가 있을 수 있으므로 숫자를 합산하지 않습니다. 표시를 위한 추가 외부 호출은 없습니다.
- 시간축 계보도는 출판 연도를 수평축에 놓고 **인용된 논문 → 이를 인용한 논문**의
  방향으로 화살표를 그립니다. 실제 참고문헌 ID에서 만든 `citation_edges`만 사용하며,
  공통 참고문헌·동시 인용 유사도에서 인용 방향을 추정하지 않습니다. 같은 연도는
  세로로 분산하고, 연도 미상은 별도 열에 표시합니다. 원본 메타데이터의 연도와
  인용 순서가 다르더라도 실제 인용 방향을 보존합니다. 가로·세로 스크롤과 기준 논문
  위치 이동을 지원하며, `view=timeline` 주소로 직접 열 수 있습니다.
- 계층 기준은 시작 논문으로 초기화하며, 논문 선택 후 **선택 논문으로 설정**을 누르면
  해당 논문을 중심으로 바꿉니다. 방향은 선행·후속·양쪽, 깊이는 기본 1단계부터 최대
  3단계까지이며, 계층 표시 상한 30·60편을 독립적으로 조절합니다. 계보도와
  Prior/Derivative 목록이 같은 설정을 공유합니다. Graph/List의 유사성 지도는 유지합니다.
  단계는 실제 직접 인용의 최단 경로 길이이며, 순환·중복 관계를 제거하고 기준 논문을
  항상 포함합니다. 표시 상한 안에서 방향·단계별로 번갈아 선택하고 경로의 중간 논문을
  함께 포함해, 직접 인용이 많은 기준 논문도 깊은 단계가 드러나게 합니다.
  2·3단계도 이미 저장된 최대 180편에서만 탐색하고 추가 수집하지 않습니다.
  필터로 중간 논문이 숨겨져도 가상의 연결선을 만들지 않습니다.
- 기본 **인용 관계 기반** 모드는 DOI로 OpenAlex 공개 메타데이터(CC0)를 조회합니다.
  참고문헌 최대 80편, 인용 논문 최대 60편, 관련·공통 참고문헌 후보를 포함해 최대
  180편을 수집합니다. 작업당 최대 8회 요청, 동시 실행 2개·대기 포함 8개로 제한합니다.
- 공통 참고문헌의 코사인 유사도(0.65), 수집 범위에서 관찰한 동시 인용(0.35),
  직접 인용 보너스(0.12)로 연결을 계산합니다. 동시 인용은 적은 관측 수에 감점합니다.
  기준 논문과 근거가 있는 최대 60편을 표시하고, 주변 연결선은 상위 관계만 남깁니다.
  제목 일치나 OpenAlex 관련 논문 추천만으로 인용 연결을 만들지 않습니다.
- 선행 연구는 표시 논문이 인용한 알려진 논문, 후속 연구는 표시 논문을 인용한 알려진
  논문입니다. 관계 수는 현재 표시 논문과의 관계 수이며 전체 인용 수와 다릅니다.
  DOI가 있는 외부 논문도 다시 기준으로 탐색할 수 있습니다. 점 크기는 전체 인용 수,
  색은 출판 연도이며, 전체 인용망·Connected Papers와 동일한 결과를 의미하지 않습니다.
- 공개 수집 결과만 `py_01_data/connection/`에 30일 캐시합니다(최대 512개 파일).
  기준 논문 한 편만 수집된 결과는 24시간 후 다음 조회에서 갱신하고,
  화면에 연결 정보 부족 안내와 제목·초록 지도 전환 버튼을 표시합니다.
  일부 요청이 실패한 결과는 5분만 캐시해 다음 조회에서 복구할 수 있게 합니다.
  OpenAlex 참고문헌이 비어 있으면 Crossref를 확인하고, 목록이 없으면 공유 저장소의
  보유 PDF References를 읽습니다. DOI 또는 제목·저자·연도로 확인한 항목만 연결하며,
  미확인 항목 수와 PDF 페이지·참고문헌 번호를 표시합니다. PDF 원문과 로컬 경로는
  그래프 캐시에 넣지 않습니다. OpenAlex 미등재 DOI도 Crossref/SQL로 시작할 수 있습니다.
  IEEE 후속 인용은 메타데이터 조회 1회 후 인용수가 양수인 경우 IEEE/non-IEEE 목록을
  각각 1페이지씩 보완합니다(지도당 최대 3회, 자동 페이지 확장·자동 재시도 없음).
  여러 출처의 인용수를 더하지 않으며 IEEE 집계는 별도 메타데이터로 보존합니다.
  기존 OpenAlex 캐시는 외부 원본을 재조회하지 않고 보완 정보를 추가해 업그레이드합니다.
  미확인 참고문헌이 있으면 24시간, IEEE 보완 실패·생략은 1시간 후 다음 조회에서 재검토합니다.
  즐겨찾기·보유 PDF·SQL 초록은 요청마다 해당 계정 권한으로 결합하며 캐시에 넣지 않습니다.

IEEE 호출 보호 설정은 `.env`의 `IEEE_API_DAILY_LIMIT`(기본 50회/최근 24시간),
`IEEE_API_MIN_INTERVAL_SECONDS`(기본 2초)입니다. 이는 서버 자체 상한이며 IEEE 계정의
실제 계약 한도를 의미하지 않습니다. 0회 설정으로 IEEE 원격 요청을 중단할 수 있습니다.
지도와 `scripts/update_favorite_citations.py`는
`py_01_data/connection/provider-state.sqlite3`의 같은 지속 카운터와 응답 캐시를 공유합니다.
성공·실패한 모든 실제 요청을 집계하며, 중복 요청 병합, IEEE 오류 응답 1시간 캐시,
429/인증 제한 시 최소 1시간 공통 중단을 적용합니다. 다른 기기·외부 프로그램의 키 사용량은
이 서버 카운터에 포함되지 않습니다. IEEE 기본 메타데이터는 하루, 인용 목록은 7일 캐시합니다.
현재 운영 키는 사용자 확인(2026-10-17) 기준 초당 10회·하루 200회입니다.
운영 `.env`는 `IEEE_API_DAILY_LIMIT=200`, `IEEE_API_MIN_INTERVAL_SECONDS=2`로 설정했습니다.
서버는 최근 24시간 기준으로 집계하고, 기존 누적 카운터는 재시작 후에도 유지합니다.
  API 키 없이 시작할 수 있고 `OPENALEX_API_KEY`를 서버 환경에 선택적으로 설정할 수 있습니다.
  OpenAlex 사용량 제한·장애가 발생하면 명시적 오류와 SQL 모드 전환을 제공합니다.
- **제목·초록 유사도** 모드는 외부 요청 없이 기존 FULLTEXT 후보 최대 500편과 저장
  초록의 기술 개념을 비교합니다. 초록이 없으면 제목만 사용하며 인용 관계를 뜻하지 않습니다.
- Connected Papers API·scite·외부 모델 호출은 사용하지 않습니다. 추가 Python 패키지는
  필요하지 않습니다. OpenAlex에 전송하는 검색값은 선택 논문의 DOI와 공개 논문 ID입니다.

화면은 `web/templates/connection.html`, `web/static/connection.js`,
`web/static/connection.css`, SQL 조회는 `paper_server/connection.py`, 외부 수집은
`paper_server/citation_provider.py`, 인용 계산·캐시는 `paper_server/citation_graph.py`에 있습니다.
읽기 API는 `/api/connection/search`, `/api/connection/graph`입니다. 그래프 API는
`mode=citation&article_number=...` 또는 `mode=citation&doi=...`를 받습니다.
수집 중에는 202와 `Retry-After`를 반환하며, 완료 후 같은 요청은 200을 반환합니다.
`mode=local` 또는 mode 생략은 기존 SQL 모드입니다.
응답의 `lineage_nodes`·`lineage_edges`는 저장된 주변 논문의 계층 탐색용 데이터입니다.
깊이·방향·계층 상한·계층 기준 변경은 브라우저에서만 계산합니다. 주소의 `depth`,
`direction`, `hierarchy_limit`, `root`로 설정을 공유할 수 있습니다.
API의 `cached_only=1`은 저장 결과만 읽고, 캐시가 없으면 외부 호출 없이 404를 반환합니다.
운영 서버 재시작 후 `.venv/Scripts/python.exe scripts/verify_connection.py`로
로그인 경계·검색·지도·정적 파일을 점검합니다. `--citation`은 샘플 DOI의 인용 지도까지,
`--public`은 기존 공개 도메인까지 확인합니다(외부 공개 메타데이터 캐시는 생성될 수 있습니다).
`--saved-only`를 함께 사용하면 검증 중에도 추가 외부 메타데이터 조회를 하지 않습니다.

## 코드와 문서 위치

서비스 코드는 `paper_server/` 패키지에 있습니다. 운영 스크립트는 기존 `scripts/` 경로에서 실행하며, 웹 진입점은 `web.app:app`입니다. PDF·DB·캐시 경로는 유지합니다.

문서는 [문서 목차](docs/README.md)에서 찾을 수 있습니다. 문서의 실행 명령은 프로젝트 루트에서 실행합니다.
