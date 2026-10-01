# 2026-10-02 실행 검증

사용자가 지정한 캐나다 서버의 `/srv/skew/economic-machine-commerce-20261002`에서만
의존성 설치·Solidity 컴파일·Python/EVM 테스트·API 호출을 수행했습니다.
Mac에서는 소스 편집, 작은 파일 확인, 원격 제어와 브라우저 확인만 수행했습니다.

## 검증 결과

- 전체 47개 테스트 통과 후 이벤트 매칭을 개선했습니다.
- 변경된 market 13개와 API 6개를 재검증했습니다. 변경되지 않은 계약을 다시 컴파일하지 않았습니다.
- 중복을 제외한 최종 검증 사례는 48개입니다. 이름과 로그 해시는 `artifacts/verification.json`에 있습니다.
- 정적 검사 통과, JavaScript 구문 검사 통과.
- 서비스 `machine-commerce.service` active. 재시작 뒤 readiness를 확인하고 API 데모를 실행했습니다.

로그: `artifacts/python-tests.log`, `artifacts/market-tests-final.log`, `artifacts/api-tests-final.log`,
`artifacts/lint.log`, `artifacts/contract-build.json`.

## 별도 에이전트 호출

`artifacts/bilateral-agents.json`:

- 독립 판매·구매 세션의 정책 등록과 HTTP 호출.
- 제안 단가 0.05 → 판매 규칙 단가 0.04 → 10개 총액 0.4 합의.
- LLM 호출 0회. 이는 코드 경로에서 호출하지 않았다는 사실이며 경쟁 제품 대비 절감률은 아닙니다.
- 데이터 버전 변경 후 이전 조건으로 결제 요청 시 HTTP 409.
- 결제 요청은 `REAL_PAYMENT_NOT_CONFIGURED`로 차단. 구매자 잔액 10, 지출 0 유지.
- 이 기록의 판매 데이터는 정책 fixture입니다. 실제 데이터 납품이나 데이터 진실 검증의 증거가 아닙니다.
- 기록된 HTTP 지연은 단일 샘플입니다. p95·처리량·24시간 성능을 측정하지 않았습니다.

`artifacts/agent-purchases.json`:

- CSV 정규화 서비스와 실제 Arbitrum Sepolia RPC 상태 조회 서비스 호출.
- 실제 예제 납품과 장부 영수증 2개. 지출 합계 0.16 TEST_CREDIT, 중복 호출은 재지급 없음.
- RPC 결과의 체인 ID·블록 해시·나이를 대조. L1 finality나 독립 오라클 검증은 아닙니다.
- `tx_hash: null`, `settlement: SANDBOX_LEDGER`. 실제 체인 결제가 아닙니다.

## 화면 검증

실행 중인 원격 서버를 SSH로 연결한 `http://127.0.0.1:4260`에서 확인했습니다.

- 구매 수요 등록 → 협상 조건 및 조건 해시 → 미결제 상태 표시.
- 결제 연결 상태 확인 → 실제 서명·지급을 하지 않았다는 결과 표시.
- 별도 구매 실행 예제에서 예산 설정 → 두 서비스 주문 → 납품 및 장부 영수증 표시.
- 브라우저 보유액 9.84, 지급 완료 0.16, 예약 중 0.
- 브라우저 오류/경고 로그 없음.
- 모바일 390px에서 문서 폭 390px. 가로 넘침 없음.

화면 증거는 `artifacts/browser-execution.jpg`, `artifacts/browser-market.jpg`,
`artifacts/browser-mobile.jpg`입니다. 브라우저에서 생성한 증거도 원격 artifacts 디렉터리에 동기화합니다.

## 코드 규모와 재사용

의미 없는 줄 채우기는 하지 않았습니다. 현재 core/contract의 비어 있지 않은 주석 외 줄은 1,396줄,
테스트는 634줄입니다. 문자열과 선언도 포함되는 단순 물리적 집계이며 모든 줄이 독창적이거나
동일한 난도의 구현이라는 뜻은 아닙니다. 전체 파일별 집계는 원격 manifest에 기록합니다.

원본 `values.py`와 `journal.py`의 현재 내용은 복사본과 SHA-256이 일치합니다.
그 밖의 TRON 배분·데이터셋·Qwen·기존 화면을 가져오지 않았습니다.

## 남은 연결

실제 외부 판매 endpoint, 승인된 실토큰 바인딩, 사용자 지갑 서명, x402 facilitator,
영속적인 제출/nonce 저널, 체인 토큰 이동 대조, 납품 분쟁 처리는 미연결입니다.
ERC-20 에스크로는 PyEVM에서 검증했으며 Arbitrum에 배포하지 않았습니다.
공개 고객 서비스나 대규모 시장의 운영 신뢰성을 확인한 결과는 아닙니다.
