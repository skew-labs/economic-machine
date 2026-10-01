# Economic Machine Commerce

구매·판매 에이전트가 함께 사용하는 데이터 거래 실행 레이어입니다. 데이터 자체를 판매하거나
모든 데이터의 진위를 보증하는 사업이 아닙니다. GPU·RAM 거래소와 별도 프로젝트입니다.

구매자의 수요와 판매자의 규칙을 같은 형식으로 연결하고, 데이터 버전·최신성·수량·가격·사용 목적·사용권·
응답 기한을 비교합니다. 허용된 거래만 조건 해시로 고정하고, 나머지는 이유 코드와 함께 유보합니다.
일반 매칭·협상·만료 처리에는 LLM 호출이 없습니다.

## 현재 실행되는 것

- 독립적인 구매·판매 클라이언트의 인증된 정책 등록 API.
- 데이터 종류별 인덱스를 이용한 이벤트 매칭. 전체 마켓의 반복 조회는 하지 않습니다.
- 판매 제안 → 수량 할인/최저가 규칙 적용 → 구매 정책 수락의 한 번짜리 제한 협상.
- 사용권·최신성·응답 기한·예산 불일치에 대한 `ESCALATE` 기록.
- 데이터 버전 변경에 따른 이전 합의 무효화, 15초 주기의 합의 만료 타이머.
- 결제 직전 구매자 소유권·조건 해시·만료·현재 데이터 버전을 다시 확인하는 경계.
- API 키·자금·지출 한도·거래 기록을 관리하는 영어 콘솔. 발견·협상·주문은 에이전트 API로 제공합니다.
- 키 발급·회수·만료·권한 검증, 주문 키와 하나의 공유 지출 정책 연결. 비밀 키 원문은 발급 시 한 번만 제공합니다.
- 구매 실행 예제: CSV 정규화 / Arbitrum Sepolia 공식 RPC 상태 조회 → 납품 검증 → 테스트 장부 정산.
- exact EVM EIP-3009 방식의 x402 v2 HTTP 결제 요구를 검사하는 라이브러리.
- 독립적인 ERC-20 예산/에스크로 계약. 원격 PyEVM에서 실제 EVM 바이트코드로 테스트합니다.

## 상태를 구분하는 법

`AGREED`는 조건 합의입니다. 서명·지급·데이터 전달을 뜻하지 않습니다.

`SANDBOX_LEDGER`는 테스트 크레딧 장부입니다. 실제 토큰이 아니며 실제 토큰으로 환산하지 않습니다.
화면의 구매 실행 예제와 양쪽 정책 협상은 별개 경로입니다.

`SIGNATURE_REQUIRED`는 x402 요구가 승인된 실토큰 바인딩과 일치한다는 라이브러리 결과입니다.
서명과 facilitator 호출을 수행하지 않습니다. 이 라이브러리는 아직 마켓의 실결제 어댑터로 연결되지 않았습니다.

`SETTLEMENT_REPORTED`는 x402 서버의 보고입니다. 독립적인 체인 영수증·토큰 이동을 확인하기 전에는
정산 완료로 승격하지 않습니다. 거래 해시가 없다는 보고만으로 결제를 자동 재시도하지 않습니다.

현재 양쪽 정책 마켓의 결제 요청은 `REAL_PAYMENT_NOT_CONFIGURED`로 차단됩니다.
실토큰 상품, 승인된 판매 endpoint·자산·수령인 등록, 지갑 서명, facilitator, 체인 영수증 대조가 아직 없습니다.
Arbitrum 배포도 없습니다. 샘플 공급자의 최신성과 사용권은 선언이며 검증된 데이터 납품이 아닙니다.

## 원격 실행

Mac은 편집·제어만 합니다. 현재 사용자 지정 캐나다 서버의 격리 경로:

`/srv/skew/economic-machine-commerce-20261002`

```sh
# 아래 명령은 원격 디렉터리에서만 실행합니다.
python3 -m venv .venv
.venv/bin/pip install -e '.[verification]'
.venv/bin/python scripts/compile_contracts.py
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check src/machine_commerce tests scripts
.venv/bin/uvicorn machine_commerce.api:app_factory --factory --host 127.0.0.1 --port 4260
```

