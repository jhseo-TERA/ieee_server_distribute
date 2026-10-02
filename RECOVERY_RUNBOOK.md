# IEEE Paper Server 복구 런북

이 절차는 Google Drive ZIP 백업으로 PDF 파일과 신규 MySQL 데이터베이스를 복구할 때
사용한다. 원본 ZIP, 기존 PDF, 기존 DB 행 및 과거 백업을 삭제하지 않는다.

## 안전 원칙

- ZIP과 추출본은 각각 `drive-zip-incoming`, `drive-zip-extracted`에 보존한다.
- 기존 파일과 내용이 다르면 덮어쓰지 않고 작업을 중단한다.
- 실제 PDF가 아닌 항목은 `drive-zip-extracted/_invalid`에 별도 보존하고 DB에서 제외한다.
- DB 변경 전후에 `backup_mysql.ps1 -NoPrune`을 실행한다.
- DesignCon 기본 importer와 JLT migration의 삭제 경로는 복구 과정에서 사용하지 않는다.

## 복구 순서

```powershell
# 1. ZIP 경로·CRC·PDF 헤더 읽기 전용 검증
.venv\Scripts\python.exe scripts\restore_drive_zip_bundle.py

# 2. 긴 경로를 지원하는 무덮어쓰기 추출 및 프로젝트 복사
.venv\Scripts\python.exe scripts\restore_drive_zip_bundle.py --apply --skip-crc

# 3. 모든 프로젝트 PDF의 카탈로그·페이지 구조 검사
.venv\Scripts\python.exe scripts\validate_recovered_pdf_bundle.py

# 4. SQL 변경 전 백업
.\scripts\backup_mysql.ps1 -NoPrune

# 5. 검증 manifest 기반 SQL 계획 확인
.venv\Scripts\python.exe scripts\reconcile_recovered_pdf_bundle.py

# 6. 단일 트랜잭션으로 PDF 경로·즐겨찾기 반영
.venv\Scripts\python.exe scripts\reconcile_recovered_pdf_bundle.py --apply

# 7. DB 플래그와 실제 파일 대조
.venv\Scripts\python.exe scripts\check_favorite_pdfs.py

# 8. SQL 변경 후 백업 및 전체 회귀 테스트
.\scripts\backup_mysql.ps1 -NoPrune
.venv\Scripts\python.exe -m unittest discover -s tests -q
```

## 2026-09-07 복구 기준점

- Drive ZIP: 5개, ZIP CRC 정상 5/5
- ZIP 항목: 3,068개, 압축 해제 기준 8.441 GiB
- 정상 PDF: 3,067개
- HTML이 `.pdf`로 저장된 DesignCon 항목: 1개, 별도 보존
- 동일 논문의 중복 물리 파일: legacy JLT 30개, DesignCon 13개
- 고유 PDF 연동 논문: 3,024개
- DB 전체: 185,544행
- PDF 보유 및 즐겨찾기: 각각 3,024행

복구 세부 결과는 `drive-zip-extracted/RESTORE_MANIFEST_*.json`,
`PDF_VALIDATION_*.json`, `SQL_RECONCILE_*.json`에 기록된다. 이 디렉터리는 Git에서
제외되며 로컬 복구 증적으로만 보존한다.

## SerDes Survey 재구축 (2026-09-07)

복구 전 `serdes_*` 테이블과 `paper_abstracts`는 비어 있었다. 기존 SQL 백업의
복원이 아니라, 복구 논문/PDF와 아래 원본으로부터 데이터를 재생성했다.

- 개인 서베이: Git에서 제외되는 `config/private_sources.json`의 `survey_spreadsheet_id`로 지정
- WLink 2025: https://web.engr.oregonstate.edu/~anandt/linksurvey/data/WLink_survey_2025.xlsx
- XLSX 원본 스냅샷: `outputs/serdes_recovery/source_snapshots/`

### 복구 결과

| 항목 | 결과 |
| --- | ---: |
| IEEE 전수 분류 | 21,572편 |
| SerDes 포함 논문 | 1,178편 (Core 701 / Adjacent 477) |
| 포함 논문 중 PDF·즐겨찾기 | 1,008편 |
| 성능 측정값 보유 논문 | 1,158편 |
| 제목 외 구조화 근거 보유 논문 | 800편 |
| 기본 구조화 성능 API 측정점 | 832개 |
| PDF 초록 저장 / 포함 논문 초록 | 953건 / 684건 |
| 제목 / 초록 기반 측정 레코드 | 1,083개 / 684개 |
| 개인 시트 반영 | 199항목 → 204개 측정 레코드 |
| WLink 참조 측정 레코드 | 212개 (58개 논문 매칭) |
| 필드별 근거 | 7,810개 |
| 동일 구현 자동 묶음 | 3개 (6편) |

