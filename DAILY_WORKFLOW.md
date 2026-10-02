# 일일 PDF·Zotero 작업 흐름

## Windows 작업 스케줄러

아래 표는 작업별 설정 시각입니다. PDF 작업의 실제 등록·최근 실행 상태는
`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\inspect_pdf_tasks.ps1`로 확인합니다.

| 시각 | 작업 이름 | 실행 파일 | 역할 |
|---|---|---|---|
| 매주 월요일 03:00 | `IEEE_Paper_Server_WeeklyUpdate` | `scripts/update_ieee_weekly.bat` | IEEE 저널·학회 Crossref 갱신 (OJSSC·ICTA 포함) |
| 매주 월요일 03:00 | `IEEE_Paper_Server_NatureWeeklyUpdate` | `scripts/update_nature_weekly.bat` | Nature 3개 저널 메타데이터 갱신 |
| 매주 월요일 03:30 | `IEEE_Paper_Server_OpticaWeeklyUpdate` | `scripts/update_optica_weekly.bat` | Optica 5개 저널·OFC Crossref 갱신 |
| 매일 09:00 | `IEEE_Paper_Server_OpticaMorningPDFDownload` | `scripts/run_pdf_download_daily.bat --provider optica --limit 13` | Optica 오전 최대 13건 다운로드 |
| 매일 15:30 | `IEEE_Paper_Server_DailyPDFIntegrity` | `scripts/run_pdf_integrity_daily.bat` | 관리 PDF 전수 구조 검사 |
| 매일 19:00 | `IEEE_Paper_Server_DailyPDFDownload` | `scripts/run_pdf_download_daily.bat` | 손상 복구 우선, IEEE/Nature 처리·Optica 비활성화·전체 합계 최대 30건 다운로드 |

Codex의 `Daily PDF acquisition report` 자동화는 매일 22:00에 로그와 상태만
읽어 보고한다. 다운로드 실행은 Windows의 09:00·19:00 작업만 담당하며, Codex 자동화는
배치 파일을 실행하거나 실패 작업을 재시도하지 않는다.

무결성 검사는 손상 파일을 삭제하지 않고 `pdf-quarantine/날짜/출판사/`로
이동한다. 해당 DB 레코드를 `pdf_available=0`으로 되돌리고
`logs/pdf_download_state/repair_queue.json`에 넣는다. 19:00 다운로드는 이
복구 큐를 일반 미보유 즐겨찾기보다 먼저 처리하되, 복구와 신규 다운로드를 합쳐
하루 최대 30건만 시도한다.

각 다운로드는 `.part` 임시 파일로 저장한 뒤 `pypdf`로 문서 카탈로그와 모든
페이지 참조를 검사한다. 검사를 통과한 경우에만 최종 `.pdf`로 교체하고 DB 보유
플래그와 복구 큐를 갱신한다.

15:30 검사 로그는 `logs/pdf_integrity_daily_*.log`, 상세 JSON 보고서는
`logs/pdf_integrity/pdf_integrity_*.json`, 19:00 작업 로그는
`logs/pdf_download_daily_*.log`에 저장된다.

DesignCon은 사용자가 PDF를 수동 반입하는 출처다. 전수 검사에는 포함하지만
파일 이동·DB 상태 변경·자동 재다운로드는 하지 않고 손상/누락을 수동 확인 목록에 남긴다.
모든 출처의 미참조 파일과 해시가 같은 중복본도 보고한다. JLT 중복본 정리와 복원 절차는
[PDF_DOWNLOAD_ROUTINE.md](PDF_DOWNLOAD_ROUTINE.md)를 따른다.

## Zotero 후속 작업의 정확한 범위

09:00/19:00 PDF 작업에서 검증된 신규 PDF가 하나라도 확보되면, 뒤 항목의 실패 내역은 상태와
보고서에 남기면서 다음 후처리를 실행한다. 성공 PDF가 0건이면 대상 없음·보류·실패 모두
세 단계를 실행하지 않는다. `--resume`도 실제 확보 건수를 확인한다.

1. `scripts/sync_zotero_favorites.bat`: DB 즐겨찾기 중 `pdf_available=1`이고
   로컬 실파일이 존재하는 항목만 `IEEE•Optica•Nature` 컬렉션에 동기화한다.
   JLT는 DOI 기반 안정 키를 식별자로 유지하되, Zotero URL에는 DB에 저장된 실제
   OPG 또는 IEEE Xplore 출판사 주소를 사용하고 기존 관리 항목의 잘못된 URL도 교정한다.
2. `scripts/sync_zotero_pdf_attachments.bat`: Zotero PDF 자식 첨부가 없는 항목에
   Paper Server 원본의 절대 경로를 `linked_file`로 연결한다. Zotero File
   Storage 용량은 사용하지 않으며 다른 장치로 PDF 파일 자체가 동기화되지는 않는다.
3. `scripts/zotero_tag_ingest.bat`: Zotero에서 `📌` 태그가 붙은 항목의 메타데이터,
   PDF 첨부 링크, 노트·하이라이트를 읽어 Obsidian 마크다운을 갱신한다.

태그 ingest는 Zotero Local API를 우선 사용하고, 데스크톱 앱이 꺼져 있으면
`.env`의 `ZOTERO_USER_ID`와 `ZOTERO_API_KEY`로 Web API를 읽기 전용 사용한다.

## 주간 메타데이터 작업과 PDF 다운로드의 구분

세 주간 배치는 `scripts/run_metadata_update.py`를 통해 Crossref 메타데이터만 수집한다.
출처 설정은 `config/metadata_sources.json`에 있으며, 이번 실행에서 생성하고 검증한
파일만 MySQL에 업서트한다. 실패·전체 0건·연도별 건수 급감은 적재를 차단하고
비정상 종료한다. 상세 상태는 `logs/metadata_updates/latest_<pipeline>.json`에 남는다.
예약 작업 권한 재검증, 출처별 담당, 검증 및 복구 절차는 [METADATA_UPDATE.md](METADATA_UPDATE.md)를 따른다.
