# 기존 Economic Machine의 추출 범위

원본: `/Users/heoun/gwdc-tron-b-planning/src/economic_machine`.
기존 GWDC PR11 기준 commit: `142530c`. 원본 작업 폴더를 수정하거나 이동하지 않았습니다.

동일한 소스로 복사한 파일:

- `values.py`: MachineError, 정확한 Decimal 검증, canonical JSON, digest, 엄격한 키 검사.
- `journal.py`: 이벤트 중복 방지, 입력/출력 해시, 연결된 이벤트 저널, 무결성 확인.

두 파일의 SHA-256과 원본의 현재 파일 일치는 원격 검증 manifest에 기록합니다.
원본 전체 kernel이 그대로 추출됐다는 의미는 아닙니다. TRON 상품/배분/부채/transaction adapter는 가져오지 않았습니다.

`machine_commerce/`는 새 상거래 의미론입니다. 정책·불변조건·상태 전이·영수증의 설계를
구매/판매 에이전트의 거래에 적용합니다. 데이터를 독점 판매하는 주체와 거래 실행 레이어를 구분합니다.

원본의 데이터셋, 수집기, Qwen 서비스, WHOLLET 화면, TRON 키와 고객 지갑 자료를 복사하지 않았습니다.
새 화면과 독립 프로젝트로 만들었으며 GPU·RAM 거래소와도 분리합니다.
