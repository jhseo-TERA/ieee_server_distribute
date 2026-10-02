# 주간 저널·학회 메타데이터 갱신

## 실행 주체와 일정

2026-09-14 권한 있는 Windows 조회에서 다음 세 예약 작업이 모두 `Ready`이고,
당일 03:00/03:30 실행 기록 및 다음 2026-09-21 예약이 확인됐다.
이전에 미등록으로 보고했던 것은 샌드박스의 `Access denied`를 숨겨서 발생한 오진이다.
작업은 현재 워크폴더의 배치를 실행한다. TaskScheduler Operational 이벤트 로그는
비활성화되어 있어 개별 이벤트 이력은 없다. 예약 작업을 다시 만들 필요는 없다.

| 작업 | 시각 | 담당 출처 |
|---|---|---|
| `IEEE_Paper_Server_WeeklyUpdate` | 월요일 03:00 | IEEE 저널·학회, OJSSC, CICC, ICTA 포함 |
| `IEEE_Paper_Server_NatureWeeklyUpdate` | 월요일 03:00 | NPHOTON, NELECTRON, NCOMMS |
| `IEEE_Paper_Server_OpticaWeeklyUpdate` | 월요일 03:30 | OE, OL, PR, Optica, JLT, OFC |

기존 작업/배치 이름은 유지한다. 배치는 `run_metadata_update.py --pipeline ...`을
호출한다. `StartWhenAvailable`, `IgnoreNew` 설정이 있으며, Interactive 로그온이므로
해당 사용자가 로그인되어 있어야 한다. Python 실행기는 파이프라인별 MySQL 잠금으로
중복 수집을 거부하고, 적재는 별도의 공통 잠금으로 직렬화한다.

## 대상 관리

유일한 출처 설정은 `config/metadata_sources.json`이다. 이름, 유형, 출판사, ISSN,
IEEE ID, 수집 방법, 주간 담당 작업, 시작/종료 연도, 활성 여부를 여기서 관리한다.
저널·학회를 자동으로 추가하지 않는다. CLI의 알 수 없는 이름/빈 선택은 오류다.

- JSSC/TCAS-I/TCAS-II/TMTT와 IEEE 학회도 공개 Crossref API로 수집한다.
- ICTA/OJSSC/SSCL/PTL/MWTL/SSC-M/CICC는 IEEE 작업만 담당한다.
- JLT는 기존대로 Optica 소속과 DOI 기반 키를 유지한다.
- OE/OL/PR/Optica의 `?doi=10.1364/...` 형식 URL도 수집한다. 아직 URI가 없는
  논문에는 파일명으로 안전한 DOI 기반 키를 부여하고, 이후 URI가 생겨도 기존
  DOI 매칭으로 키와 즐겨찾기/PDF 연결을 유지한다. 동일 DOI에 기존 키가 여러 개면
  임의 병합하지 않고 검토가 필요한 오류로 보고한다.
- VLSI-Circuits는 2021년까지, 2022년 이후 통합 학회는 기존 `VLSI-Tech`로 저장한다.
  2025년 `10.23919`, 2026년 `10.1109` DOI 접두사를 모두 허용한다.
- ESSCIRC는 2023년까지이며, 기존 DB에 있는 후속 출처 `ESSERC`를 2024년부터 수집한다.
- MWCL은 2022년까지, MWTL은 2023년부터다. 요청 기간과 발행 기간이 겹치지 않는
  출처는 `outside_publication_range`로 기록한다.
- DesignCon은 기존 수동 적재 대상으로 명시한다. 오래된 세션명, `CIRC`,
  `IEEE Archive` 등 미등록 DB 출처는 보고서의 `coverage`에 남긴다.
  논문별 확인 없이 출처명을 일괄 변경하지 않는다.

## 검증과 적재 정책

기본은 현재 연도와 전년도다. 학회명 후보를 찾은 뒤 정확한 `container-title`로
모든 페이지를 읽고 DOI, 학회명, 연도를 다시 검사한다. 쉼표가 있는 ICTA는
고유 약칭을 이용한 검색 결과 전체를 읽고 동일 검증을 적용한다. 후보 집합이
10,000건을 넘으면 범위가 불명확하므로 실패한다. 공개 API 요청은 순차 실행하며
429의 `Retry-After`를 준수한다.

실행 결과와 파일은 다음 위치에 저장한다.

- `logs/metadata_updates/<run-id>.json`: 출처별 상태, 건수, 연도별 건수,
  기존 DB 대비 건수, 과거 평균, 실패 이유, 파일 해시, PID/부모 PID/실행 사용자.
