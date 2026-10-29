# 계정별 개인 상태

> 실행 명령과 코드·데이터 경로는 프로젝트 루트 기준입니다.

논문 메타데이터, PDF, SerDes 측정값은 공용이다. 로그인 계정은 `accounts`에
저장하고 즐겨찾기는 `account_favorites`, 추천 제외·검토 상태는
`account_recommendation_feedback`에 `account_id`와 함께 저장한다.
AI 작업은 `ai_jobs.account_id`로 조회·취소를 제한한다. 사용자명 변경은
소유권을 바꾸지 않는다. 추천 SQL 캐시와 배경 작업도 계정별로 분리된다.

권한은 `admin`(공용 자료 및 개인 상태 수정), `member`(자신의 개인 상태 수정),
`viewer`(읽기 전용)이다. 클라이언트가 보낸 계정 ID는 사용하지 않고 서버가
검증한 세션 ID만 사용한다. 비활성화 및 비밀번호 변경 시 기존 세션은 무효화된다.

## 이전 및 새 계정

먼저 `scripts/backup_mysql.ps1 -NoPrune`로 로컬 DB를 백업한다.
계정 분리는 `scripts/migrations/013_accounts.sql`과 전용 마이그레이션으로 적용한다.
다음 예시의 계정명은 실제 설정에 맞게 바꾼다.

```powershell
.venv\Scripts\python.exe scripts\migrate_accounts.py --new-user researcher --copy-from admin
.venv\Scripts\python.exe scripts\migrate_accounts.py --new-user researcher --copy-from admin --apply --admin-defaults C:\path\to\mysql\admin.cnf
```

처음 실행할 때 `.env`의 두 로그인 계정과 기존 비밀번호 해시를 그대로 이전한다.
이후 DB가 인증 정보의 원본이다. 새 계정은 `member`로 생성하고 선택한 계정의
즐겨찾기만 복사한다. 추천 제외 기록과 AI 대화는 복사하지 않는다. 재실행은
이미 존재하는 계정의 비밀번호·즐겨찾기를 덮어쓰지 않는다.

새 계정은 비밀번호 설정 전에는 비활성 상태다. 아래 명령은 화면에 보이지 않게
두 번 입력받아 해시만 저장하고 계정을 활성화한다. 서버 재시작은 필요 없다.

```powershell
.venv\Scripts\python.exe scripts\setup_account_auth.py --username researcher
```

로그인 뒤 화면의 **비밀번호 변경**에서도 본인 비밀번호를 바꿀 수 있다.
새 비밀번호는 8~256자이며 계정명을 포함하지 않는다.

### 다른 계정의 즐겨찾기를 실시간 조회하는 읽기 전용 계정

DB 백업 후 다음 명령으로 사전 확인하고 생성한다. 처음 적용할 때
`014_viewer_favorites_source.sql`을 적용하므로 실행 중인 웹 서버는 생성 후
재시작하여 새 조회 코드를 읽도록 한다.

```powershell
.venv\Scripts\python.exe scripts\create_viewer_account.py --username viewer --source admin
.venv\Scripts\python.exe scripts\create_viewer_account.py --username viewer --source admin --apply --generate-password
```

`viewer`는 `favorites_source_account_id`로 지정한 활성 관리자/일반 계정의
즐겨찾기를 실시간으로 조회한다. 즐겨찾기 행을 복사하지 않으며 원본의 변경은
다음 조회에 반영된다. 이 연결은 즐겨찾기 조회와 그 기반 추천에만 적용한다.
로그인, 비밀번호, 추천 제외 기록, AI 작업 소유권은 자신의 계정으로 유지된다.
즐겨찾기·추천 제외 기록·공용 자료 수정 권한은 부여하지 않는다.
연결이 없는 기존 계정은 자신의 즐겨찾기를 그대로 사용한다.

`--generate-password`는 임시 비밀번호를 한 번 출력하고 첫 로그인에서
비밀번호 변경을 요구한다. 이 옵션을 빼면 비밀번호를 화면에 표시하지 않고
직접 두 번 입력받는다. 비밀번호 평문은 파일이나 DB에 저장하지 않는다.
동일한 이름의 계정이 이미 있으면 변경하지 않고 중단한다.
비밀번호를 나중에 직접 설정하려면 `--defer-password`로 비활성 계정을 만든 뒤
`setup_account_auth.py --username viewer`를 실행한다. 비밀번호 설정 즉시 활성화된다.

## 기존 자동화와 호환

`papers.is_favorite`는 `legacy_owner=1`인 원래 관리자 계정의 호환용 값이다.
관리자의 웹 변경은 이 값에도 반영되고, 기존 유지보수 스크립트가 이 값을
변경하면 MySQL 트리거가 해당 관리자 개인 목록에 반영한다.
다른 계정의 변경은 이 컬럼을 변경하지 않는다. 다운로드 및 Zotero 자동화의
원래 관리자 범위도 그대로 유지된다. 트리거 설치에는 로컬 MySQL 관리자
접속이 필요할 수 있으며 서버 전역 보안 설정은 변경하지 않는다.
인용 수는 공용 서지정보이므로 즐겨찾기 해제로 지우지 않으며, 인용 수 갱신
작업은 모든 계정의 즐겨찾기를 합친 논문 집합을 대상으로 한다.

`ACCOUNT_AUTH_ENABLED` 기본값은 `1`이다. `0`은 이전 `.env` 인증/공유 상태를
검증하는 테스트·복구 전용이며 운영 다중 계정 서비스에서는 사용하지 않는다.
이전 쿠키는 재로그인이 필요하다. DB 백업은 비밀번호 해시도 포함하므로
기존 정책대로 로컬 비공개 위치에 보관한다.

## 검증

```powershell
$env:ACCOUNT_AUTH_ENABLED='0'
.venv\Scripts\python.exe -m unittest discover -s tests
```

`test_account_isolation.py`는 별도 SQLite에서 DB 인증 모드를 켜고 계정 간
읽기·쓰기, 권한, CSRF, 캐시, 비밀번호 변경에 따른 세션 무효화를 검사한다.
배포 전 `verify_account_isolation.py --source admin --member researcher`로 실제
MySQL·HTTP 처리기를 검증할 수 있다. 이 검증은 초기 복사 직후에 사용하며
모든 검증 쓰기를 롤백한다.
