# AI Prompt Game

생성형 AI를 사용해서 제시된 이미지와 비슷한 이미지 생성 후
유사도를 AI에게 판정받아 점수를 매기는 프롬프트 게임입니다.

## 주요 기능

- 닉네임 기반 입장과 기존 세션 재접속
- 서버 메모리 기반 런타임 게임 상태 관리
- 단일 대기실과 매니저 1명 전용 관리 화면
- 드래그 앤 드롭 3팀 편성
- 팀별 메모장, 이미지 생성 3회 제한, 제출, 3라운드 점수 집계
- Gemini API 기반 이미지 생성 및 이미지 판정

## 클라이언트 첫 화면 흐름

- 닉네임 입력 후 기존 세션이 있다면 기존 세션 참여
- 세션이 없으면 새 참가 세션 생성

## 실행

```bash
python -m pip install -r requirements.txt
set GEMINI_API_KEY=your_key_here
python app.py
```

브라우저에서 다음 경로를 엽니다.

- 클라이언트: `http://localhost:5000/`
- 매니저: `http://localhost:5000/manager`

## 설정 파일

- API 설정: `properties.json`

`properties.json`에서 `image_api`, `judge_api`의 모델과 타임아웃을 조정할 수 있습니다.
Gemini API 키는 환경변수 `GEMINI_API_KEY`로 읽습니다.

## 서드파티 라이브러리

- 로컬 환경에 포함된 JavaScript 라이브러리의 라이선스 및 출처: `THIRD_PARTY_NOTICES.md`

## 테스트 스텁 동작

- Gemini API 키가 없으면 이미지 생성은 프롬프트를 반영한 플레이스홀더 이미지로 대체됩니다.
- Gemini API 키가 없으면 최종 판정은 고정 결과로 동작합니다.
- 현재 고정 순위는 `B팀 1위`, `A팀 2위`, `C팀 3위`입니다.