- `logs/metadata_updates/latest_<pipeline>.json`: 가장 최근 성공/실패 상태.
- `py_01_data/00_metadata/runs/<run-id>/<source>.xlsx`: 이번 실행의 출처별 결과.
- 같은 실행 폴더의 `before_import.json`: 적재 전에 조회한 기존 대상 레코드 백업.

각 출처의 전체 0건, API/페이지 수집 실패, 반복 페이지, 미완성 페이지네이션,
연도별 건수가 기준의 70% 미만으로 감소하면 실패한다. 기준은 동일 설정·연도 범위로
완료된 최근 5개 실행의 평균과 현재 DB 건수 중 큰 값이다. 수집만 한 실행이나 실패한
실행은 평균에 넣지 않는다. 70% 기준은 설정 파일의 `minimum_count_ratio`로 조정한다.
아직 출판되지 않은 올해 학회가 0건이어도, 기존 올해 자료가 없고 전년도 수집이
정상적이면 허용한다. 출처 전체가 0건인 경우에는 허용하지 않는다.

하나라도 실패하면 해당 파이프라인의 DB 적재 전체를 차단하고 종료 코드 1을 반환한다.
실패 자료와 상세 보고서는 보존한다. 모든 출처가 검증되면 **이번 실행의 파일만**
적재하며, 기존 폴더의 Excel 전체를 다시 읽지 않는다. 과거 자료 복원용
`import_excel_to_db.py` 무인수 실행은 기존 최상위 폴더를 읽는 수동 기능으로 남아 있다.
`--files`로 정확한 파일을 지정할 수도 있다.

업서트는 DOI를 저장하고 즐겨찾기·기존 PDF 경로/보유 상태를 보존한다.
모든 키가 적재되었는지 커밋 전에 확인하고 오류 시 롤백한다. PDF 다운로드나
Zotero 동기화는 이 메타데이터 루틴에 포함되지 않는다.

## 실행과 점검

```powershell
# 오프라인 의존성/설정 점검. DB와 네트워크를 사용하지 않는다.
scripts\update_ieee_weekly.bat --self-test
scripts\update_nature_weekly.bat --self-test
scripts\update_optica_weekly.bat --self-test

# 실제 수집 + DB 읽기 검증, DB 쓰기는 하지 않는다.
.venv\Scripts\python.exe scripts\run_metadata_update.py --pipeline ieee --collect-only

# 정상 운영 실행: 수집 -> 검증 -> 이번 실행 파일만 업서트
scripts\update_ieee_weekly.bat

# 특정 출처/범위만 실행
.venv\Scripts\python.exe scripts\run_metadata_update.py --pipeline ieee --sources JSSC,ISSCC --start-year 2026 --end-year 2025

# 현재 출처별 DB 건수와 미등록 출처를 점검
.venv\Scripts\python.exe scripts\probe_metadata_update.py --db

# 최신 완료 실행의 파일 해시, DB 키, 기존 즐겨찾기/PDF 보존 확인
.venv\Scripts\python.exe scripts\verify_crossref_import.py

# 권한 오류와 미등록을 구분하는 읽기 전용 Windows 예약 작업 점검
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\inspect_metadata_tasks.ps1
```

주간 수집에서 빠진 예전 연도는 자동 복구되지 않는다. `--full` 또는 명시적인 연도
범위로 별도 복구할 수 있지만, 명칭/DOI 규칙과 Crossref의 과거 수록 범위에 따라
추가 검증이 필요하다. 새 출처를 추가했다는 사실만으로 전체 과거 이력이 보장되지는 않는다.

## 브라우저 실패 진단

2026-09-14 재현에서는 로그인 제출 후 `https://library.yonsei.ac.kr/login`에
머물렀고 필요한 IEEE DOM 요소가 없었다. 이는 로그인 완료를 검증하지 않던
기존 흐름이 실패를 숨길 수 있음을 보여 준다. 새벽 실행에는 화면 증거가 없으므로
그 시점의 실패 원인을 같은 원인이라고 확정할 수는 없다.

레거시 `sweep_ieee_journals.py`와 `sweep_ieee_conferences.py`는 수동 진단용으로
유지하며, 로그인 성공 확인 및 비정상 종료를 추가했다. 실패 시
`logs/metadata_browser/`에 URL(토큰이 있는 쿼리 제외), 제목, 단계, 예외 타입,
DOM 요소 개수, 입력값을 지운 스크린샷과 출처별 요약을 저장한다.

Crossref 사용 방식은 [공식 필터 문서](https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-filters/)와
[페이지네이션 안내](https://www.crossref.org/documentation/retrieve-metadata/rest-api/tips-for-using-the-crossref-rest-api/)를 따른다.
