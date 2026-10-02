# IEEE Paper Repository — 운영 가이드

수집한 IEEE/Optica 논문 메타데이터(Excel)를 MySQL에 적재하고, Flask로
검색·필터·PDF 뷰어가 가능한 웹사이트를 제공합니다.

## 구성

```
IEEE_Paper_Server/
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
  AI 준비 중이거나 실패하면 SQL 추천을 제공한다. 상세 설정과 제한은 [LOCAL_AI_RESEARCH.md](LOCAL_AI_RESEARCH.md) 참고.
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
계정 생성·이전·백업과 기존 자동화 호환은 [ACCOUNT_ISOLATION.md](ACCOUNT_ISOLATION.md)를 참고하세요.

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
  경로를 `linked_file` 자식 첨부로 연결하며 기존 첨부는 그대로 보존한다.
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

현재 운영 기준은 [METADATA_UPDATE.md](METADATA_UPDATE.md)에 정리되어 있습니다.
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

오전 최대 13건을 유지하고, 19:00 통합 작업에서 Optica 최대 8건을 추가 처리합니다. 오후 항목 간격은 1,500~2,400초 범위의 난수(random.uniform)이며 분 단위 반올림 없이 대기합니다. Optica 하루 누적 상한은 21건, 전체 상한은 30건입니다. 오후 Optica 예산은 앞선 IEEE/JLT 큐보다 먼저 확보하며, CAPTCHA/속도제한 쿨다운은 유지합니다. 작업 제한 시간은 8시간입니다.

source_name=JLT이고 저장 URL이 IEEE document인 항목은 jlt_ieee 큐로 분리합니다. 별도 로그인·쿨다운·시도 집계를 사용하며 Optica 분류와 저장 키는 유지합니다. Optica URL만 있는 JLT의 DOI→IEEE 매핑은 아직 포함하지 않습니다.
