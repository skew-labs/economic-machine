// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @dev ABI-compatible, immutable coordinator boundary. No coordinator migration/admin mint.
interface SkewVRF {
    struct Request { bytes32 keyHash; uint256 subId; uint16 requestConfirmations;
        uint32 callbackGasLimit; uint32 numWords; bytes extraArgs; }
    function requestRandomWords(Request calldata request) external returns (uint256);
    function getSubscription(uint256 subId) external view returns
        (uint96 balance, uint96 nativeBalance, uint64 reqCount, address owner, address[] memory consumers);
}

contract SkewSolutionToken {
    string public constant name = "Skew Solution";
    string public constant symbol = "SKEW";
    uint8 public constant decimals = 18;
    address public immutable mining;
    uint256 public immutable cap;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    error UnauthorizedIssuer();
    error InvalidAddress();
    error SupplyCap();
    error InsufficientBalance();
    error InsufficientAllowance();

    constructor(uint256 maximum) { mining = msg.sender; cap = maximum; }
    function mint(address to, uint256 amount) external {
        if (msg.sender != mining) revert UnauthorizedIssuer();
        if (to == address(0)) revert InvalidAddress();
        if (amount > cap - totalSupply) revert SupplyCap();
        totalSupply += amount; balanceOf[to] += amount; emit Transfer(address(0), to, amount);
    }
    function approve(address spender, uint256 amount) external returns (bool) {
        if (spender == address(0)) revert InvalidAddress();
        allowance[msg.sender][spender] = amount; emit Approval(msg.sender, spender, amount); return true;
    }
    function transfer(address to, uint256 amount) external returns (bool) {
        _move(msg.sender, to, amount); return true;
    }
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 available = allowance[from][msg.sender];
        if (available < amount) revert InsufficientAllowance();
        if (available != type(uint256).max) {
            allowance[from][msg.sender] = available - amount;
            emit Approval(from, msg.sender, available - amount);
        }
        _move(from, to, amount); return true;
    }
    function _move(address from, address to, uint256 amount) private {
        if (from == address(0) || to == address(0)) revert InvalidAddress();
        if (balanceOf[from] < amount) revert InsufficientBalance();
        balanceOf[from] -= amount; balanceOf[to] += amount; emit Transfer(from, to, amount);
    }
}

