// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice ABI subset of Chainlink VRF v2.5. Coordinator verifies the VRF proof, not this consumer.
interface SolutionVRF {
    struct Request { bytes32 keyHash; uint256 subId; uint16 requestConfirmations;
        uint32 callbackGasLimit; uint32 numWords; bytes extraArgs; }
    function requestRandomWords(Request calldata request) external returns (uint256);
}

/// @notice Research-only emission token. No premine, owner mint or reward for API usage.
contract SolutionResearchToken {
    string public constant name = "SKEW Solution Research Token";
    string public constant symbol = "SKEWSIM";
    uint8 public constant decimals = 18;
    address public immutable mining;
    uint256 public immutable cap;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    event Transfer(address indexed from, address indexed to, uint256 amount);
    event Approval(address indexed owner, address indexed spender, uint256 amount);
    constructor(uint256 maximum) { mining = msg.sender; cap = maximum; }
    function mint(address to, uint256 amount) external {
        require(msg.sender == mining && to != address(0) && totalSupply + amount <= cap, "ISSUANCE_BOUND");
        totalSupply += amount; balanceOf[to] += amount; emit Transfer(address(0), to, amount);
    }
    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount; emit Approval(msg.sender, spender, amount); return true;
    }
    function transfer(address to, uint256 amount) external returns (bool) { move(msg.sender,to,amount); return true; }
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        allowance[from][msg.sender] -= amount; move(from,to,amount); return true;
    }
    function move(address from, address to, uint256 amount) private {
        require(to != address(0), "RECIPIENT"); balanceOf[from] -= amount; balanceOf[to] += amount; emit Transfer(from,to,amount);
    }
}

