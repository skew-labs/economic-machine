// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice Test-only token. Never presented as real USDC.
contract TestCommerceToken {
    string public constant name = "Machine Commerce Test Credit";
    string public constant symbol = "MCTEST";
    uint8 public constant decimals = 6;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    bool public failTransfers;
    bool public chargeTransferFee;
    event Transfer(address indexed from, address indexed to, uint256 amount);

    function mint(address recipient, uint256 amount) external { balanceOf[recipient] += amount; }
    function setFailTransfers(bool value) external { failTransfers = value; }
    function setChargeTransferFee(bool value) external { chargeTransferFee = value; }
    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }
    function transfer(address to, uint256 amount) external returns (bool) {
        if (failTransfers) return false;
        _move(msg.sender, to, amount);
        return true;
    }
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        if (failTransfers) return false;
        allowance[from][msg.sender] -= amount;
        _move(from, to, amount);
        return true;
    }
    function _move(address from, address to, uint256 amount) internal {
        balanceOf[from] -= amount;
        uint256 output = chargeTransferFee ? amount - 1 : amount;
        balanceOf[to] += output;
        emit Transfer(from, to, output);
    }
}