/// @notice Mainnet release candidate. Deployment/activation require a reviewed release.
/// @dev Permissionless search/commit/reveal/finalize/claim; paid VRF requests use revocable keepers.
contract SkewSolutionMining {
    uint256 public constant PROBLEMS = 16;
    uint256 public constant NODES = 32;
    uint256 public constant EDGES = 496;
    uint256 public constant REWARD = 1e18;
    uint256 public constant MAX_ROUNDS = 10000;
    uint256 public constant REQUEST_INTERVAL = 30 minutes;
    uint16 public constant FLOOR_BPS = 5200;
    uint16 public constant CEILING_BPS = 6000;
    bytes32 public constant GRAPH_DOMAIN = keccak256("SKEW_MAXCUT_V1");
    SkewVRF public immutable coordinator;
    SkewSolutionToken public immutable rewardToken;
    bytes32 public immutable keyHash;
    uint256 public immutable subscription;
    uint96 public immutable minimumLinkReserve;
    address public governor;
    address public pendingGovernor;
    uint256 public keeperEpoch = 1;
    mapping(address => uint256) public keeperEpochOf;
    bool public admissionPaused = true;
    bool public activated;
    uint256 public nextRound = 1;
    uint256 public activeRound;
    uint16 public nextThreshold = 5500;
    uint8[4] public recentQualified;
    uint256 public historyCount;
    bool private entered;
    struct Round { uint256 requestId; uint256 seed; uint64 requestedAt; uint64 commitEnd;
        uint64 revealEnd; uint16 threshold; uint8 state; }
    struct Best { address miner; uint32 bits; uint32 score; uint256 ordinal; bool claimed; }
    struct Submission { bytes32 commitment; uint256 ordinal; bool revealed; }
    mapping(uint256 => Round) public rounds;
    mapping(uint256 => uint256) private requestRound;
    mapping(uint256 => mapping(uint8 => Best)) public best;
    mapping(uint256 => mapping(uint8 => uint256)) public counts;
    mapping(uint256 => mapping(uint8 => mapping(address => Submission))) public submissions;
    event Requested(uint256 indexed round, uint256 indexed request);
    event Started(uint256 indexed round, uint256 seed, uint16 threshold, uint64 commitEnd, uint64 revealEnd);
    event Committed(uint256 indexed round, uint8 indexed problem, address indexed miner, bytes32 commitment);
    event Revealed(uint256 indexed round, uint8 indexed problem, address indexed miner, uint32 bits, uint32 score);
    event Finalized(uint256 indexed round, uint8 qualified, uint16 nextThreshold);
    event Claimed(uint256 indexed round, uint8 indexed problem, address indexed miner, uint256 amount);
    event AdmissionPaused(bool paused);
    event Activated();
    event GovernorProposed(address indexed candidate);
    event GovernorAccepted(address indexed previous, address indexed current, uint256 keeperEpoch);
    event KeeperSet(address indexed keeper, bool allowed, uint256 epoch);
    event IgnoredFulfillment(uint256 indexed request, uint8 reason);
    modifier onlyGovernor() { require(msg.sender == governor, "ONLY_GOVERNOR"); _; }
    modifier lock() { require(!entered, "REENTRANT"); entered = true; _; entered = false; }

    constructor(address vrf, bytes32 lane, uint256 subId, address owner, uint96 reserve) {
        require(vrf.code.length != 0 && lane != bytes32(0) && subId != 0 && owner != address(0)
            && reserve != 0, "CONFIG_REQUIRED");
        coordinator = SkewVRF(vrf); keyHash = lane; subscription = subId;
        governor = owner; minimumLinkReserve = reserve;
        rewardToken = new SkewSolutionToken(MAX_ROUNDS * PROBLEMS * REWARD);
    }
    function _subscriptionReady() private view {
        (uint96 balance,,,,address[] memory consumers) = coordinator.getSubscription(subscription);
        require(balance >= minimumLinkReserve, "VRF_LINK_RESERVE");
        bool admitted;
        for (uint256 i; i < consumers.length; ++i) if (consumers[i] == address(this)) { admitted = true; break; }
        require(admitted, "VRF_CONSUMER_REQUIRED");
    }
    function activate() external onlyGovernor {
        require(!activated, "ALREADY_ACTIVATED"); _subscriptionReady();
        activated = true; admissionPaused = false; emit Activated(); emit AdmissionPaused(false);
    }
    function pauseAdmission(bool paused) external onlyGovernor {
        require(activated, "ACTIVATE_FIRST");
        if (!paused) _subscriptionReady();
        admissionPaused = paused; emit AdmissionPaused(paused);
    }
    function proposeGovernor(address candidate) external onlyGovernor {
        require(candidate != address(0) && candidate != governor, "NEW_GOVERNOR_REQUIRED");
        pendingGovernor = candidate; emit GovernorProposed(candidate);
    }
    function acceptGovernor() external {
        require(msg.sender == pendingGovernor, "ONLY_PENDING_GOVERNOR");
        address previous = governor; governor = msg.sender; pendingGovernor = address(0);
        // A compromised old governor's keepers do not retain authority after rotation.
        ++keeperEpoch; emit GovernorAccepted(previous, governor, keeperEpoch);
    }
    function setKeeper(address keeper, bool allowed) external onlyGovernor {
        require(keeper != address(0), "KEEPER_REQUIRED");
        keeperEpochOf[keeper] = allowed ? keeperEpoch : 0; emit KeeperSet(keeper, allowed, keeperEpoch);
    }
    function request() external lock returns (uint256 id) {
        require(activated && !admissionPaused && (msg.sender == governor || keeperEpochOf[msg.sender] == keeperEpoch),
            "AUTHORIZED_KEEPER_REQUIRED");
        if (activeRound != 0) {
            Round storage previous = rounds[activeRound];
            require(previous.state == 3, "PREVIOUS_NOT_FINALIZED");
            require(block.timestamp >= previous.requestedAt + REQUEST_INTERVAL, "REQUEST_COOLDOWN");
        }
        require(nextRound <= MAX_ROUNDS, "ROUND_CAP"); _subscriptionReady();
        id = nextRound++; activeRound = id;
        Round storage r = rounds[id]; r.state = 1; r.requestedAt = uint64(block.timestamp); r.threshold = nextThreshold;
        uint256 req = coordinator.requestRandomWords(SkewVRF.Request(keyHash, subscription, 3, 250000, 1,
            abi.encodeWithSelector(bytes4(keccak256("VRF ExtraArgsV1")), false)));
        require(req != 0 && requestRound[req] == 0, "REQUEST_ID_REQUIRED");
        r.requestId = req; requestRound[req] = id; emit Requested(id, req);
    }
    function rawFulfillRandomWords(uint256 req, uint256[] calldata words) external {
        require(msg.sender == address(coordinator), "ONLY_COORDINATOR");
        uint256 id = requestRound[req];
        if (id == 0 || rounds[id].state != 1) { emit IgnoredFulfillment(req, 1); return; }
        if (words.length != 1 || block.timestamp > type(uint64).max - 1200) {
            emit IgnoredFulfillment(req, 2); return;
        }
        // Never cancel, discard a late seed or request a replacement to re-roll.
        // Delayed VRF is an availability incident, not permission to choose another graph.
        Round storage r = rounds[id]; r.seed = words[0]; r.state = 2;
        r.commitEnd = uint64(block.timestamp + 10 minutes); r.revealEnd = uint64(block.timestamp + 20 minutes);
        emit Started(id, r.seed, r.threshold, r.commitEnd, r.revealEnd);
    }
    function commitmentFor(uint256 id, uint8 problem, address miner, uint32 bits, bytes32 salt) public view returns (bytes32) {
        return keccak256(abi.encode(address(this), block.chainid, id, problem, miner, bits, salt));
    }
    function commit(uint256 id, uint8 problem, bytes32 fingerprint) external {
        Round storage r = rounds[id];
        require(!admissionPaused && r.state == 2 && block.timestamp < r.commitEnd, "COMMIT_CLOSED");
        require(problem < PROBLEMS && fingerprint != bytes32(0), "COMMIT_INPUT");
        Submission storage s = submissions[id][problem][msg.sender]; require(s.ordinal == 0, "ONE_COMMITMENT_PER_ADDRESS");
        uint256 ordinal = ++counts[id][problem]; s.commitment = fingerprint; s.ordinal = ordinal;
        emit Committed(id, problem, msg.sender, fingerprint);
    }
    function weight(uint256 seed, uint8 problem, uint256 edge) public pure returns (uint32) {
        require(problem < PROBLEMS && edge < EDGES, "GRAPH_BOUND");
        return uint32(uint256(keccak256(abi.encode(GRAPH_DOMAIN, seed, uint256(problem), edge))) % 1024 + 1);
    }
    function score(uint256 id, uint8 problem, uint32 bits) public view returns (uint32 cut, uint32 total) {
        Round storage r = rounds[id];
        require((r.state == 2 || r.state == 3) && problem < PROBLEMS && (bits & 1) == 0, "CANONICAL_SOLUTION");
        uint256 edge;
        for (uint256 u; u < NODES; ++u) for (uint256 v = u + 1; v < NODES; ++v) {
            uint32 w = weight(r.seed, problem, edge++); total += w;
            if ((((bits >> u) ^ (bits >> v)) & 1) != 0) cut += w;
        }
    }
    function reveal(uint256 id, uint8 problem, uint32 bits, bytes32 salt) external {
        Round storage r = rounds[id];
        require(r.state == 2 && block.timestamp >= r.commitEnd && block.timestamp < r.revealEnd, "REVEAL_CLOSED");
        Submission storage s = submissions[id][problem][msg.sender];
        require(s.ordinal != 0 && !s.revealed && s.commitment == commitmentFor(id, problem, msg.sender, bits, salt),
            "BOUND_COMMITMENT");
        (uint32 cut, uint32 total) = score(id, problem, bits);
        require(uint256(cut) * 10000 >= uint256(total) * r.threshold, "BELOW_THRESHOLD"); s.revealed = true;
        Best storage b = best[id][problem];
        if (b.miner == address(0) || cut > b.score || (cut == b.score && s.ordinal < b.ordinal)) {
            b.miner = msg.sender; b.score = cut; b.bits = bits; b.ordinal = s.ordinal;
        }
        emit Revealed(id, problem, msg.sender, bits, cut);
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
        nextThreshold = next; emit Finalized(id, qualified, next);
    }
    function _claim(uint256 id, uint8 problem) private {
        require(rounds[id].state == 3 && problem < PROBLEMS, "FINALIZED_PROBLEM_REQUIRED");
        Best storage b = best[id][problem]; require(b.miner == msg.sender && !b.claimed, "WINNER_ONLY_ONCE");
        b.claimed = true; rewardToken.mint(msg.sender, REWARD); emit Claimed(id, problem, msg.sender, REWARD);
    }
    function claim(uint256 id, uint8 problem) external lock { _claim(id, problem); }
    function claimMany(uint256[] calldata ids, uint8[] calldata problems) external lock {
        require(ids.length != 0 && ids.length <= 16 && ids.length == problems.length, "CLAIM_BATCH_BOUND");
        for (uint256 i; i < ids.length; ++i) _claim(ids[i], problems[i]);
    }
}
