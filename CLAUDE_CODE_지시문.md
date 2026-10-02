# IEEE Paper Repository 웹사이트 구축 지시문

## 프로젝트 목표

내 PC에 수집해둔 IEEE 논문 메타데이터(Excel)와 PDF 파일들을 MySQL에 넣고,
Flask 웹서버로 검색·필터·PDF 뷰어가 가능한 웹사이트를 만든 뒤,
ngrok으로 외부에서도 접속 가능하게 하는 것.

UI는 다크 레드(#6B1E2E) 헤더에 표 형태로, 검색창·연도/저널 필터·PDF 버튼이 있는
레퍼지토리 형태를 원함.

---

## 현재 내 환경 (먼저 파악해줘)

작업을 시작하기 전에 다음을 먼저 확인해줘:

1. 이 폴더의 전체 구조를 파악해줘 (기존 Selenium 수집 코드, Excel 저장 위치 등)
2. `py_01_data/00_metadata/` 안에 있는 Excel 파일의 컬럼 구조를 읽어서 알려줘
   - 저널 수집 파일과 학회 수집 파일의 컬럼이 다를 수 있음
   - 저널 파일: Journal, Year, Issue, Title, Authors, URL
   - 학회 파일: Conference, Year, Page, Title, Authors, URL
3. venv가 어디 있는지, 어떤 패키지가 이미 설치돼 있는지 확인해줘 (`pip list`)
4. PDF 파일이 저장된 폴더가 있는지 확인해줘 (`pdfs/` 등)

파악이 끝나면, 아래 작업을 진행하기 전에 내 환경에 맞게 경로를 조정해줘.

---

## 만들어야 할 것

### 1. 폴더 구조

```
프로젝트루트/
├── pdfs/                        # PDF 저장 폴더 (article_number.pdf 형식)
├── py_01_data/00_metadata/      # 기존 Excel (이미 존재)
├── web/
│   ├── app.py                   # Flask 서버
│   └── templates/
│       └── index.html           # UI
├── scripts/
│   ├── import_excel_to_db.py    # Excel → MySQL
│   └── schema.sql               # DB 스키마
└── .env                         # DB 비밀번호 등 (기존 것 활용)
```

### 2. MySQL 스키마 (`scripts/schema.sql`)

```sql
CREATE DATABASE IF NOT EXISTS ieee_repo CHARACTER SET utf8mb4;
USE ieee_repo;

CREATE TABLE IF NOT EXISTS papers (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    article_number  VARCHAR(20) UNIQUE,
    title           TEXT NOT NULL,
    authors         TEXT,
    year            VARCHAR(10),
    source_name     VARCHAR(50),   -- JSSC / ISSCC 등
    source_type     VARCHAR(10),   -- journal / conference
    issue           VARCHAR(50),
    url             VARCHAR(300),
    pdf_local_path  VARCHAR(300),
    pdf_available   BOOLEAN DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FULLTEXT idx_search (title, authors)
);
```

### 3. Excel → MySQL import 스크립트 (`scripts/import_excel_to_db.py`)

요구사항:
- `py_01_data/00_metadata/` 안의 모든 `.xlsx` 파일을 자동으로 읽어서 합칠 것
- URL에서 정규식 `/document/(\d+)` 으로 article_number 추출
- 저널/학회 파일 컬럼 차이를 자동 판별 (Journal 컬럼 유무로)
- `pdfs/` 폴더에 해당 PDF가 실제로 존재하는지 확인해서 `pdf_available` 세팅
- `ON DUPLICATE KEY UPDATE` 로 중복 실행해도 안전하게
- DB 비밀번호는 `.env`에서 읽을 것

### 4. Flask 서버 (`web/app.py`)

필요한 엔드포인트:
- `GET /` — index.html 렌더링
- `GET /api/papers` — 논문 목록 (검색/필터/페이징)
  - 쿼리 파라미터: `q`(제목+저자 FULLTEXT 검색), `year`, `source`, `type`, `pdf_only`, `page`, `size`
- `GET /api/meta` — 필터용 메타정보 (연도 목록, 저널 목록, 전체 개수, PDF 보유 개수)
- `GET /pdf/<article_number>` — PDF 파일 서빙 (article_number는 숫자만 허용, 보안)

주의사항:
- `app.run(host="0.0.0.0", port=5000)` 로 바인딩 (ngrok이 잡을 수 있게)
- flask-cors 적용
- SQLAlchemy 사용
- DB 접속정보는 `.env`에서 읽을 것

### 5. UI (`web/templates/index.html`)

레이아웃 (첨부 이미지 참고):
- 상단: 다크 레드(#6B1E2E) 헤더 + "IEEE Paper Repository" 타이틀
  + 전체/PDF보유/기간 통계 뱃지
- 필터 바: 검색 입력창, 연도 셀렉트, 저널·학회 셀렉트, 구분 셀렉트,
  "PDF만 보기" 체크박스, 검색/초기화 버튼
- 테이블: No / 제목 / 저자 / 연도 / 출처(뱃지) / 권호 / 원문(Xplore·PDF 버튼)
  - 헤더는 더 진한 레드(#4A1520), sticky
  - 행 hover 효과
- PDF 버튼 클릭 시 오른쪽에 사이드 패널로 PDF를 iframe으로 인라인 표시
- 하단: 페이지네이션

기능:
- 검색창은 debounce (약 380ms) 적용
- 필터 변경 시 자동 재검색
- 페이지 로드 시 `/api/meta` 먼저 호출해서 필터 옵션 채우기

### 6. 실행 및 외부 공개

작업 완료 후 다음을 안내해줘:
1. MySQL에 스키마 적용하는 명령
2. Excel import 실행하는 명령
3. Flask 서버 실행하는 명령
4. ngrok으로 외부 공개하는 명령 (`ngrok http 5001`)

---

## 진행 방식

- 한 번에 다 만들지 말고, 폴더 구조 파악 → 스키마 → import 스크립트 →
  Flask → UI 순서로 단계별로 진행하면서 각 단계마다 테스트해줘
- import 스크립트를 먼저 돌려서 실제 내 Excel 데이터가 DB에 잘 들어가는지
  확인한 다음에 웹서버를 만들어줘
- 에러가 나면 내 실제 데이터 구조에 맞게 코드를 수정해줘
- 완료되면 `http://127.0.0.1:5001` 으로 접속해서 화면이 잘 나오는지
  먼저 로컬 테스트하고, 그 다음에 ngrok을 안내해줘

---

## 참고: 아직 없는 것들

- MySQL과 ngrok은 내가 이미 PC에 설치해뒀어 (root 비밀번호도 설정함)
- PDF 다운로드는 아직 안 했을 수도 있음 — PDF가 없으면 pdf_available=0으로
  두고, 나중에 채우는 걸로 진행해줘
- PDF 다운로드 스크립트도 필요하면 만들어줘 (기존 Selenium 세션의 쿠키를
  requests로 이식해서 stamp.jsp URL로 다운로드하는 방식)
