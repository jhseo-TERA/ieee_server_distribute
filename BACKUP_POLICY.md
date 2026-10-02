# 로컬 백업 정책

이 프로젝트의 백업은 외부 Git 저장소로 전송하지 않고 현재 Windows 사용자 프로필 아래에만 저장한다.

| 시각 | 예약 작업 | 보관 위치 | 보관 기간 |
|---|---|---|---|
| 매일 02:00 | `IEEE_Paper_Server_DailySourceBackup` | `_backups/source_daily/source_*.tar.gz` | 7일 |
| 매일 02:15 | `IEEE_Paper_Server_DailyMySQLBackup` | `_backups/mysql_daily/mysql_*.tar.gz` | 7일 |

소스 백업에는 `scripts`, `web`, `tests`, `py_00_src`와 루트 문서만 포함한다. `.env`, PDF, Excel/CSV, 데이터베이스 파일, 로그, 보고서, 런타임/캐시, Zotero·Obsidian 자료는 제외한다.

MySQL 백업은 `--single-transaction` 논리 덤프이며 SQL 파일, 생성 시각·행 수가 담긴 manifest, SQL SHA-256을 하나의 `tar.gz`로 묶는다. 압축 파일 자체의 SHA-256은 같은 이름의 `.sha256` 파일에 기록한다. 임시 접속 설정 파일은 압축 전에 삭제하며 비밀번호를 로그나 백업에 넣지 않는다.

복구 전에는 `.sha256` 파일로 압축 파일을 검증하고, 별도 데이터베이스에서 SQL 덤프를 시험 복원한 뒤 운영 DB에 적용한다.

## 소스 Git과 Python 환경 복구

비공개 Git 저장소에는 소스와 `requirements.lock.txt`만 올리고 `.venv` 자체는 올리지 않는다. `.venv`는 절대경로와 플랫폼별 바이너리를 포함하므로 다른 위치에서 신뢰성 있게 복원되지 않는다.

Python 3.13 설치가 남아 있는 상태에서 가상환경만 손상됐다면 다음 명령으로 기존 환경을 `.runtime` 아래에 보존한 뒤 새 환경을 만들고 전체 테스트까지 실행한다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\restore_venv.ps1 -Recreate
```