학회명·문헌 유형 481행을 정규화했다. 기존 논문 185,544행, PDF 및 즐겨찾기
각 3,024행은 그대로 유지했다. 원본 PDF/ZIP은 삭제·수정하지 않았다.

### 남은 복구 한계와 검토 항목

- 개인 시트 389항목 중 214개 매칭: 199개는 포함, 15개는 기존 분류 규칙에 따라
  제외/추가 검토 대상이다. 나머지 175개는 미매칭 174개와 모호한 매칭 1개이며,
  근거 없이 논문 ID를 생성하거나 강제 연결하지 않았다.
- WLink 미매칭 154개는 별도 참조로 보존했다. 숫자 지표가 있는 153개가 미매칭
  참조 API에 나타나며, 논문이 연결된 차트 모집단과 구분된다.
- 자동 추출값은 `extracted`, 원본 시트 값은 `user_sheet`/`reference_xlsx` 출처로
  구분된다. 로컬 초록은 `local_pdf` provider와 PDF 해시·첫 페이지 링크를 보존한다.
- PDF `9180563`은 폰트 문제로 pypdf 텍스트 추출만 실패했다. 정상 PDF 파일은 보존되어
  있으며, 이 문제를 파일 손상으로 판정하지 않았다.
- 초록 경계가 불명확한 문서는 자동 초록으로 만들지 않았다. PDF 글자 분리로 생긴
  `4 0nm` 같은 수치는 확정값이 아닌 검토 후보로 남긴다.
- 측정값 검토 대기열은 22편 / 23개 항목이다. 과거 수동 판정·메모·API 초록은
  백업이 없어 복원된 것으로 간주하지 않는다.

### 재현 및 검증

`recover_serdes_survey.py --stage survey`는 **빈 SerDes 테이블에서만** 실행된다.
복구 완료 DB에 재실행하지 말고, 이후 추가 수집은 기존 개별 파이프라인을 사용한다.
각 검사 실행의 `outputs/serdes_recovery/<UTC timestamp>_<stage>/inspection.json`에
SQL 변경 전 학회명과 문헌 유형, PDF 해시, 추출 결과를 남긴다.

```powershell
# 새 DB의 최초 재구축에만 사용. 두 XLSX를 tmp/serdes-recovery에 준비한다.
.\scripts\backup_mysql.ps1 -NoPrune
.venv\Scripts\python.exe -X utf8 scripts\recover_serdes_survey.py --stage inspect
# --cache에는 바로 위 실행이 생성한 inspection.json 경로를 지정한다.
.venv\Scripts\python.exe -X utf8 scripts\recover_serdes_survey.py --stage metadata --cache <inspection.json> --apply
.venv\Scripts\python.exe -X utf8 scripts\recover_serdes_survey.py --stage survey --apply

# 현재 DB의 읽기 전용 검증. 인증 쿠키는 로컬 메모리와 loopback에서만 사용한다.
.venv\Scripts\python.exe -X utf8 scripts\verify_serdes_recovery.py --local-http
.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -q
.\scripts\backup_mysql.ps1 -NoPrune
.\scripts\backup_source.ps1 -NoPrune
```

복구 중 발견한 PyMySQL의 `LIKE 'all%'` 파라미터 처리 오류와 PDF 숫자 분리 오류를
수정하고 회귀 테스트를 추가했다. 소스 백업에는 루트의 SerDes Python 모듈도 포함한다.
이번 실행의 상세 결과:

- `outputs/serdes_recovery/user_sheet_applied_20260907.json`
- `outputs/serdes_recovery/review_20260907/recovery_review.json`
- `outputs/serdes_recovery/fom_audit_20260907/serdes_fom_regeneration_dry_run.json`
- `outputs/serdes_recovery/verification_*.json`

로컬 로그인 세션으로 화면 HTML·목록·차트 API·근거·PDF 헤더 검증을 통과했다.
외부 `/health`는 200 응답을 확인했다. 외부 로그인 검증은 인증 쿠키 전송에 대한
자동 보안 검토에서 차단되어 수행하지 않았으며, 해당 검증에는 추가 승인이 필요하다.
