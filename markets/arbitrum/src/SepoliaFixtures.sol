// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;
import './Interfaces.sol';

/// Public testnet fixtures, never accepted on a production chain.
/// No monetary value or external price provenance is claimed.
contract SepoliaCollateral is ICollateral {
    string public constant name = 'MachineBook Test Collateral';
    string public constant symbol = 'TEST-MUSD';
    uint256 public immutable totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    event Transfer(address indexed from, address indexed to, uint256 amount);
    event Approval(address indexed owner, address indexed spender, uint256 amount);
    constructor(address recipient, uint256 supply) {
        require(block.chainid == 421614 && recipient != address(0) && supply == 3000e6, 'testnet only');
        totalSupply = supply; balanceOf[recipient] = supply;
        emit Transfer(address(0), recipient, supply);
    }
    function decimals() external pure returns (uint8) { return 6; }
    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount; emit Approval(msg.sender, spender, amount); return true;
    }
    function transfer(address to, uint256 amount) external returns (bool) { _move(msg.sender, to, amount); return true; }
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        allowance[from][msg.sender] -= amount; _move(from, to, amount); return true;
    }
    function _move(address from, address to, uint256 amount) private {
        require(to != address(0)); balanceOf[from] -= amount; balanceOf[to] += amount;
        emit Transfer(from, to, amount);
    }
}

contract SepoliaOracle is IMarketOracle {
    address public immutable operator;
    uint16 public index = 1000;
    event TestPrice(uint16 index);
    constructor(address owner) { require(block.chainid == 421614 && owner != address(0), 'testnet only'); operator = owner; }
    function set(uint16 price) external {
        require(msg.sender == operator && price >= 900 && price <= 1100, 'test price scope');
        index = price; emit TestPrice(price);
    }
    function read() external view returns (uint16, uint16) { return (index, index); }
}