`deploy/machine-commerce.service`는 격리된 시스템 사용자와 로컬 주소로 서비스를 시작합니다.
SQLite는 `/var/lib/machine-commerce/commerce.sqlite3`에 저장됩니다.
공개 인터넷 서비스용으로 인증·입력 제한·큐·동시성·보존 정책을 완성한 배포가 아닙니다.

```sh
# 서버에서 실제 HTTP API를 호출하는 두 가지 증거 생성
.venv/bin/python scripts/market_agents.py
.venv/bin/python scripts/buyer_agent.py
```

`market_agents.py`는 별도 구매·판매 세션으로 합의·버전 변경·이전 합의 거절·미결제를 확인합니다.
판매 규칙 fixture의 데이터 자체를 구매하지 않습니다.
`buyer_agent.py`는 예제 서비스를 호출하고 납품과 장부 영수증을 확인합니다.
Arbitrum 데이터 예제는 실제 공식 RPC를 사용합니다. 이는 동일 RPC의 일관성 확인이고 독립적인 진실 오라클이 아닙니다.

## 핵심 API

| API | 실행 |
| --- | --- |
| `POST /api/sessions` | 테스트 참여자 생성 또는 브라우저 세션 재개 |
| `GET /api/keys` | 소유자 전용 키 메타데이터 조회; 비밀 키 조회 불가 |
| `POST /api/keys` | 권한·기간·지출 정책이 제한된 에이전트 키 발급 |
| `POST /api/keys/{id}/revoke` | 소유자 전용 회수; 이후 호출 거절 |
| `POST /api/demands` | 구매 수요 등록 및 해당 데이터 종류 매칭 |
| `POST /api/supplies` | 판매 규칙 등록 및 해당 데이터 종류 매칭 |
| `POST /api/supplies/{id}/refresh` | 새로운 버전 이벤트; 이전 합의 무효화 |
| `GET /api/market` | 내 수요·합의·유보 판단 및 공개 공급 조건 |
| `POST /api/matches/{id}/payment-request` | 조건 재검사; 현재 실결제 차단 |
| `POST /api/policies` | 예제 서비스의 예산·단건 한도·허용 목록 |
| `POST /api/orders` | 예제 주문의 원자적 예약·중복 방지 |
| `POST /api/orders/{id}/run` | 납품·검증·테스트 장부 정산 또는 환불 |
| `GET /api/orders/{id}/artifact` | 실제 예제 납품 결과 |
| `GET /api/orders/{id}/receipt` | 테스트 장부 정산 근거 |

브라우저는 소유자 HttpOnly 쿠키를 사용합니다. 에이전트에는 콘솔에서 발급한 제한된 API 키를
`Authorization: Bearer ...`로 제공합니다. 키는 해시로 저장하며 브라우저 로컬 저장소에 보관하지 않습니다.
같은 지출 정책에 연결된 여러 키는 하나의 예산을 공유합니다. 에이전트 키는 키 관리나 한도 증액을 할 수 없습니다.
기존 테스트 세션 토큰은 소유자 권한의 호환 API로 유지합니다. 공개 서비스용 인증은 아직 아닙니다.
증거 파일에 토큰을 기록하지 않습니다. 상세 계약은 [Console & agent access](docs/CONSOLE.md)에 있습니다.

## 다음 수직 연결

1. 하나의 외부 구매 에이전트와 실제 데이터 판매 endpoint를 연결합니다.
2. 사용자 승인으로 실토큰 거래 조건을 고정하고 x402 요구 검사를 결제 클라이언트에 연결합니다.
3. 서명·제출·체인 정산·납품·실패 처리를 거래 단위로 재현합니다. 중복 지출 방지용 영속적 nonce/제출 저널이 필요합니다.
4. 직접 연결 방식과 동일 workload를 비교합니다. 탐색 요청 수, LLM 호출/토큰, 성사율, p50/p95 합의 지연을 측정합니다.

현재 한 번의 HTTP 지연 샘플은 벤치마크나 토큰 절감률의 증명이 아닙니다.
이 구조의 가치는 등록된 규칙 안에서 거래를 반복하고, 전체 탐색과 자유형 협상을 줄이는지로 입증해야 합니다.

출처·설계 경계는 [아키텍처](docs/ARCHITECTURE.md), 기존 코드 재사용 범위는
[추출 기록](docs/EXTRACTION.md)에 기록합니다.
