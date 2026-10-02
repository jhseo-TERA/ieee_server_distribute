# PDF 확보 routine 운영 정책

목표는 차단을 우회하는 것이 아니라, 기관 VPN/프록시에 부하를 주지 않고 허용된
구독 범위 안에서 소량 다운로드를 오래 유지하는 것입니다.

## 기본 실행

IEEE/Nature 로그인에는 워크폴더 `.env`의 `YONSEI_ID`와 `YONSEI_PW`가 필요합니다.
비밀번호에 `#`, 공백 등이 있으면 dotenv 문법에 맞게 따옴표로 감싸세요.
값은 대화나 로그에 출력하지 않습니다. 로그인 폼은 연세대의 HTTPS 호스트에서만
입력하고, 화면에 보이는 입력란과 버튼을 사용한 뒤 실제 출판사 호스트 도착을 검증합니다.
로그인 실패 증거는 `logs/pdf_download_state/login_diagnostics/`에 저장하며
URL 쿼리와 입력값을 제거합니다. 준비 단계 실패는 비정상 종료하고 논문 시도 예산을 반환합니다.

```bat
.venv\Scripts\python.exe scripts\run_pdf_download_routine.py
```

예약 실행의 공통 진입점은 다음 파일입니다.

```bat
scripts\run_pdf_download_daily.bat
```

