"""Read-only dual-RPC finalized-state boundary. No signer or broadcast method."""
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak, to_checksum_address
from economic_machine.values import MachineError


def quantity(value):
    if not isinstance(value, str) or not value.startswith('0x') or not 1 <= len(value[2:]) <= 64:
        raise MachineError('SOLUTION_RPC_QUANTITY')
    try:
        result = int(value, 16)
    except ValueError as exc:
        raise MachineError('SOLUTION_RPC_QUANTITY') from exc
    if result < 0 or hex(result) != value.lower():
        raise MachineError('SOLUTION_RPC_QUANTITY')
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MachineError('SOLUTION_RPC_REDIRECT_REJECTED')


class ReadRPC:
    def __init__(self, url, *, allow_local=False):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.username or parsed.password or not parsed.hostname or parsed.fragment
                or (parsed.scheme != 'https' and not (allow_local and parsed.scheme == 'http'
                    and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}))):
            raise MachineError('SOLUTION_RPC_TRANSPORT')
        self.url = url
        self.identity = parsed.hostname.lower()
        self.counter = 0
        self.opener = urllib.request.build_opener(NoRedirect())

    def __call__(self, method, params):
        if method not in {'eth_chainId', 'eth_getBlockByNumber', 'eth_getCode', 'eth_call',
                          'eth_getTransactionReceipt', 'eth_getTransactionByHash'}:
            raise MachineError('SOLUTION_READ_ONLY_RPC')
        self.counter += 1
        body = json.dumps({'jsonrpc': '2.0', 'id': self.counter, 'method': method, 'params': params}).encode()
        request = urllib.request.Request(self.url, data=body, headers={'Content-Type': 'application/json',
            'Accept':'application/json','User-Agent':'SKEW-solution-read/1.0'})
        try:
            with self.opener.open(request, timeout=10) as response:
                data = response.read(131073)
            if len(data) > 131072:
                raise MachineError('SOLUTION_RPC_RESPONSE_BOUND')
            raw = json.loads(data)
            if raw.get('id') != self.counter or raw.get('jsonrpc') != '2.0' or 'error' in raw or 'result' not in raw:
                raise MachineError('SOLUTION_RPC_INVALID_RESPONSE')
            return raw['result']
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            # Endpoints can contain provider secrets; never interpolate the URL or response body.
            raise MachineError('SOLUTION_RPC_UNAVAILABLE') from None


