// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

interface IReentrantDataPass {
    function purchase(bytes32, bytes32, bytes32, uint128, bytes32) external returns (uint256);
}

contract DataPassHostileToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    uint256 public mode;
    address public callback;
    bytes public callbackData;

    function mint(address who, uint256 amount) external { balanceOf[who] += amount; }
    function approve(address who, uint256 amount) external returns (bool) {
        allowance[msg.sender][who] = amount;
        return true;
    }
    function configure(uint256 value, address target, bytes calldata data) external {
        mode = value; callback = target; callbackData = data;
    }
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(allowance[from][msg.sender] >= amount && balanceOf[from] >= amount);
        allowance[from][msg.sender] -= amount;
        if (mode == 1) return false;
        if (mode == 2) { balanceOf[from] -= amount; balanceOf[to] += amount - 1; return true; }
        if (mode == 3) return true;
        if (mode == 4) {
            (bool okay,) = callback.call(callbackData);
            require(okay, "callback failed");
        }
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

contract DataPassHostileReceiver {
    bool public accept;
    address public target;
    bytes public data;
    function configure(bool accepted, address callback, bytes calldata payload) external {
        accept = accepted; target = callback; data = payload;
    }
    function onERC721Received(address, address, uint256, bytes calldata) external returns (bytes4) {
        if (target != address(0)) {
            (bool okay,) = target.call(data);
            require(okay, "callback failed");
        }
        return accept ? bytes4(0x150b7a02) : bytes4(0xffffffff);
    }
}
