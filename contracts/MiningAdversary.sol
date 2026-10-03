// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice Test-only ERC20 callback attack. Not an accepted production reward asset.
contract MiningAdversary {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    address public target;
    bytes public payload;
    bool public attempted;
    bool public blocked;
    function mint(address to, uint256 amount) external { balanceOf[to] += amount; }
    function approve(address to, uint256 amount) external returns (bool) { allowance[msg.sender][to] = amount; return true; }
    function hook(address to, bytes calldata data) external { target = to; payload = data; }
    function attack() private {
        if (target != address(0)) {
            attempted = true; (bool ok,) = target.call(payload); blocked = !ok;
        }
    }
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        allowance[from][msg.sender] -= amount; balanceOf[from] -= amount; balanceOf[to] += amount; attack(); return true;
    }
    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount; balanceOf[to] += amount; attack(); return true;
    }
}