class FinalizedChain:
    def __init__(self, first, second, contract, expected_code_sha256, *, chain_id=421614, working_confirmations=4):
        if first.identity == second.identity:
            raise MachineError('SOLUTION_INDEPENDENT_RPC_HOSTS_REQUIRED')
        if chain_id != 421614 or not isinstance(expected_code_sha256, str) or len(expected_code_sha256) != 64:
            raise MachineError('SOLUTION_CHAIN_CONFIG')
        try:
            bytes.fromhex(expected_code_sha256)
            self.contract = to_checksum_address(contract)
        except ValueError:
            raise MachineError('SOLUTION_CHAIN_CONFIG') from None
        self.first, self.second = first, second
        self.expected_code = expected_code_sha256
        self.chain_id = chain_id
        if type(working_confirmations) is not int or not 2 <= working_confirmations <= 64:
            raise MachineError('SOLUTION_WORKING_CONFIRMATIONS')
        self.working_confirmations = working_confirmations

    def pair(self, method, params):
        left, right = self.first(method, params), self.second(method, params)
        compare_left, compare_right = left, right
        if method == 'eth_getBlockByNumber':
            compare_left, compare_right = self.block(left), self.block(right)
        elif left is not None and right is not None and method in {'eth_getTransactionReceipt','eth_getTransactionByHash'}:
            fields = ['transactionHash','blockHash','blockNumber','status','logs'] if method=='eth_getTransactionReceipt' else ['hash','blockHash','to','from','input','value','chainId']
            if not isinstance(left,dict) or not isinstance(right,dict):
                raise MachineError('SOLUTION_RPC_OBJECT_REQUIRED')
            compare_left={key:left.get(key) for key in fields};compare_right={key:right.get(key) for key in fields}
        if compare_left != compare_right:
            raise MachineError('SOLUTION_RPC_DISAGREEMENT')
        return left

    @staticmethod
    def block(raw):
        if not isinstance(raw, dict):
            raise MachineError('SOLUTION_FINALIZED_BLOCK_REQUIRED')
        number, timestamp = quantity(raw.get('number')), quantity(raw.get('timestamp'))
        fingerprint = raw.get('hash')
        if not isinstance(fingerprint, str) or len(fingerprint) != 66 or not fingerprint.startswith('0x'):
            raise MachineError('SOLUTION_BLOCK_HASH')
        try:
            bytes.fromhex(fingerprint[2:])
        except ValueError:
            raise MachineError('SOLUTION_BLOCK_HASH') from None
        return {'number': number, 'timestamp': timestamp, 'hash': fingerprint.lower()}

    def anchor(self, *, working=False):
        if quantity(self.pair('eth_chainId', [])) != self.chain_id:
            raise MachineError('SOLUTION_WRONG_CHAIN')
        tag = 'latest' if working else 'finalized'
        left = self.block(self.first('eth_getBlockByNumber', [tag, False]))
        right = self.block(self.second('eth_getBlockByNumber', [tag, False]))
        height = min(left['number'], right['number'])
        if working:
            if height < self.working_confirmations:
                raise MachineError('SOLUTION_WORKING_HEIGHT')
            height -= self.working_confirmations
        block = self.block(self.pair('eth_getBlockByNumber', [hex(height), False]))
        if block['number'] != height:
            raise MachineError('SOLUTION_ANCHOR_HEIGHT')
        code = self.pair('eth_getCode', [self.contract, hex(height)])
        try:
            code_bytes = bytes.fromhex(code[2:]) if isinstance(code, str) and code.startswith('0x') else b''
        except ValueError:
            code_bytes = b''
        if not code_bytes or hashlib.sha256(code_bytes).hexdigest() != self.expected_code:
            raise MachineError('SOLUTION_DEPLOYED_CODE_MISMATCH')
        return block

    def check_anchor(self, anchor):
        if self.block(self.pair('eth_getBlockByNumber', [hex(anchor['number']), False])) != anchor:
            raise MachineError('SOLUTION_REORG_DURING_READ')

    def call(self, anchor, signature, argument_types, arguments, output_types, *, target=None):
        data = '0x' + (keccak(text=signature)[:4] + encode(argument_types, arguments)).hex()
        raw = self.pair('eth_call', [{'to': target or self.contract, 'data': data}, hex(anchor['number'])])
        try:
            payload = bytes.fromhex(raw[2:])
            if len(payload) != 32 * len(output_types):
                raise ValueError('ABI shape')
            return decode(output_types, payload)
        except (TypeError, ValueError, DecodingError):
            raise MachineError('SOLUTION_CONTRACT_ABI_RESPONSE') from None

    def snapshot(self, *, round_id=None, finalized=False):
        # Time-sensitive work uses confirmed recent state, explicitly NOT finalized.
        # Receipt effects below still require both providers' finalized anchors.
        anchor = self.anchor(working=not finalized)
        if round_id is None:
            active, = self.call(anchor, 'activeRound()', [], [], ['uint256'])
        else:
            if type(round_id) is not int or not 1 <= round_id <= 10000:
                raise MachineError('SOLUTION_ROUND_BOUND')
            active = round_id
        paused, = self.call(anchor, 'admissionPaused()', [], [], ['bool'])
        result = {'chain': self.chain_id, 'contract': self.contract, 'code_sha256': self.expected_code,
                  'anchor': anchor, 'round': active, 'admission_paused': paused,
                  'assurance': 'DUAL_RPC_FINALIZED' if finalized else 'DUAL_RPC_RECENT_CANONICAL_NOT_FINALIZED'}
        if active:
            if active > 10000:
                raise MachineError('SOLUTION_ROUND_BOUND')
            values = self.call(anchor, 'rounds(uint256)', ['uint256'], [active],
                               ['uint256', 'uint256', 'uint64', 'uint64', 'uint64', 'uint16', 'uint8'])
            result.update(zip(['request', 'seed', 'requested_at', 'commit_end', 'reveal_end', 'threshold', 'state'], values))
            if not 1 <= result['state'] <= 4 or not 5200 <= result['threshold'] <= 8000:
                raise MachineError('SOLUTION_ROUND_STATE')
            if result['state'] in {2, 3} and (result['commit_end'] <= result['requested_at']
                    or result['reveal_end'] != result['commit_end'] + 600):
                raise MachineError('SOLUTION_ROUND_WINDOWS')
        self.check_anchor(anchor)
        return result

    def winner(self, snapshot, problem):
        values=self.call(snapshot['anchor'],'best(uint256,uint8)',['uint256','uint8'],[snapshot['round'],problem],
                         ['address','uint32','uint32','uint256','bool'])
        self.check_anchor(snapshot['anchor'])
        return dict(zip(['miner','bits','score','ordinal','claimed'],values))

    def rewards(self, anchor, miner):
        token,=self.call(anchor,'rewardToken()',[],[],['address'])
        token=to_checksum_address(token)
        code=self.pair('eth_getCode',[token,hex(anchor['number'])])
        if code=='0x':raise MachineError('SOLUTION_REWARD_TOKEN_CODE_REQUIRED')
        issuer,=self.call(anchor,'mining()',[],[],['address'],target=token)
        cap,=self.call(anchor,'cap()',[],[],['uint256'],target=token)
        supply,=self.call(anchor,'totalSupply()',[],[],['uint256'],target=token)
        balance,=self.call(anchor,'balanceOf(address)',['address'],[miner],['uint256'],target=token)
        if issuer.lower()!=self.contract.lower() or cap!=160000*10**18 or supply>cap or balance>supply:
            raise MachineError('SOLUTION_REWARD_INVARIANTS')
        self.check_anchor(anchor)
        return {'token':token,'cap':str(cap),'supply':str(supply),'balance':str(balance)}

    def outcome(self, tx_hash, intent):
        if not isinstance(tx_hash, str) or len(tx_hash) != 66 or not tx_hash.startswith('0x'):
            raise MachineError('SOLUTION_TX_HASH')
        try:
            bytes.fromhex(tx_hash[2:])
        except ValueError:
            raise MachineError('SOLUTION_TX_HASH') from None
        anchor = self.anchor()
        receipt = self.pair('eth_getTransactionReceipt', [tx_hash])
        if receipt is None:
            return {'state': 'UNKNOWN', 'tx_hash': tx_hash}
        tx = self.pair('eth_getTransactionByHash', [tx_hash])
        expected = intent['transaction']
        if (not isinstance(tx, dict) or str(tx.get('to', '')).lower() != expected['to'].lower()
                or str(tx.get('from', '')).lower() != expected['from'].lower()
                or tx.get('input', '').lower() != expected['data'].lower() or quantity(tx.get('value')) != 0
                or (tx.get('chainId') is not None and quantity(tx['chainId']) != self.chain_id)):
            raise MachineError('SOLUTION_TRANSACTION_BINDING')
        height = quantity(receipt.get('blockNumber'))
        receipt_hash = receipt.get('blockHash')
        canonical = self.block(self.pair('eth_getBlockByNumber', [hex(height), False]))
        if (receipt.get('transactionHash', '').lower() != tx_hash.lower()
                or tx.get('hash', '').lower() != tx_hash.lower() or tx.get('blockHash') != receipt_hash):
            raise MachineError('SOLUTION_RECEIPT_BINDING')
        if canonical['hash'] != str(receipt_hash).lower():
            return {'state': 'ORPHANED', 'tx_hash': tx_hash, 'block': height}
        self.check_anchor(anchor)
        status = quantity(receipt.get('status'))
        if status not in {0, 1}:
            raise MachineError('SOLUTION_RECEIPT_STATUS')
        if height > anchor['number']:
            return {'state': 'PENDING_FINALITY' if status else 'PENDING_REVERT', 'tx_hash': tx_hash, 'block': height,
                    'block_hash': canonical['hash']}
        outcome={'state': 'CONFIRMED' if status else 'REVERTED', 'tx_hash': tx_hash,
                 'block': height, 'block_hash': canonical['hash'], 'finalized_anchor': anchor}
        reward=intent.get('expected_reward')
        if status and reward is not None:
            token=reward['token'].lower();owner=expected['from'].lower()
            transfer='0x'+keccak(text='Transfer(address,address,uint256)').hex()
            minted=[log for log in receipt.get('logs',[]) if str(log.get('address','')).lower()==token
                and log.get('topics')==[transfer,'0x'+'0'*64,'0x'+'0'*24+owner[2:]]
                and log.get('data')=='0x'+format(10**18,'064x')]
            claimed='0x'+keccak(text='Claimed(uint256,uint8,address,uint256)').hex()
            claimed_logs=[log for log in receipt.get('logs',[]) if str(log.get('address','')).lower()==self.contract.lower()
                and log.get('topics')==[claimed,'0x'+format(reward['round'],'064x'),
                    '0x'+format(reward['problem'],'064x'),'0x'+'0'*24+owner[2:]]
                and log.get('data')=='0x'+format(10**18,'064x')]
            if len(minted)!=1 or len(claimed_logs)!=1:raise MachineError('SOLUTION_CLAIM_MINT_EVENT_REQUIRED')
            observed=self.rewards(anchor,expected['from'])
            if observed['token'].lower()!=token:raise MachineError('SOLUTION_REWARD_TOKEN_BINDING')
            outcome.update({'minted_reward':str(10**18),'reward_state_at_finalized_anchor':observed})
        return outcome
