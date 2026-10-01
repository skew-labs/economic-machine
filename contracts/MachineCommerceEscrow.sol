// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice Buyer-scoped ERC20 budgets for agents buying verifiable services.
/// @dev The buyer selects a delivery verifier. Its attestation is a trust
///      boundary, not a cryptographic proof that data is true or useful.
///      Only exact-transfer tokens are supported. No upgrade or arbitrary call.
contract MachineCommerceEscrow {
    enum Phase { None, Funded, Delivered, Settled, Refunded }

    struct Budget {
        address buyer;
        address agent;
        address seller;
        address verifier;
        address token;
        uint256 ceiling;
        uint256 perOrder;
        uint256 reserved;
        uint256 spent;
        uint64 expiresAt;
        bool active;
    }

    struct Order {
        bytes32 budgetId;
        bytes32 serviceHash;
        bytes32 requestHash;
        bytes32 termsHash;
        bytes32 artifactHash;
        uint256 amount;
        uint64 deadline;
        Phase phase;
    }

    address public immutable platform;
    uint16 public immutable feeBps;
    mapping(bytes32 => Budget) public budgets;
    mapping(bytes32 => Order) public orders;
    mapping(address => uint256) public liabilities;
    bool private entered;

    event BudgetRegistered(bytes32 indexed budgetId, address indexed buyer,
        address indexed agent, address seller, address verifier, address token,
        uint256 ceiling, uint256 perOrder, uint64 expiresAt);
    event BudgetRevoked(bytes32 indexed budgetId);
    event OrderFunded(bytes32 indexed orderId, bytes32 indexed budgetId,
        bytes32 serviceHash, bytes32 requestHash, bytes32 termsHash, uint256 amount, uint64 deadline);
    event ArtifactDelivered(bytes32 indexed orderId, bytes32 indexed artifactHash);
    event PaymentSettled(bytes32 indexed orderId, bytes32 indexed artifactHash,
        bytes32 verificationHash, address seller, address token, uint256 sellerAmount, uint256 fee);
    event PaymentRefunded(bytes32 indexed orderId, address buyer, address token, uint256 amount);

    error Unauthorized();
    error InvalidTerms();
    error InvalidPhase();
    error Expired();
    error BudgetExceeded();
    error TransferMismatch();
    error Reentrancy();

    constructor(address platform_, uint16 feeBps_) {
        if (platform_ == address(0) || feeBps_ > 1000) revert InvalidTerms();
        platform = platform_;
        feeBps = feeBps_;
    }

    modifier nonReentrant() {
        if (entered) revert Reentrancy();
        entered = true;
        _;
        entered = false;
    }

    function registerBudget(bytes32 budgetId, address agent, address seller,
        address verifier, address token, uint256 ceiling, uint256 perOrder, uint64 expiresAt) external {
        if (budgetId == bytes32(0) || budgets[budgetId].buyer != address(0)
            || agent == address(0) || seller == address(0) || verifier == address(0)
            || token.code.length == 0 || seller == msg.sender || seller == platform
            || verifier == seller || platform == msg.sender
            || ceiling == 0 || perOrder == 0 || perOrder > ceiling
            || expiresAt <= block.timestamp) revert InvalidTerms();
        budgets[budgetId] = Budget(msg.sender, agent, seller, verifier, token,
            ceiling, perOrder, 0, 0, expiresAt, true);
        emit BudgetRegistered(budgetId, msg.sender, agent, seller, verifier,
            token, ceiling, perOrder, expiresAt);
    }

    function revokeBudget(bytes32 budgetId) external {
        Budget storage budget = budgets[budgetId];
        if (msg.sender != budget.buyer) revert Unauthorized();
        budget.active = false;
        emit BudgetRevoked(budgetId);
    }

    function fundOrder(bytes32 orderId, bytes32 budgetId, bytes32 serviceHash,
        bytes32 requestHash, bytes32 termsHash, uint256 amount, uint64 deadline) external nonReentrant {
        Budget storage budget = budgets[budgetId];
        if (msg.sender != budget.agent && msg.sender != budget.buyer) revert Unauthorized();
        if (!budget.active || budget.expiresAt <= block.timestamp) revert Expired();
        if (orderId == bytes32(0) || orders[orderId].phase != Phase.None
            || serviceHash == bytes32(0) || requestHash == bytes32(0) || termsHash == bytes32(0)
            || amount == 0 || deadline <= block.timestamp || deadline > budget.expiresAt
            || deadline > block.timestamp + 1 days) revert InvalidTerms();
        if (amount > budget.perOrder || budget.spent + budget.reserved + amount > budget.ceiling)
            revert BudgetExceeded();
        budget.reserved += amount;
        liabilities[budget.token] += amount;
        orders[orderId] = Order(budgetId, serviceHash, requestHash, termsHash,
            bytes32(0), amount, deadline, Phase.Funded);
        uint256 beforeBalance = _balance(budget.token, address(this));
        _call(budget.token, abi.encodeWithSignature("transferFrom(address,address,uint256)",
            budget.buyer, address(this), amount));
        if (_balance(budget.token, address(this)) != beforeBalance + amount) revert TransferMismatch();
        emit OrderFunded(orderId, budgetId, serviceHash, requestHash, termsHash, amount, deadline);
    }

    function deliver(bytes32 orderId, bytes32 artifactHash) external {
        Order storage order = orders[orderId];
        Budget storage budget = budgets[order.budgetId];
        if (msg.sender != budget.seller) revert Unauthorized();
        if (order.phase != Phase.Funded || artifactHash == bytes32(0)) revert InvalidPhase();
        if (order.deadline <= block.timestamp) revert Expired();
        order.artifactHash = artifactHash;
        order.phase = Phase.Delivered;
        emit ArtifactDelivered(orderId, artifactHash);
    }

    /// @notice The buyer-designated verifier accepts this exact artifact hash.
    function settle(bytes32 orderId, bytes32 artifactHash, bytes32 verificationHash) external nonReentrant {
        Order storage order = orders[orderId];
        Budget storage budget = budgets[order.budgetId];
        if (msg.sender != budget.verifier) revert Unauthorized();
        if (order.phase != Phase.Delivered || artifactHash != order.artifactHash
            || verificationHash == bytes32(0)) revert InvalidPhase();
        if (order.deadline <= block.timestamp) revert Expired();
        uint256 fee = order.amount * feeBps / 10000;
        uint256 sellerAmount = order.amount - fee;
        order.phase = Phase.Settled;
        budget.reserved -= order.amount;
        budget.spent += order.amount;
        liabilities[budget.token] -= order.amount;
        _transferExact(budget.token, budget.seller, sellerAmount);
        if (fee > 0) _transferExact(budget.token, platform, fee);
        emit PaymentSettled(orderId, artifactHash, verificationHash,
            budget.seller, budget.token, sellerAmount, fee);
    }

    /// @notice Anyone can trigger a refund after expiry; destination is fixed.
    function refundExpired(bytes32 orderId) external nonReentrant {
        Order storage order = orders[orderId];
        Budget storage budget = budgets[order.budgetId];
        if (order.phase != Phase.Funded && order.phase != Phase.Delivered) revert InvalidPhase();
        if (order.deadline > block.timestamp) revert InvalidPhase();
        _refund(orderId, order, budget);
    }

    /// @notice A selected verifier can reject delivered data before the deadline.
    function rejectDelivery(bytes32 orderId, bytes32 artifactHash) external nonReentrant {
        Order storage order = orders[orderId];
        Budget storage budget = budgets[order.budgetId];
        if (msg.sender != budget.verifier) revert Unauthorized();
        if (order.phase != Phase.Delivered || artifactHash != order.artifactHash) revert InvalidPhase();
        _refund(orderId, order, budget);
    }

    function _refund(bytes32 orderId, Order storage order, Budget storage budget) internal {
        order.phase = Phase.Refunded;
        budget.reserved -= order.amount;
        liabilities[budget.token] -= order.amount;
        _transferExact(budget.token, budget.buyer, order.amount);
        emit PaymentRefunded(orderId, budget.buyer, budget.token, order.amount);
    }

    function _balance(address token, address account) internal view returns (uint256) {
        (bool ok, bytes memory result) = token.staticcall(abi.encodeWithSignature("balanceOf(address)", account));
        if (!ok || result.length != 32) revert TransferMismatch();
        return abi.decode(result, (uint256));
    }

    function _call(address token, bytes memory payload) internal {
        (bool ok, bytes memory result) = token.call(payload);
        if (!ok || (result.length != 0 && (result.length != 32 || !abi.decode(result, (bool)))))
            revert TransferMismatch();
    }

    function _transferExact(address token, address recipient, uint256 amount) internal {
        uint256 escrowBefore = _balance(token, address(this));
        uint256 recipientBefore = _balance(token, recipient);
        _call(token, abi.encodeWithSignature("transfer(address,uint256)", recipient, amount));
        if (_balance(token, address(this)) != escrowBefore - amount
            || _balance(token, recipient) != recipientBefore + amount) revert TransferMismatch();
    }
}
