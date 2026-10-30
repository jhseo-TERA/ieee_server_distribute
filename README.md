# Paper Server

IEEE·Optica·Nature·DesignCon 논문 메타데이터를 검색하고, 계정별 즐겨찾기와
PDF 열람, SerDes 조사, 인용 관계, 개인 리뷰를 관리하는 개인용 논문 서버입니다.

**처음 받는 경우에는 `git clone`, 이미 받은 사본을 업데이트할 때는 `git pull`을 사용합니다.**

```powershell
git clone https://github.com/jhseo-TERA/ieee_server_distribute.git
cd ieee_server_distribute
```

## 시작하기

[새 사용자 설치 가이드](docs/operations/INSTALLATION.md)에 다음 내용을 정리했습니다.

- Windows에서 Python·MySQL·가상환경 준비
- 개인 `.env`, 비밀번호 해시, 세션키 생성
- 빈 DB의 스키마·계정 초기화와 첫 실행
- 논문 메타데이터·PDF 준비 및 선택 기능별 설정
- 업데이트·백업·문제 해결

저장소에는 **DB 내용, 논문 PDF, 개인 노트, 로그인 정보, API 키가 포함되지 않습니다.**
설치 직후 논문 목록이 비어 있는 것은 정상입니다. 논문과 메타데이터는 사용자가 준비합니다.

기본 실행 환경은 Windows, Python 3.13, MySQL 8.4입니다. Node.js 24는 화면 테스트에
사용합니다. MinerU·PaddleOCR·GROBID·Ollama 등은 선택 기능이며 별도 설치합니다.
PowerShell·예약 작업·Acrobat 연동을 포함한 전체 운영 절차는 Windows를 기준으로 합니다.

## 테스트

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.venv\Scripts\python.exe scripts/run_tests.py
```

Node.js 없이 Python만 검사하려면 마지막 명령에 `--python-only`를 붙입니다.
테스트 실행기는 설정·논문 데이터를 제외한 임시 복사본과 일회성 설정을 사용합니다.
실제 MySQL·출판사·Zotero 접속은 단위 테스트에 필요하지 않습니다.
실제 MySQL 연결과 외부 프로그램을 요구하는 통합 검사는 기본 실행에서 제외됩니다.

## 문서와 라이선스

- [전체 문서 목록](docs/README.md)
- [서버 운영 가이드](README_SERVER.md)
- [설정과 개인정보 분리](docs/operations/SOURCE_PRIVACY.md)
- [MIT License](LICENSE.md)

외부 라이브러리는 각 라이브러리의 라이선스를 따릅니다.
이 프로젝트의 MIT 라이선스가 논문 PDF나 외부 데이터의 이용 권한을 부여하지는 않습니다.