/// @notice Bounded Max-Cut protocol candidate. Not audited or deployed for public issuance.
/// @dev No blockhash/random admin seed fallback. Mock coordinator is test-only assurance.
contract SolutionMining {
    uint256 public constant PROBLEMS = 16;
    uint256 public constant NODES = 32;
    uint256 public constant EDGES = 496;
    uint256 public constant REWARD = 1e18;
    uint256 public constant MAX_ROUNDS = 10000;
    uint16 public constant FLOOR_BPS = 5200;
    uint16 public constant CEILING_BPS = 8000;
    bytes32 public constant GRAPH_DOMAIN = keccak256("SKEW_MAXCUT_V1");
    SolutionVRF public immutable coordinator;
    SolutionResearchToken public immutable rewardToken;
    bytes32 public immutable keyHash;
    uint256 public immutable subscription;
    address public immutable roundOperator;
    bool public admissionPaused;
    uint256 public nextRound = 1;
    uint256 public activeRound;
    uint16 public nextThreshold = 5500;
    uint8[4] public recentQualified;
    uint256 public historyCount;
    mapping(uint256 => bool) private usedSeed;
    bool private entered;

    struct Round {
        uint256 requestId; uint256 seed; uint64 requestedAt; uint64 commitEnd; uint64 revealEnd;
        uint16 threshold; uint8 state; // 1 awaiting randomness; 2 active; 3 finalized; 4 aborted
    }
    struct Best { address miner; uint32 bits; uint32 score; uint256 ordinal; bool claimed; }
    struct Submission { bytes32 commitment; uint256 ordinal; bool revealed; }
    mapping(uint256 => Round) public rounds;
    mapping(uint256 => uint256) private requestRound;
    mapping(uint256 => mapping(uint8 => Best)) public best;
    mapping(uint256 => mapping(uint8 => uint256)) public counts;
    mapping(uint256 => mapping(uint8 => mapping(address => Submission))) public submissions;
    event Requested(uint256 indexed round, uint256 indexed request);
    event Started(uint256 indexed round, uint256 seed, uint16 threshold, uint64 commitEnd, uint64 revealEnd);
    event Aborted(uint256 indexed round);
    event Committed(uint256 indexed round, uint8 indexed problem, address indexed miner, bytes32 commitment);
    event Revealed(uint256 indexed round, uint8 indexed problem, address indexed miner, uint32 bits, uint32 score);
    event Finalized(uint256 indexed round, uint8 qualified, uint16 nextThreshold);
    event Claimed(uint256 indexed round, uint8 indexed problem, address indexed miner, uint256 amount);
    event AdmissionPaused(bool paused);
    modifier lock() { require(!entered, "REENTRANT"); entered = true; _; entered = false; }

    constructor(address vrf, bytes32 lane, uint256 subId) {
        require(vrf.code.length != 0 && lane != bytes32(0) && subId != 0, "VRF_CONFIG_REQUIRED");
        coordinator = SolutionVRF(vrf); keyHash = lane; subscription = subId;
        roundOperator = msg.sender;
        rewardToken = new SolutionResearchToken(MAX_ROUNDS * PROBLEMS * REWARD);
    }
    function request() external lock returns (uint256 id) {
        require(msg.sender == roundOperator && !admissionPaused, "AUTHORIZED_ROUND_OPERATOR");
        if (activeRound != 0) {
            Round storage previous = rounds[activeRound];
            require(previous.state == 3 || previous.state == 4, "PREVIOUS_NOT_TERMINAL");
            require(block.timestamp >= previous.requestedAt + 30 minutes, "REQUEST_COOLDOWN");
        }
        require(nextRound <= MAX_ROUNDS, "ROUND_CAP");
        id = nextRound++; activeRound = id;
        Round storage r = rounds[id]; r.state = 1; r.requestedAt = uint64(block.timestamp); r.threshold = nextThreshold;
        // Chainlink's VRF ExtraArgsV1 tag and LINK payment flag; no native token payment here.
        uint256 req = coordinator.requestRandomWords(SolutionVRF.Request(keyHash,subscription,3,150000,1,
            abi.encodeWithSelector(bytes4(keccak256("VRF ExtraArgsV1")), false)));
        require(req != 0 && requestRound[req] == 0, "REQUEST_ID_REQUIRED");
        r.requestId = req; requestRound[req] = id; emit Requested(id, req);
    }
    /// @notice Stop new rounds/commitments; existing reveal/finalize/claim rights remain available.
    /// @dev This role cannot change seeds, deadlines, scores, rewards or minted supply.
    function pauseAdmission(bool paused) external {
        require(msg.sender == roundOperator, "ONLY_ROUND_OPERATOR");
        admissionPaused = paused; emit AdmissionPaused(paused);
    }
    function rawFulfillRandomWords(uint256 req, uint256[] calldata words) external {
        require(msg.sender == address(coordinator), "ONLY_COORDINATOR");
        uint256 id = requestRound[req]; require(id != 0, "KNOWN_REQUEST");
        Round storage r = rounds[id];
        // Late/repeated deliveries cannot restart a failed round or choose a replacement seed.
        if (r.state != 1) return;
        if (block.timestamp >= r.requestedAt + 1 hours || words.length != 1 || usedSeed[words[0]]) {
            r.state = 4; emit Aborted(id); return;
        }
        r.seed = words[0]; r.state = 2;
        usedSeed[r.seed] = true;
        r.commitEnd = uint64(block.timestamp + 10 minutes); r.revealEnd = uint64(block.timestamp + 20 minutes);
        emit Started(id,r.seed,r.threshold,r.commitEnd,r.revealEnd);
    }
    function abort(uint256 id) external {
        Round storage r = rounds[id]; require(r.state == 1 && block.timestamp >= r.requestedAt + 1 hours, "TIMEOUT_REQUIRED");
        r.state = 4; emit Aborted(id);
    }
    function commitmentFor(uint256 id,uint8 problem,address miner,uint32 bits,bytes32 salt) public view returns (bytes32) {
        return keccak256(abi.encode(address(this),block.chainid,id,problem,miner,bits,salt));
    }
    function commit(uint256 id,uint8 problem,bytes32 fingerprint) external {
        require(!admissionPaused, "ADMISSION_PAUSED");
        Round storage r = rounds[id]; require(r.state == 2 && block.timestamp < r.commitEnd, "COMMIT_CLOSED");
        require(problem < PROBLEMS && fingerprint != bytes32(0), "COMMIT_INPUT");
        // No global first-come admission cap: 64 Sybil wallets cannot exclude miner 65.
        // No enumeration of submitters occurs in scoring, finalization or claiming.
        require(submissions[id][problem][msg.sender].ordinal == 0, "ONE_COMMITMENT_PER_ADDRESS");
        uint256 ordinal = ++counts[id][problem];
        submissions[id][problem][msg.sender] = Submission(fingerprint,ordinal,false);
        emit Committed(id,problem,msg.sender,fingerprint);
    }
    function weight(uint256 seed,uint8 problem,uint256 edge) public pure returns (uint32) {
        require(problem < PROBLEMS && edge < EDGES, "GRAPH_BOUND");
        return uint32(uint256(keccak256(abi.encode(GRAPH_DOMAIN,seed,uint256(problem),edge))) % 1024 + 1);
    }
    function score(uint256 id,uint8 problem,uint32 bits) public view returns (uint32 cut,uint32 total) {
        Round storage r = rounds[id]; require(r.state >= 2 && r.state != 4 && problem < PROBLEMS && (bits & 1) == 0, "CANONICAL_SOLUTION");
        uint256 edge;
        for (uint256 u; u < NODES; ++u) for (uint256 v = u + 1; v < NODES; ++v) {
            uint32 w = weight(r.seed,problem,edge++); total += w;
            if ((((bits >> u) ^ (bits >> v)) & 1) != 0) cut += w;
        }
    }
    function reveal(uint256 id,uint8 problem,uint32 bits,bytes32 salt) external {
        Round storage r = rounds[id]; require(r.state == 2 && block.timestamp >= r.commitEnd && block.timestamp < r.revealEnd, "REVEAL_CLOSED");
        Submission storage s = submissions[id][problem][msg.sender];
        require(s.ordinal != 0 && !s.revealed && s.commitment == commitmentFor(id,problem,msg.sender,bits,salt), "BOUND_COMMITMENT");
        (uint32 cut,uint32 total) = score(id,problem,bits);
        require(uint256(cut)*10000 >= uint256(total)*r.threshold, "BELOW_THRESHOLD"); s.revealed = true;
        Best storage b = best[id][problem];
        if (b.miner == address(0) || cut > b.score || (cut == b.score && s.ordinal < b.ordinal)) {
            b.miner = msg.sender; b.score = cut; b.bits = bits; b.ordinal = s.ordinal;
        }
        emit Revealed(id,problem,msg.sender,bits,cut);
    }
    function finalize(uint256 id) external {
        Round storage r = rounds[id]; require(r.state == 2 && block.timestamp >= r.revealEnd, "FINALIZE_CLOSED");
        r.state = 3; uint8 qualified;
        for (uint8 p; p < PROBLEMS; ++p) if (best[id][p].miner != address(0)) ++qualified;
        recentQualified[historyCount % 4] = qualified; ++historyCount;
        uint256 periods = historyCount < 4 ? historyCount : 4; uint256 sum;
        for (uint256 i; i < periods; ++i) sum += recentQualified[i];
        uint16 next = r.threshold;
        if (sum * 4 >= periods * PROBLEMS * 3 && next < CEILING_BPS) next += 100;
        else if (sum * 4 <= periods * PROBLEMS && next > FLOOR_BPS) next -= 100;
        nextThreshold = next; emit Finalized(id,qualified,next);
    }
    function claim(uint256 id,uint8 problem) external lock {
        require(rounds[id].state == 3 && problem < PROBLEMS, "FINALIZED_PROBLEM_REQUIRED");
        Best storage b = best[id][problem]; require(b.miner == msg.sender && !b.claimed, "WINNER_ONLY_ONCE");
        b.claimed = true; rewardToken.mint(msg.sender,REWARD); emit Claimed(id,problem,msg.sender,REWARD);
    }
}
