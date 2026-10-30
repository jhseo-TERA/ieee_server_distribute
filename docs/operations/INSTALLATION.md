# 새 사용자 설치 가이드

아래는 **새 Windows PC와 빈 MySQL 데이터베이스**를 준비하는 순서입니다.
명령은 저장소 루트에서 PowerShell로 실행합니다. 기존 운영 DB를 업그레이드할 때는
이 초기화 절차를 반복하지 말고 먼저 백업한 뒤 변경된 마이그레이션을 확인하세요.

## 1. 준비물과 범위

| 항목 | 기본 실행에 필요한 내용 |
| --- | --- |
| Git | 저장소 복제와 업데이트 |
| Python | 3.13, `.venv`를 새 PC에서 새로 생성 |
| MySQL | 8.4, 서버가 실행 중이어야 하며 `mysql` 명령을 사용할 수 있어야 함 |
| Node.js | 24, JavaScript 테스트와 선택적 OCR 도구에 사용 |
| 브라우저 | 로컬 웹 화면과 PDF 열람 |

잠금 파일 `requirements.lock.txt`는 이 프로젝트의 Python 의존성 목록입니다.
가상환경 자체를 다른 PC에서 복사하지 마세요.
([Python venv 문서](https://docs.python.org/3.13/library/venv.html))

기본 검색·즐겨찾기·보유 PDF 열람에는 API 키가 필요하지 않습니다.
메타데이터 수집, 외부 인용수, 자동 다운로드, AI·OCR은 별도 기능입니다.
운영체제가 다른 경우 Windows 배치·PowerShell·예약 작업·Acrobat 연동을 조정해야 합니다.
이 가이드의 DB 초기화는 실제 MySQL에서 실행해야 하며 단위 테스트가 그 검증을 대신하지 않습니다.

## 2. 소스와 Python 환경

```powershell
git clone https://github.com/jhseo-TERA/ieee_server_distribute.git
cd ieee_server_distribute
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

이미 복제한 경우 `git status`로 개인 변경을 확인한 다음 `git pull --ff-only`로 갱신합니다.
처음에는 복제부터 진행합니다. ([GitHub 안내](https://docs.github.com/en/repositories/creating-and-managing-repositories/cloning-a-repository))

## 3. 빈 DB와 전용 사용자

MySQL 서버를 시작하고, 저장소 루트에서 관리자 계정으로 클라이언트를 엽니다.

```powershell
mysql --host=127.0.0.1 --user=root --password
```

MySQL 프롬프트에서 아래를 실행합니다. 예제 비밀번호는 **직접 만든 DB 비밀번호로 교체**합니다.

```sql
SOURCE scripts/schema.sql;
CREATE USER 'ieee_app'@'127.0.0.1' IDENTIFIED BY 'REPLACE_WITH_YOUR_DB_PASSWORD';
GRANT ALL PRIVILEGES ON ieee_repo.* TO 'ieee_app'@'127.0.0.1';
EXIT;
```

`schema.sql`은 `ieee_repo` DB와 기본 `papers` 테이블·뷰를 생성합니다.
전용 사용자는 이후 스키마·계정 마이그레이션에 필요한 CREATE·ALTER·TRIGGER 권한도
갖습니다. 웹서버에 MySQL root 비밀번호를 넣지 않습니다.
([MySQL SQL 파일 실행 안내](https://dev.mysql.com/doc/refman/8.4/en/batch-mode.html))

## 4. 로컬 설정 생성

```powershell
.venv\Scripts\python.exe scripts/create_local_config.py
```

DB 호스트·포트·사용자·비밀번호와 새 `admin`·`viewer` 로그인 비밀번호를 입력합니다.
도구는 `.env.example`에서 `.env`를 만들고, 로그인 비밀번호는 해시로 저장하며,
서버 세션키는 새로 생성합니다. DB 비밀번호는 DB 연결을 위해 `.env`에 저장합니다.
기존 `.env`는 덮어쓰지 않습니다. DB에 접속하거나 계정을 생성하는 단계는 아닙니다.

| 설정 | 의미 |
| --- | --- |
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | 본인 MySQL 연결 정보 |
| `AUTH_USERNAME`, `AUTH_PASSWORD_HASH` | 최초 viewer 계정 생성용 값 |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH` | 최초 관리자 계정 생성용 값 |
| `APP_SECRET_KEY` | 설치별 고유 세션키, 변경 시 기존 쿠키가 무효화됨 |
| `ACCOUNT_AUTH_ENABLED=1` | 기본 DB 계정 모드 |
| `SESSION_COOKIE_SECURE=0` | 로컬 HTTP용, HTTPS 배포 시 `1` |

공개 배포본의 개인 리뷰·계정 활동 전용 계정명은 `admin`입니다.
별도 계정으로 운영하려면 관련 권한 검사도 함께 조정해야 합니다.
`.env`, DB 백업, 개인 노트, API 키를 Git에 추가하지 마세요.

## 5. 부가 스키마와 계정 초기화

```powershell
.venv\Scripts\python.exe scripts/serdes_data_pipeline.py --migrate-only
mysql --host=127.0.0.1 --user=ieee_app --password ieee_repo
```

이어 MySQL 프롬프트에서 새 DB에 다음 마이그레이션을 순서대로 적용합니다.
앞의 Python 명령은 `001`~`011` 스키마를 준비합니다.

```sql
SOURCE scripts/migrations/012_recommendation_pair_cache.sql;
SOURCE scripts/migrations/013_accounts.sql;
SOURCE scripts/migrations/014_viewer_favorites_source.sql;
SOURCE scripts/migrations/015_daily_review.sql;
SOURCE scripts/migrations/016_account_activity.sql;
EXIT;
```

이후 최초 `.env`의 admin·viewer 해시를 DB로 옮기고 member 계정을 준비합니다.

```powershell
.venv\Scripts\python.exe scripts/migrate_accounts.py --new-user member --copy-from admin --apply
.venv\Scripts\python.exe scripts/setup_account_auth.py --username member
```

`member`는 비밀번호를 설정해야 활성화됩니다. `admin`·`member`는 자기 즐겨찾기를
편집할 수 있고 `viewer`는 읽기 전용입니다. DB 모드에서는 이후 비밀번호 변경을
`setup_account_auth.py --username 계정명`으로 수행합니다. `.env` 해시를 바꿔도
이미 생성된 DB 계정의 비밀번호를 자동으로 바꾸지는 않습니다.

## 6. 첫 실행과 데이터

```powershell
.venv\Scripts\waitress-serve.exe --host=127.0.0.1 --port=5001 web.app:app
```

[로컬 화면](http://127.0.0.1:5001)에 접속해 `admin` 또는 `viewer`로 로그인합니다.
`/health`는 프로세스 생존 확인이며 DB·외부 서비스 상태까지 확인하는 것은 아닙니다.
중지는 실행한 터미널에서 `Ctrl+C`입니다.

이 저장소에는 운영 DB와 PDF가 없습니다. 빈 목록에서 시작합니다.

- 본인의 메타데이터 Excel은 `py_01_data/00_metadata/`에 준비하고
  `scripts/import_excel_to_db.py`로 가져옵니다. 컬럼·출처별 규칙은 해당 스크립트와
  [메타데이터 가이드](METADATA_UPDATE.md)를 확인하세요.
- Crossref 수집은 [메타데이터 가이드](METADATA_UPDATE.md)에서 범위·옵션을 확인한 뒤 실행합니다.
- 보유 PDF는 `ieee-pdf/`, `optica-pdf/`, `nature-pdf/`, `DesignCon/`과
  DB의 `pdf_local_path`, `pdf_available`을 맞춰야 합니다. 경로만 복사해도 DB가 자동 복구되지는 않습니다.
- API 키 없이도 저장된 논문은 열람할 수 있으나 외부 인용 관계·인용수 갱신은 제한될 수 있습니다.

## 7. 선택 기능

| 기능 | 추가 준비 |
| --- | --- |
| 외부 인용수·인용 관계 | 본인의 `IEEE_API_KEY`·`OPENALEX_API_KEY`, `CROSSREF_MAILTO`, 제공자 이용 조건·요청 한도 확인 |
| 논문 자동 다운로드 | 현재 연세대 프록시에 맞춘 로그인·호스트 규칙, Edge/Selenium, 본인의 접근 권한 |
| 다른 기관·프록시 없는 원문 링크 | `web/templates/index.html`의 `PROXY_HOST_MAP` 및 다운로드 인증 규칙을 본인 환경에 맞게 조정 |
| Zotero | 본인 `ZOTERO_USER_ID`, `ZOTERO_API_KEY`, 컬렉션·첨부파일 저장 방식; 기본 `config/pdf_postprocess.json`에서 연동 비활성화 |
| Obsidian 리뷰 | 설치된 Obsidian, 본인 보관함 경로 `OBSIDIAN_VAULT_PATH`; Acrobat 연동은 별도 설치·경로 지정 |
| AI 질문·분석 | 로컬 Ollama와 사용할 모델, `LOCAL_AI_OLLAMA_URL`; 미사용 시 `LOCAL_AI_ENABLED=0` |
| OCR·고급 문서 파서 | [문서 파서](../development/DOCUMENT_PARSERS.md), [참고문헌 파서](../development/REFERENCE_PARSERS.md)의 별도 환경·모델·Java·선택적 GPU |
| 예약 실행·외부 접속 | Windows 작업 스케줄러와 별도 서버·HTTPS 설정; 자동 시작은 [운영 가이드](../../README_SERVER.md) 참고 |

논문을 볼 권한과 PDF를 재배포할 권한은 별개입니다. 소스 라이선스는 외부 논문·데이터에 적용되지 않습니다.

## 8. 검증·업데이트·문제 해결

```powershell
.venv\Scripts\python.exe scripts/run_tests.py
git status
git pull --ff-only
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

업데이트 전 DB·PDF·설정은 [백업 정책](BACKUP_POLICY.md)에 따라 따로 보관합니다.
받은 변경의 마이그레이션을 확인하고, 필요한 항목을 적용한 뒤 서버를 재시작합니다.
Node.js가 없는 경우 테스트에 `--python-only`를 사용할 수 있습니다.

| 증상 | 먼저 확인할 내용 |
| --- | --- |
| 인증 설정 누락으로 시작 실패 | `.env` 존재, 해시와 세션키가 채워졌는지 |
| DB 연결 거부 | MySQL 실행 여부, 호스트·포트·계정·비밀번호·권한 |
| 테이블/컬럼 없음 | 기본 스키마와 필요한 마이그레이션 적용 여부 |
| 로그인 실패 | DB 계정 활성화와 비밀번호; bootstrap 후에는 `.env` 대신 DB 계정 변경 도구 사용 |
| 논문 0개·PDF 없음 | 자신의 메타데이터·PDF와 DB 상태가 준비됐는지 |
| 원문 링크가 다른 기관 프록시로 이동 | 공개본에 남아 있는 기관별 호스트 규칙 조정 |
| OCR·AI 실행 실패 | 기본 Python 환경과 별도 파서/모델 환경을 구분했는지 |

문제를 제보할 때는 OS, Python·Node 버전, 실행 명령, 비밀값을 지운 오류 메시지와
사용한 커밋을 첨부하세요. `.env`·토큰·DB 덤프·개인 논문·노트를 첨부하지 않습니다.

## 설치 절차 검증 기록

2026-10-30에 기존 운영 DB와 분리한 빈 MySQL 8.4.9 인스턴스에서 위 스키마·마이그레이션,
설정 생성, admin·viewer 계정 생성과 관리자 로그인을 확인했습니다. 메인 화면과
`/health`, `/api/meta`, `/api/papers`, `/api/serdes/meta`가 모두 HTTP 200을 반환했습니다.
Python 3.13.15 및 Node.js 24.19.0에서 기본 테스트를 실행했습니다.
