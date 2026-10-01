// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice Isolated test fixture, not a production token or customer asset.
contract TestEIP3009Token {
    string public constant name = "Machine Payment Test";
    uint8 public constant decimals = 6;
    bytes32 public immutable DOMAIN_SEPARATOR;
    bytes32 public constant TYPEHASH = keccak256(
        "TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(bytes32 => bool)) public authorizationState;
    event AuthorizationUsed(address indexed authorizer, bytes32 indexed nonce);
    event Transfer(address indexed from, address indexed to, uint256 value);

    constructor() {
        DOMAIN_SEPARATOR = keccak256(abi.encode(
            keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
            keccak256(bytes(name)), keccak256("1"), block.chainid, address(this)
        ));
    }

    function mint(address to, uint256 value) external { balanceOf[to] += value; }

    function transferWithAuthorization(
        address from, address to, uint256 value, uint256 validAfter,
        uint256 validBefore, bytes32 nonce, uint8 v, bytes32 r, bytes32 s
    ) external {
        require(block.timestamp > validAfter && block.timestamp < validBefore, "time window");
        require(!authorizationState[from][nonce], "nonce consumed");
        bytes32 message = keccak256(abi.encode(TYPEHASH, from, to, value, validAfter, validBefore, nonce));
        address signer = ecrecover(keccak256(abi.encodePacked("\x19\x01", DOMAIN_SEPARATOR, message)), v, r, s);
        require(signer != address(0) && signer == from, "signature");
        authorizationState[from][nonce] = true;
        balanceOf[from] -= value;
        balanceOf[to] += value;
        emit AuthorizationUsed(from, nonce);
        emit Transfer(from, to, value);
    }
}
