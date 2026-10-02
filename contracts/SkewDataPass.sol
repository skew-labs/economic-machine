// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

interface IDataPaymentToken {
    function balanceOf(address account) external view returns (uint256);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

interface IDataPassReceiver {
    function onERC721Received(address operator, address from, uint256 tokenId, bytes calldata data)
        external returns (bytes4);
}

/// @notice Version-bound data access licenses. Not ownership of upstream data.
/// @dev Payment is atomic with minting; off-chain delivery is not atomic.
contract SkewDataPass {
    string public constant name = "Skew DataPass";
    string public constant symbol = "SDPASS";
    bytes4 private constant RECEIVED = 0x150b7a02;

    struct Release {
        address seller;
        address asset;
        bytes32 contentRoot;
        bytes32 termsHash;
        bytes32 provenanceRoot;
        uint128 price;
        uint64 duration;
        uint64 saleEnds;
        bool transferable;
        bool active;
        string metadataURI;
    }

    struct License {
        bytes32 releaseId;
        uint64 expires;
    }

    struct Sale {
        address seller;
        uint128 price;
        uint64 expires;
        uint64 nonce;
    }

    address public owner;
    address public pendingOwner;
    bool public paused;
    uint256 private entered = 1;
    uint256 public nextTokenId = 1;
    mapping(address => bool) public publishers;
    mapping(address => bool) public paymentAssets;
    mapping(bytes32 => Release) private releases;
    mapping(uint256 => License) public licenses;
    mapping(uint256 => address) private owners;
    mapping(address => uint256) private balances;
    mapping(uint256 => address) private approvals;
    mapping(address => mapping(address => bool)) private operators;
    mapping(address => mapping(bytes32 => uint256)) public purchaseIds;
    mapping(uint256 => Sale) public sales;
    mapping(uint256 => uint64) public saleNonces;

    event Transfer(address indexed from, address indexed to, uint256 indexed tokenId);
    event SaleListed(uint256 indexed tokenId, address indexed seller, uint128 price, uint64 expires, uint64 nonce);
    event SaleCancelled(uint256 indexed tokenId, uint64 nonce);
    event LicenseResold(uint256 indexed tokenId, address indexed seller, address indexed buyer,
        uint128 price, bytes32 purchaseId);
    event Approval(address indexed owner, address indexed approved, uint256 indexed tokenId);
    event ApprovalForAll(address indexed owner, address indexed operator, bool approved);
    event ReleaseRegistered(bytes32 indexed releaseId, address indexed seller, bytes32 contentRoot,
        bytes32 termsHash, bytes32 provenanceRoot, address asset, uint256 price);
    event LicensePurchased(uint256 indexed tokenId, bytes32 indexed releaseId, address indexed buyer,
        bytes32 purchaseId, uint256 expires, uint256 amount);
    event ReleaseStopped(bytes32 indexed releaseId);
    event PublisherPermission(address indexed publisher, bool allowed);
    event AssetPermission(address indexed asset, bool allowed);
    event PauseChanged(bool paused);
    event OwnershipTransferStarted(address indexed priorOwner, address indexed pendingOwner);
    event OwnershipTransferred(address indexed priorOwner, address indexed newOwner);

    error Unauthorized();
    error InvalidTerms();
    error Unavailable();
    error ExpiredLicense();
    error NonTransferable();
    error UnsafeRecipient();
    error TokenNotFound();
    error ExactPaymentRequired();
    error Reentrant();

    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    modifier guarded() {
        if (entered != 1) revert Reentrant();
        entered = 2;
        _;
        entered = 1;
    }

    constructor(address initialOwner, address initialAsset) {
        if (initialOwner == address(0) || initialAsset.code.length == 0) revert InvalidTerms();
        owner = initialOwner;
        publishers[initialOwner] = true;
        paymentAssets[initialAsset] = true;
        emit OwnershipTransferred(address(0), initialOwner);
        emit PublisherPermission(initialOwner, true);
        emit AssetPermission(initialAsset, true);
    }

    function supportsInterface(bytes4 interfaceId) external pure returns (bool) {
        return interfaceId == 0x01ffc9a7 || interfaceId == 0x80ac58cd || interfaceId == 0x5b5e139f;
    }

    function setPublisher(address publisher, bool allowed) external onlyOwner {
        if (publisher == address(0)) revert InvalidTerms();
        publishers[publisher] = allowed;
        emit PublisherPermission(publisher, allowed);
    }

    function setPaymentAsset(address asset, bool allowed) external onlyOwner {
        if (asset.code.length == 0) revert InvalidTerms();
        paymentAssets[asset] = allowed;
        emit AssetPermission(asset, allowed);
    }

    function setPaused(bool value) external onlyOwner {
        paused = value;
        emit PauseChanged(value);
    }

    function startOwnershipTransfer(address successor) external onlyOwner {
        if (successor == address(0) || successor == owner) revert InvalidTerms();
        pendingOwner = successor;
        emit OwnershipTransferStarted(owner, successor);
    }

    function acceptOwnership() external {
        if (msg.sender != pendingOwner) revert Unauthorized();
        address prior = owner;
        owner = msg.sender;
        pendingOwner = address(0);
        // Administrative ownership doesn't silently add publication authority.
        emit OwnershipTransferred(prior, owner);
    }

    function registerRelease(bytes32 releaseId, Release calldata release) external guarded {
        if (!publishers[msg.sender] || msg.sender != release.seller) revert Unauthorized();
        if (paused || releases[releaseId].seller != address(0)) revert Unavailable();
        if (releaseId == bytes32(0) || release.contentRoot == bytes32(0) || release.termsHash == bytes32(0)
            || release.provenanceRoot == bytes32(0) || !paymentAssets[release.asset]
            || release.price == 0 || release.duration < 60 || release.duration > 365 days
            || release.saleEnds <= block.timestamp || release.saleEnds > block.timestamp + 365 days
            || !release.active || bytes(release.metadataURI).length == 0
            || bytes(release.metadataURI).length > 512) revert InvalidTerms();
        releases[releaseId] = release;
        emit ReleaseRegistered(releaseId, release.seller, release.contentRoot, release.termsHash,
            release.provenanceRoot, release.asset, release.price);
    }

    function releaseInfo(bytes32 releaseId) external view returns (Release memory) {
        if (releases[releaseId].seller == address(0)) revert Unavailable();
        return releases[releaseId];
    }

    function stopRelease(bytes32 releaseId) external {
        Release storage release = releases[releaseId];
        if (msg.sender != release.seller && msg.sender != owner) revert Unauthorized();
        if (release.seller == address(0)) revert Unavailable();
        release.active = false;
        emit ReleaseStopped(releaseId);
    }

    function purchase(bytes32 releaseId, bytes32 expectedContentRoot, bytes32 expectedTermsHash,
        uint128 expectedPrice, bytes32 purchaseId) external guarded returns (uint256 tokenId) {
        Release storage release = releases[releaseId];
        if (paused || !release.active || !publishers[release.seller] || !paymentAssets[release.asset]
            || block.timestamp >= release.saleEnds || purchaseId == bytes32(0)) revert Unavailable();
        if (release.contentRoot != expectedContentRoot || release.termsHash != expectedTermsHash
            || release.price != expectedPrice || purchaseIds[msg.sender][purchaseId] != 0
            || msg.sender == release.seller) revert InvalidTerms();
        if (block.timestamp + release.duration > type(uint64).max) revert InvalidTerms();
        IDataPaymentToken asset = IDataPaymentToken(release.asset);
        uint256 buyerBefore = asset.balanceOf(msg.sender);
        uint256 sellerBefore = asset.balanceOf(release.seller);
        tokenId = nextTokenId++;
        purchaseIds[msg.sender][purchaseId] = tokenId;
        licenses[tokenId] = License(releaseId, uint64(block.timestamp + release.duration));
        owners[tokenId] = msg.sender;
        balances[msg.sender]++;
        // No custom-token callbacks can re-enter purchase or safe transfer.
        (bool okay, bytes memory response) = release.asset.call(abi.encodeCall(
            IDataPaymentToken.transferFrom, (msg.sender, release.seller, release.price)));
        if (!okay || (response.length != 0 && (response.length != 32 || !abi.decode(response, (bool)))))
            revert ExactPaymentRequired();
        uint256 buyerAfter = asset.balanceOf(msg.sender);
        uint256 sellerAfter = asset.balanceOf(release.seller);
        if (buyerAfter > buyerBefore || sellerAfter < sellerBefore
            || buyerBefore - buyerAfter != release.price || sellerAfter - sellerBefore != release.price)
            revert ExactPaymentRequired();
        emit Transfer(address(0), msg.sender, tokenId);
        emit LicensePurchased(tokenId, releaseId, msg.sender, purchaseId,
            licenses[tokenId].expires, release.price);
        _receive(address(0), msg.sender, tokenId, "");
    }

    function balanceOf(address account) external view returns (uint256) {
        if (account == address(0)) revert InvalidTerms();
        return balances[account];
    }

    function listSale(uint256 tokenId, uint128 price, uint64 expires) external guarded {
        if (ownerOf(tokenId) != msg.sender) revert Unauthorized();
        License memory license = licenses[tokenId];
        if (paused || price == 0 || expires <= block.timestamp || expires > license.expires)
            revert InvalidTerms();
        if (!releases[license.releaseId].transferable) revert NonTransferable();
        uint64 nonce = ++saleNonces[tokenId];
        sales[tokenId] = Sale(msg.sender, price, expires, nonce);
        emit SaleListed(tokenId, msg.sender, price, expires, nonce);
    }

    function cancelSale(uint256 tokenId) external guarded {
        if (msg.sender != sales[tokenId].seller && msg.sender != ownerOf(tokenId)) revert Unauthorized();
        delete sales[tokenId];
        uint64 nonce = ++saleNonces[tokenId];
        emit SaleCancelled(tokenId, nonce);
    }

    function purchaseResale(uint256 tokenId, address expectedSeller, uint128 expectedPrice,
        uint64 expectedNonce, bytes32 expectedContentRoot, bytes32 expectedTermsHash,
        bytes32 purchaseId) external guarded {
        Sale memory sale = sales[tokenId];
        License memory license = licenses[tokenId];
        Release storage release = releases[license.releaseId];
        if (paused || sale.seller == address(0) || block.timestamp >= sale.expires
            || !paymentAssets[release.asset]) revert Unavailable();
        if (ownerOf(tokenId) != sale.seller || sale.seller != expectedSeller || sale.price != expectedPrice
            || sale.nonce != expectedNonce || msg.sender == sale.seller || purchaseId == bytes32(0)
            || purchaseIds[msg.sender][purchaseId] != 0 || release.contentRoot != expectedContentRoot
            || release.termsHash != expectedTermsHash) revert InvalidTerms();
        if (!release.transferable) revert NonTransferable();
        if (block.timestamp >= license.expires) revert ExpiredLicense();
        purchaseIds[msg.sender][purchaseId] = tokenId;
        _payExact(release.asset, msg.sender, sale.seller, sale.price);
        _move(sale.seller, msg.sender, tokenId);
        emit LicenseResold(tokenId, sale.seller, msg.sender, sale.price, purchaseId);
        _receive(sale.seller, msg.sender, tokenId, "");
    }

    function _payExact(address token, address buyer, address seller, uint128 amount) private {
        IDataPaymentToken asset = IDataPaymentToken(token);
        uint256 buyerBefore = asset.balanceOf(buyer);
        uint256 sellerBefore = asset.balanceOf(seller);
        (bool okay, bytes memory response) = token.call(abi.encodeCall(
            IDataPaymentToken.transferFrom, (buyer, seller, amount)));
        if (!okay || (response.length != 0 && (response.length != 32 || !abi.decode(response, (bool)))))
            revert ExactPaymentRequired();
        uint256 buyerAfter = asset.balanceOf(buyer);
        uint256 sellerAfter = asset.balanceOf(seller);
        if (buyerAfter > buyerBefore || sellerAfter < sellerBefore
            || buyerBefore - buyerAfter != amount || sellerAfter - sellerBefore != amount)
            revert ExactPaymentRequired();
    }

    function ownerOf(uint256 tokenId) public view returns (address holder) {
        holder = owners[tokenId];
        if (holder == address(0)) revert TokenNotFound();
    }

    function tokenURI(uint256 tokenId) external view returns (string memory) {
        ownerOf(tokenId);
        return releases[licenses[tokenId].releaseId].metadataURI;
    }

    function getApproved(uint256 tokenId) external view returns (address) {
        ownerOf(tokenId);
        return approvals[tokenId];
    }

    function isApprovedForAll(address holder, address operator) external view returns (bool) {
        return operators[holder][operator];
    }

    function approve(address recipient, uint256 tokenId) external {
        address holder = ownerOf(tokenId);
        if (msg.sender != holder && !operators[holder][msg.sender]) revert Unauthorized();
        if (recipient == holder) revert InvalidTerms();
        approvals[tokenId] = recipient;
        emit Approval(holder, recipient, tokenId);
    }

    function setApprovalForAll(address operator, bool approved) external {
        if (operator == msg.sender || operator == address(0)) revert InvalidTerms();
        operators[msg.sender][operator] = approved;
        emit ApprovalForAll(msg.sender, operator, approved);
    }

    function transferFrom(address from, address to, uint256 tokenId) public guarded {
        _transfer(from, to, tokenId);
    }

    function safeTransferFrom(address from, address to, uint256 tokenId) external guarded {
        _transfer(from, to, tokenId);
        _receive(from, to, tokenId, "");
    }

    function safeTransferFrom(address from, address to, uint256 tokenId, bytes calldata data) external guarded {
        _transfer(from, to, tokenId);
        _receive(from, to, tokenId, data);
    }

    function _transfer(address from, address to, uint256 tokenId) private {
        address holder = ownerOf(tokenId);
        if (from != holder || to == address(0)) revert InvalidTerms();
        if (msg.sender != holder && approvals[tokenId] != msg.sender && !operators[holder][msg.sender])
            revert Unauthorized();
        _move(from, to, tokenId);
    }

    function _move(address from, address to, uint256 tokenId) private {
        License memory license = licenses[tokenId];
        if (paused) revert Unavailable();
        if (block.timestamp >= license.expires) revert ExpiredLicense();
        if (!releases[license.releaseId].transferable) revert NonTransferable();
        delete approvals[tokenId];
        delete sales[tokenId];
        ++saleNonces[tokenId];
        balances[from]--;
        balances[to]++;
        owners[tokenId] = to;
        emit Transfer(from, to, tokenId);
    }

    function _receive(address from, address to, uint256 tokenId, bytes memory data) private {
        if (to.code.length > 0) {
            try IDataPassReceiver(to).onERC721Received(msg.sender, from, tokenId, data) returns (bytes4 result) {
                if (result != RECEIVED) revert UnsafeRecipient();
            } catch { revert UnsafeRecipient(); }
        }
    }

    function entitlement(uint256 tokenId, address holder, bytes32 contentRoot, bytes32 termsHash)
        external view returns (bool) {
        if (holder == address(0) || owners[tokenId] != holder) return false;
        License memory license = licenses[tokenId];
        Release storage release = releases[license.releaseId];
        // Stopping sales doesn't revoke a paid, unexpired license.
        return block.timestamp < license.expires && release.contentRoot == contentRoot && release.termsHash == termsHash;
    }
}
