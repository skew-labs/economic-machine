// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;
/// Isolated-controller fixture only; never deployed to a public network.
contract PilotToken {
    mapping(address=>uint256) public balanceOf;
    mapping(address=>mapping(address=>uint256)) public allowance;
    event Transfer(address indexed from,address indexed to,uint256 value);
    function decimals() external pure returns(uint8){return 6;}
    function mint(address to,uint256 amount) external {balanceOf[to]+=amount;emit Transfer(address(0),to,amount);}
    function approve(address to,uint256 amount) external returns(bool){allowance[msg.sender][to]=amount;return true;}
    function transfer(address to,uint256 amount) external returns(bool){move(msg.sender,to,amount);return true;}
    function transferFrom(address from,address to,uint256 amount) external returns(bool){allowance[from][msg.sender]-=amount;move(from,to,amount);return true;}
    function move(address from,address to,uint256 amount) internal {balanceOf[from]-=amount;balanceOf[to]+=amount;emit Transfer(from,to,amount);}
}