Windows 작업 스케줄러에는 다음 명령으로 매일 09:00 Optica 13건 작업과
19:00 오후 통합 작업을 등록하거나 동일 설정으로 재등록합니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_pdf_download_task.ps1
```

MySQL을 확인·시작한 뒤 routine을 실행하고 결과를
`logs/pdf_download_daily_YYYYMMDD_HHMMSS.log`에 저장합니다.

- 한 번 실행할 때 미확보 즐겨찾기를 **IEEE → Optica → Nature** 순서로 처리합니다.
- 하루 총 시도 예산은 실행 횟수와 관계없이 누적 최대 **30건**입니다. IEEE 대상이 30건 이상이면 IEEE만
  처리하고, 남는 예산이 있을 때 Optica와 Nature로 넘어갑니다.
- Optica는 별도 하루 누적 상한 **13건**을 적용하며 09:00 작업에서만 처리합니다.
  19:00 통합 작업은 Optica를 시도하지 않고 IEEE/Nature 및 복구 큐만 처리합니다.
- 명시적 승인으로 한도를 일시 상향할 때는 `daily_limit_override.json`의 날짜가 현지 실행일과
  정확히 일치하는 경우에만 최대 **40건**까지 허용합니다. 다음 날에는 파일이 남아 있어도
  자동으로 기본 30건으로 복귀합니다.
- 하위 downloader가 정상 종료하며 성공/실패 건수를 보고하면 실제 출판사 요청만
  예산으로 확정하고 미사용 선예약은 반환합니다. 비정상 종료로 결과를 확인할 수
  없을 때만 안전을 위해 선예약 전체를 유지합니다.
- JLT의 DOI 기반 안정 키는 Optica `uri`가 아닙니다. DB의 실제 URL을 사용하며,
  OPG URL은 Optica 경로로, IEEE Xplore 조기공개 URL은 IEEE 경로로 다운로드합니다.
- 주간 메타데이터 업데이트에서는 PDF를 다운로드하지 않습니다.
- 같은 routine 또는 같은 출판사 downloader의 중복 실행을 막습니다.
- 요청 간 기본 간격은 IEEE/Nature 30~60초, Optica 660~720초입니다. Optica는
  실제 연속 다운로드에서 heavy-usage 제한이 확인되어 논문 사이를 11~12분 띄웁니다.
- IEEE/Nature의 HTTP 차단·속도제한 응답은 기본 12시간(더 긴 Retry-After 우선),
  Optica의 heavy-usage는 48시간 쿨다운 상태를 파일에 저장합니다.
- 일반 실패 3회 연속이면 기본 6시간 중단합니다.
- Optica CAPTCHA는 즉시 중단하고 72시간 쿨다운합니다.

상태 파일은 `logs/pdf_download_state/`에 저장됩니다. 따라서 작업 스케줄러가
프로세스를 새로 시작해도 쿨다운이 유지됩니다.

성공한 신규 PDF가 0건이면 Zotero/Obsidian 후처리를 실행하지 않습니다.
대상 없음은 Python 종료 코드 77, 보류는 76이며 배치는 로그에 구분한 뒤 정상 종료합니다.
준비 단계 또는 전체 다운로드 실패는 비정상 종료합니다. 후처리 직접 실행과 `--resume`도
체크포인트의 `successful_downloads > 0`을 요구합니다. 일부 성공·일부 실패는 성공분을
후처리하고 실패 내역을 상태에 유지합니다.

Zotero 후처리에는 별도로 `.env`의 `ZOTERO_USER_ID`, `ZOTERO_API_KEY`가 필요합니다.
누락 시 PDF 수집 성공과 별개로 Zotero 단계가 실패하며 이후 첨부/Obsidian 단계는 중단합니다.
이 값 역시 대화나 로그에 노출하지 않습니다. Obsidian 반영에는 실제 vault 경로도 확인해야 합니다.

## DesignCon 수동 반입과 파일 점검

DesignCon PDF는 사용자가 확보한 뒤 `DesignCon/`에 반입합니다. 자동 다운로더 대상이 아닙니다.
무결성 검사는 DesignCon도 포함하고 `pdf_local_path`를 기준으로 파일을 연결합니다.
DesignCon 파일을 이동하거나 DB 상태를 바꾸거나 자동 복구 큐에 넣지 않습니다.
손상·누락은 `manual_review`, DB가 참조하지 않는 파일은 `orphans`에 기록합니다.
`Source/Incomplete/`의 기존 불완전 원본은 별도로 표시하며 보존합니다.

```bat
.venv\Scripts\python.exe scripts\scan_pdf_integrity.py --dry-run
.venv\Scripts\python.exe scripts\import_designcon_to_db.py --dry-run
```

첫 명령은 구조·경로·중복 보고서를 생성하고 파일/DB 내용은 변경하지 않습니다.
새 DesignCon 반입분의 메타데이터 적재는 두 번째 명령으로 미리 확인합니다.
일일 무결성 검사와 다운로드는 같은 잠금을 사용해 파일 이동과 다운로드가 겹치지 않게 합니다.

JLT 구형 파일 정리는 보고서에서 동일 SHA-256이 확인된 항목만 대상으로 하며,
실행 직전에 DB 참조와 원본 해시를 재확인합니다. 유지할 DOI 키 PDF는 구조 검증도 수행합니다.
MySQL 미참조만으로 Zotero/Obsidian 링크 안전성이 보장되지 않습니다. 실제 Zotero 첨부 경로와
Obsidian vault 본문에서 구형 파일 경로가 사용되지 않는지 먼저 확인해야 합니다.
외부 라이브러리에 접근할 수 없으면 이동하지 않습니다. 확인 후에만
`--external-references-verified`를 지정해 실제 격리를 허용합니다.

```bat
.venv\Scripts\python.exe scripts\quarantine_jlt_duplicates.py --report logs\pdf_integrity\pdf_integrity_YYYYMMDD_HHMMSS.json
REM 계획 및 외부 참조를 확인한 뒤에만 실행합니다. DesignCon은 이동 대상이 아닙니다.
.venv\Scripts\python.exe scripts\quarantine_jlt_duplicates.py --report logs\pdf_integrity\pdf_integrity_YYYYMMDD_HHMMSS.json --apply --external-references-verified
```

중복본은 `pdf-quarantine/duplicates/<실행시각>/`에 보관하고 `manifest.json`에
원래 경로·유지 경로·해시를 기록합니다. 디스크에서 삭제하지 않으며 기록된 원래 경로로 복원할 수 있습니다.

## 배포 검증 및 예약 확인

```bat
.venv\Scripts\python.exe -m unittest tests.test_pdf_operations tests.test_pdf_download_outcomes tests.test_daily_pipeline tests.test_download_guard tests.test_optica_queue tests.test_optica_diagnostics tests.test_jlt_crossref tests.test_designcon_pdf_path tests.test_zotero_batch_bootstrap -q
.venv\Scripts\python.exe scripts\run_pdf_download_routine.py --provider ieee --limit 1
.venv\Scripts\python.exe scripts\check_favorite_pdfs.py
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_pdf_download_task.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_pdf_integrity_task.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\inspect_pdf_tasks.ps1
```

예약 작업은 Windows에 해당 사용자가 로그인한 상태에서 실행됩니다(`Interactive`).
설치 스크립트가 존재하는 것과 실제 작업 등록은 별개이므로 마지막 명령의 `missing`과
`nextRun`, `arguments`를 확인합니다. 검사 도구는 권한 거부를 미등록으로 해석하지 않습니다.

## Optica 실패 항목 순환

Optica의 차단·속도제한 페이지가 감지되면 기존처럼 출판사 전체 작업을 즉시 중단합니다.
반면 특정 논문의 접근권한·형식 문제는 해당 항목만 1일, 3일, 7일, 14일,
최대 30일 순서로 재시도를 유예합니다. 유예 중인 항목은 대상 조회에서 제외되므로
고정 정렬의 선두에 남은 실패 논문 3건이 매일 전체 큐를 막지 않습니다. 검증된 PDF를
받거나 기존 로컬 PDF를 DB와 다시 맞추면 그 항목의 실패 기록을 제거합니다.

일반 실패가 3회 연속되어 회로 차단기가 작동하거나 CAPTCHA·heavy-usage로 즉시
중단되면 자동 `diagnose-only` 캡처를 한 번 실행합니다. 새 페이지 이동이나 링크 클릭
없이 중단 직후 이미 열린 페이지의
제목, 현재 URL, 접근 제한 표시, PDF 링크 후보만
`logs/pdf_download_state/optica_diagnostics/`에 JSON으로 저장합니다. 세션·토큰 값은
마스킹하고 쿠키와 원문 HTML은 기록하지 않으므로 추가 다운로드 요청은 발생하지 않습니다.

## 권장 스케줄

Optica는 **09:00 최대 13건**만 실행합니다. 19:00 통합 작업과 같은 날의 수동 재실행은
Optica를 시도하지 않습니다. 상태 파일의 하루 누적 예산도 Optica 13건과 전체 30건을
넘지 않도록 제한합니다. 일주일간 차단 없이 운영된 로그를 확인한 뒤에만 빈도나 요청
간격을 조정하세요. 전체·무제한 다운로드 옵션은 지원하지 않습니다.

특정 출판사만 시험하려면 다음처럼 실행합니다.

```bat
.venv\Scripts\python.exe scripts\run_pdf_download_routine.py --provider ieee --limit 1
```

## 다음 개선 순서

1. 이미 보유한 로컬/Zotero 첨부파일 재사용
2. 합법적인 공개 PDF 또는 저자가 제공한 원문 우선
3. 마지막 수단으로 기관 프록시 downloader 실행
4. 실패·차단 상태는 재시도하지 말고 다음 주기로 넘김

자동 IP 전환, CAPTCHA 우회, 브라우저 지문 위장은 운영 정책에 포함하지 않습니다.
