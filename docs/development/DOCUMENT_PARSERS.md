# PaddleOCR / MinerU 운영 안내

> 실행 명령과 코드·데이터 경로는 프로젝트 루트 기준입니다.

2026-10-06에 Windows, Python 3.12, CPU 환경으로 설치·검증했습니다.
서버는 기존 `.venv`를 계속 사용하고, 모델 추론은 `.runtime/document-parsers`의 별도 프로세스에서 실행합니다.
웹 요청은 저장된 결과만 읽습니다.

## 설치된 구성

| 용도 | 패키지 / 모델 | 원본 |
| --- | --- | --- |
| 블록도 영역 탐지 | PaddleOCR 3.7.0, PaddlePaddle 3.3.1, PP-DocLayout-L | https://github.com/PaddlePaddle/PaddleOCR |
| 논문 구조 및 성능 근거 추출 | MinerU 4.0.10, Basic ONNX 모델, docvortex 0.5.9 | https://github.com/opendatalab/MinerU |

공식 Python 배포 패키지를 작업폴더 안에 설치했습니다. 전이 의존성은
`scripts/requirements_document_parsers.lock.txt`에 고정되어 있습니다.
모델은 `.runtime/document-parsers/models/`에 보관합니다. 모델 다운로드 이후 일반 추출은 로컬에서 처리하며 PDF를 외부 서비스에 보내지 않습니다.

## 상태 확인과 재설치

작업폴더에서 PowerShell로 실행합니다.

```powershell
.runtime/document-parsers/Scripts/python.exe scripts/document_parser_status.py
.runtime/document-parsers/Scripts/python.exe -m pip check
```

다른 PC에서는 Python 3.12 실행 파일을 지정합니다. 기존 서버 가상환경에 모델 패키지를 설치하지 마세요.

```powershell
./scripts/setup_document_parsers.ps1 -BasePython 'C:/path/to/Python312/python.exe' -DownloadModels -SamplePdf 'ieee-pdf/11509585.pdf'
```

패키지는 준비됐지만 모델만 없는 경우:

```powershell
.runtime/document-parsers/Scripts/python.exe scripts/paddle_layout_worker.py --pdf ieee-pdf/11509585.pdf --output outputs/document_parsers/paddle_setup.json --max-pages 1 --download-model
.runtime/document-parsers/Scripts/python.exe scripts/mineru_worker.py --download-models
```

다운로드는 이 명시적 준비 명령에서만 실행합니다. 상태 확인만으로 추론 성공을 보장하지는 않습니다.

## PaddleOCR 블록도 재생성

```powershell
.venv/Scripts/python.exe scripts/prepare_serdes_figures.py --article 9830507 --layout paddle --cache-namespace staging-paddle-review --retry
```

- `auto`: 준비된 로컬 모델이 있으면 보강하고, 없거나 실패하면 기존 추출로 진행합니다.
- `paddle`: 수동 검토 결과가 없는 논문은 Paddle 추론 성공이 필요합니다. 실패하면 오류를 표시합니다.
- `off`: 기존 PDF 객체·캡션 기반 추출만 사용합니다.
- `--cache-namespace`: 비교용 결과를 별도 보관합니다. 생략하면 현재 서비스 캐시에 반영됩니다.
- `--article`은 반복해서 지정할 수 있습니다. 생략하면 기존 SerDes 대상 전체를 처리하므로 시간과 범위를 고려하세요.

기존에 원문 검토한 그림·그림 부재 판정은 모델보다 우선합니다. 새 모델 후보는 `review_required`로 보관하며,
페이지·캡션·범위·잘림을 검토한 뒤 기존 `config/serdes_figure_overrides.json` 절차로 확정합니다.
`--retry`도 수동 확정 결과를 무시하지 않습니다.

모델은 그림 범위를 찾고, 실제 이미지는 원본 PDF를 렌더링해서 만듭니다. 회로 연결을 상상해 다시 그리는 기능은 아닙니다.
캡션과 연결되지 않거나 페이지 전체를 그림으로 감지한 경우에는 기존 객체 기반 추출을 사용할 수 있습니다.

## MinerU 성능 근거 추출

`--extract-mineru`에는 논문 번호(article_number)가 아니라 데이터베이스의 `papers.id`를 지정합니다.

```powershell
# DB 성능점은 추가하지 않고 근거 화면용 캐시만 생성
.venv/Scripts/python.exe scripts/serdes_data_pipeline.py --extract-mineru 14 --mineru-cache-only

# 분리된 검토 후보와 필드별 근거를 DB에 추가
.venv/Scripts/python.exe scripts/serdes_data_pipeline.py --extract-mineru 14
```

기본은 `basic`, 처음 30페이지, 최대 900초입니다.
`--mineru-max-pages`, `--mineru-timeout`, `--mineru-force`로 범위와 재분석을 지정할 수 있습니다.
`--mineru-tier flash`는 가벼운 대안이며, 표·레이아웃을 추가 검토해야 한다는 표시를 남깁니다.

결과는 `outputs/serdes_mineru/<PDF SHA256>/<설정 해시>/`에 저장됩니다.

- `evidence.json`: 수치 후보, 페이지, 좌표, 측정 조건, 값 차이, 검토 경고
- `parsed/structured_content.json`, `middle_json.json`, `markdown.md`, `images/`: 원본 파싱 결과
- `parser.log`: 추론 기록과 오류

SerDes 목록에서 **근거 확인 / 근거 N**을 누르면 기존 근거와 MinerU 후보를 함께 볼 수 있습니다.
`needs_review` 후보는 기존 확정값을 덮어쓰지 않고 성능 차트에도 포함되지 않습니다.
동일 결과 재실행 시 후보를 중복 추가하지 않으며 이미 검토한 후보 상태도 유지합니다.

## 정확도와 운영 범위

- 숫자가 같아도 동작 조건이 같은지는 별도 판단이 필요합니다. 다른 문장·표 셀의 숫자를 임의로 합치지 않습니다.
- 비교표에서 This Work가 명확하지 않거나 병합·누락 셀로 열 위치가 불명확하면 수치를 자동 귀속하지 않습니다.
- 병합 표는 HTML·이미지·캡션·각주를 보존하고 원문 페이지 링크로 검토합니다.
- 내부/병렬 출력 속도, 모의실험값, 타 논문 인용, 전력 포함 범위는 별도 경고 대상으로 취급합니다.
- PDF가 교체되면 이전 해시의 근거를 재사용하지 않습니다. 모델·추출 설정 변경 시에도 캐시를 구분합니다.
- 모델 결과 자체는 검증 완료 표시가 아닙니다. 확인된 페이지 근거와 조건을 대조한 뒤 기존 검토 워크플로로 확정합니다.

Paddle은 최대 24페이지, MinerU 기본은 30페이지까지만 처리합니다. 전체 논문인지 결과의 페이지 수를 확인하세요.
Paddle 실패 로그는 `outputs/document_parsers/paddle_layout/failures/`에 남습니다.
서버 실행 환경에서 `SERDES_PADDLE_LAYOUT=0`을 지정하면 Paddle 보강을 비활성화할 수 있습니다.
다른 전용 Python을 사용할 경우 `DOCUMENT_PARSER_PYTHON`을 지정합니다.

## 검증 기록

실제 적용 및 테스트 결과는 `PATCH_NOTES_20261006_DOCUMENT_PARSERS.md`를 참고하세요.
