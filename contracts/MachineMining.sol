// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

interface MiningToken {
    function balanceOf(address) external view returns (uint256);
}

/// @notice Requester-funded useful-work contest, NOT token issuance or live swap execution.
/// @dev Verifies only a frozen, requester-declared constant-product mathematical model.
contract MachineMining {
    uint256 private constant LIMIT = uint256(uint64(type(int64).max));
    uint256 private constant SCALE = 1_000_000;
    address public immutable token;
    uint256 public nextJob = 1;
    uint256 public liability;
    mapping(address => uint256) public credits;
    bool private entered;

    struct Edge {
        uint64 poolId; uint64 assetIn; uint64 assetOut;
        uint64 reserveIn; uint64 reserveOut; uint32 feePpm; uint64 cost;
    }
    struct Terms {
        uint64 assetIn; uint64 assetOut; uint64 amountIn;
        uint64 minimumNet; uint64 maximumCost; uint16 maximumImpactBps;
        uint8 maximumHops; uint64 commitEnd; uint64 revealEnd;
        uint256 reward; uint256 bond; bytes32 sourceRoot;
    }
    struct Job {
        address requester; Terms terms; bytes32 inputHash;
        Edge[] edges; address[] miners;
        address winner; uint256 bestNet; uint256 bestOrdinal; bool finalized;
    }
    struct Submission {
        bytes32 commitment; uint256 ordinal; bool valid; bytes32 pathHash;
    }
    mapping(uint256 => Job) private jobs;
    mapping(uint256 => mapping(address => Submission)) public submissions;

    event JobCreated(uint256 indexed id, address indexed requester, bytes32 inputHash, uint256 reward);
    event Committed(uint256 indexed id, address indexed miner, bytes32 commitment, uint256 ordinal);
    event Revealed(uint256 indexed id, address indexed miner, bytes32 pathHash, uint256 net);
    event Finalized(uint256 indexed id, address indexed winner, uint256 reward, uint256 net);
    event Withdrawn(address indexed recipient, uint256 amount);

    modifier lock() { require(!entered, "REENTRANT"); entered = true; _; entered = false; }
    constructor(address rewardToken) {
        require(rewardToken.code.length != 0, "TOKEN_REQUIRED"); token = rewardToken;
    }

    function createJob(Terms calldata t, Edge[] calldata edges) external lock returns (uint256 id) {
        require(t.assetIn != 0 && t.assetOut != 0 && t.assetIn != t.assetOut, "ASSETS");
        require(t.amountIn > 0 && t.amountIn <= LIMIT && t.minimumNet <= LIMIT && t.maximumCost <= LIMIT, "BOUNDS");
        require(t.maximumHops > 0 && t.maximumHops <= 4 && t.maximumImpactBps <= 10000, "LIMITS");
        require(edges.length > 0 && edges.length <= 16 && t.sourceRoot != bytes32(0), "SNAPSHOT");
        require(t.commitEnd >= block.timestamp + 60 && t.commitEnd <= block.timestamp + 7 days, "COMMIT_WINDOW");
        require(t.revealEnd >= t.commitEnd + 60 && t.revealEnd <= t.commitEnd + 7 days, "REVEAL_WINDOW");
        require(t.reward > 0 && t.reward <= 1e30 && t.bond > 0 && t.bond <= 1e30, "FUNDING");
        id = nextJob++;
        Job storage j = jobs[id]; j.requester = msg.sender; j.terms = t;
        j.inputHash = keccak256(abi.encode(t, edges));
        for (uint256 i; i < edges.length; ++i) {
            Edge calldata e = edges[i];
            require(e.poolId != 0 && e.assetIn != 0 && e.assetOut != 0 && e.assetIn != e.assetOut, "EDGE_ASSETS");
            require(e.reserveIn > 0 && e.reserveIn <= LIMIT && e.reserveOut > 0 && e.reserveOut <= LIMIT
                && e.feePpm < SCALE && e.cost <= LIMIT, "EDGE_BOUNDS");
            j.edges.push(e);
        }
        receiveExact(msg.sender, t.reward);
        emit JobCreated(id, msg.sender, j.inputHash, t.reward);
    }

    function job(uint256 id) external view returns (address requester, Terms memory terms,
        bytes32 inputHash, address winner, uint256 bestNet, bool finalized, uint256 count) {
        Job storage j = known(id);
        return (j.requester, j.terms, j.inputHash, j.winner, j.bestNet, j.finalized, j.miners.length);
    }
    function edgesOf(uint256 id) external view returns (Edge[] memory) { return known(id).edges; }
    function commitmentFor(uint256 id, address miner, uint8[] calldata path, bytes32 salt) public view returns (bytes32) {
        return keccak256(abi.encode(address(this), block.chainid, id, miner, path, salt));
    }
    function commit(uint256 id, bytes32 commitment) external lock {
        Job storage j = known(id);
        require(block.timestamp < j.terms.commitEnd && !j.finalized, "COMMIT_CLOSED");
        require(commitment != bytes32(0) && submissions[id][msg.sender].ordinal == 0, "ONE_COMMITMENT");
        require(j.miners.length < 64, "SUBMISSION_LIMIT");
        uint256 ordinal = j.miners.length + 1;
        submissions[id][msg.sender] = Submission(commitment, ordinal, false, bytes32(0));
        j.miners.push(msg.sender); receiveExact(msg.sender, j.terms.bond);
        emit Committed(id, msg.sender, commitment, ordinal);
    }
    function reveal(uint256 id, uint8[] calldata path, bytes32 salt) external lock {
        Job storage j = known(id);
        require(block.timestamp >= j.terms.commitEnd && block.timestamp < j.terms.revealEnd && !j.finalized, "REVEAL_CLOSED");
        Submission storage s = submissions[id][msg.sender];
        require(s.ordinal != 0 && !s.valid && s.commitment == commitmentFor(id, msg.sender, path, salt), "COMMITMENT_MISMATCH");
        (uint256 net,,) = score(id, path);
        s.valid = true; s.pathHash = keccak256(abi.encode(path));
        credits[msg.sender] += j.terms.bond;
        // Tie order is commitment order, never reveal order. Duplicate paths earn no additional reward.
        if (j.winner == address(0) || net > j.bestNet || (net == j.bestNet && s.ordinal < j.bestOrdinal)) {
            j.winner = msg.sender; j.bestNet = net; j.bestOrdinal = s.ordinal;
        }
        emit Revealed(id, msg.sender, s.pathHash, net);
    }
    function score(uint256 id, uint8[] calldata path) public view returns (uint256 net, uint256 amount, uint256 cost) {
        Job storage j = known(id); Terms storage t = j.terms;
        require(path.length > 0 && path.length <= t.maximumHops, "PATH_BOUND");
        amount = t.amountIn; uint64 asset = t.assetIn;
        for (uint256 i; i < path.length; ++i) {
            require(path[i] < j.edges.length, "EDGE_INDEX");
            Edge storage e = j.edges[path[i]];
            require(e.assetIn == asset, "DISCONNECTED");
            for (uint256 k; k < i; ++k) require(j.edges[path[k]].poolId != e.poolId, "REPEATED_POOL");
            amount = swap(e, amount, t.maximumImpactBps); asset = e.assetOut;
            cost += e.cost; require(cost <= t.maximumCost && cost <= LIMIT, "COST");
        }
        require(asset == t.assetOut && amount > cost, "TERMINAL");
        net = amount - cost; require(net >= t.minimumNet, "MINIMUM_NET");
    }
    function swap(Edge storage e, uint256 amount, uint256 maxImpact) private view returns (uint256 output) {
        uint256 fee = (amount * e.feePpm + SCALE - 1) / SCALE;
        require(amount > fee && uint256(e.reserveIn) + amount <= LIMIT, "INPUT_OVERFLOW");
        output = uint256(e.reserveOut) * (amount - fee) / (uint256(e.reserveIn) + amount - fee);
        uint256 spot = uint256(e.reserveOut) * amount / e.reserveIn;
        require(output > 0 && output < e.reserveOut && spot > 0 && spot <= LIMIT, "ILLIQUID");
        require((spot - output) * 10000 / spot + (((spot - output) * 10000 % spot == 0) ? 0 : 1) <= maxImpact, "IMPACT");
        require((amount * SCALE + output - 1) / output <= LIMIT, "PRICE_OVERFLOW");
        require((uint256(e.reserveIn) + amount) * (uint256(e.reserveOut) - output)
            >= uint256(e.reserveIn) * e.reserveOut, "POOL_INVARIANT");
    }
    function finalize(uint256 id) external lock {
        Job storage j = known(id); require(block.timestamp >= j.terms.revealEnd && !j.finalized, "FINALIZE_CLOSED");
        j.finalized = true;
        credits[j.winner == address(0) ? j.requester : j.winner] += j.terms.reward;
        for (uint256 i; i < j.miners.length; ++i) {
            if (!submissions[id][j.miners[i]].valid) credits[j.requester] += j.terms.bond;
        }
        emit Finalized(id, j.winner, j.terms.reward, j.bestNet);
    }
    function withdraw() external lock {
        uint256 amount = credits[msg.sender]; require(amount != 0, "NO_CREDIT");
        credits[msg.sender] = 0; liability -= amount;
        uint256 beforeSelf = MiningToken(token).balanceOf(address(this));
        uint256 beforeTo = MiningToken(token).balanceOf(msg.sender);
        tokenCall(abi.encodeWithSignature("transfer(address,uint256)", msg.sender, amount));
        require(MiningToken(token).balanceOf(address(this)) + amount == beforeSelf
            && MiningToken(token).balanceOf(msg.sender) == beforeTo + amount, "EXACT_TOKEN");
        emit Withdrawn(msg.sender, amount);
    }
    function receiveExact(address from, uint256 amount) private {
        uint256 beforeSelf = MiningToken(token).balanceOf(address(this));
        uint256 beforeFrom = MiningToken(token).balanceOf(from);
        tokenCall(abi.encodeWithSignature("transferFrom(address,address,uint256)", from, address(this), amount));
        require(MiningToken(token).balanceOf(address(this)) == beforeSelf + amount
            && MiningToken(token).balanceOf(from) + amount == beforeFrom, "EXACT_TOKEN");
        liability += amount;
    }
    function tokenCall(bytes memory data) private {
        (bool ok, bytes memory out) = token.call(data);
        require(ok && (out.length == 0 || (out.length == 32 && abi.decode(out, (bool)))), "TOKEN_CALL");
    }
    function known(uint256 id) private view returns (Job storage j) {
        j = jobs[id]; require(j.requester != address(0), "JOB_REQUIRED");
    }
}
