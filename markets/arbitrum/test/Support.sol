// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;
import '../src/MachineBook.sol';
import '../src/ChainlinkOracle.sol';
interface Vm {
    function warp(uint256) external;
    function getBlockTimestamp() external view returns (uint256);
    function prank(address) external;
    function startPrank(address) external;
    function stopPrank() external;
    function expectRevert() external;
    function expectRevert(bytes4) external;
    function pauseGasMetering() external;
    function resumeGasMetering() external;
}
contract Token is ICollateral {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    uint256 public fee;
    address public callback;
    bytes public callbackData;
    bool public callbackSucceeded;
    function decimals() external pure returns (uint8) { return 6; }
    function mint(address who, uint256 amount) external { balanceOf[who] += amount; }
    function approve(address who, uint256 amount) external returns (bool) { allowance[msg.sender][who] = amount; return true; }
    function setFee(uint256 f) external { fee = f; }
    function hook(address who, bytes calldata data) external { callback = who; callbackData = data; }
    function transfer(address who, uint256 amount) external returns (bool) { _transfer(msg.sender, who, amount); return true; }
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        allowance[from][msg.sender] -= amount; _transfer(from, to, amount); return true;
    }
    function _transfer(address from, address to, uint256 amount) private {
        balanceOf[from] -= amount; balanceOf[to] += amount - fee;
        if (callback != address(0)) { (bool ok,) = callback.call(callbackData); callbackSucceeded = ok; }
    }
}
contract OracleMock is IMarketOracle {
    uint16 public index = 1000; uint16 public mark = 1000; bool public stale;
    function set(uint16 i, uint16 m) external { index = i; mark = m; }
    function setStale(bool x) external { stale = x; }
    function read() external view returns (uint16, uint16) { require(!stale, 'stale'); return (index, mark); }
}
contract FeedMock is IAggregator {
    uint8 public decimals = 8;
    uint80 public round = 1; uint80 public answered = 1;
    int256 public answer = 1000e8; uint256 public started = 1; uint256 public updated = 10000;
    function set(int256 a, uint256 t, uint256 s, uint80 r, uint80 ar) external { answer=a;updated=t;started=s;round=r;answered=ar; }
    function latestRoundData() external view returns (uint80, int256, uint256, uint256, uint80) {return (round,answer,started,updated,answered);}
}
contract Base {
    Vm internal constant vm = Vm(address(uint160(uint256(keccak256('hevm cheat code')))));
    Token internal token; OracleMock internal oracle; MachineBook internal book;
    address internal constant ALICE = address(0x1001);
    address internal constant BOB = address(0x1002);
    address internal constant CAROL = address(0x1003);
    address internal constant AGENT = address(0x2001);
    uint32 internal alice; uint32 internal bob; uint32 internal carol;
    function config() internal pure returns (MachineBook.Config memory) {
        return MachineBook.Config(1000000,10000,1000,500,5,100,1000,10,60,300);
    }
    function setUp() public virtual {
        vm.warp(10000);token=new Token();oracle=new OracleMock();book=new MachineBook(address(token),address(oracle),address(this),config());
        alice=create(ALICE,1000000000000);bob=create(BOB,1000000000000);carol=create(CAROL,1000000000000);
    }
    function create(address owner, uint128 amount) internal returns(uint32 id) {
        vm.startPrank(owner);id=book.open();token.mint(owner,amount);token.approve(address(book),type(uint256).max);book.deposit(id,amount);vm.stopPrank();
    }
    function nonce(uint32 id) internal view returns(uint64 n) { (,,n,) = book.accounts(id); }
    function pos(uint32 id) internal view returns(int64 p) { (,p,,)=book.accounts(id); }
    function cash(uint32 id) internal view returns(int128 c) { (c,,,)=book.accounts(id); }
    function qty(uint32 oid) internal view returns(uint32 q) { (,,q,,,)=book.orders(oid); }
    function qcmd(uint64 n,uint16 bid,uint16 ask,uint32 bq,uint32 aq) internal view returns(uint256) {
        return uint256(n) | uint256(vm.getBlockTimestamp()+50)<<64 | uint256(bid)<<104 | uint256(ask)<<120 | uint256(bq)<<136 | uint256(aq)<<168;
    }
    function tcmd(uint64 n,bool buy,uint16 price,uint32 size,uint8 steps,bool reduce) internal view returns(uint256) {
        return uint256(n) | uint256(vm.getBlockTimestamp()+50)<<64 | uint256(price)<<104 | uint256(size)<<120 | uint256(steps)<<152 | uint256(buy?1:0)<<160 | uint256(reduce?1:0)<<161;
    }
    function quote(uint32 id,uint16 bid,uint16 ask,uint32 bq,uint32 aq) internal {
        uint256 c=qcmd(nonce(id)+1,bid,ask,bq,aq);vm.prank(book.owners(id));book.replace(id,c);
    }
    function take(uint32 id,bool buy,uint16 price,uint32 size,uint8 steps) internal returns(uint32) {
        uint256 c=tcmd(nonce(id)+1,buy,price,size,steps,false);vm.prank(book.owners(id));return book.take(id,c);
    }
    function conservation() internal view {
        int256 sum;int256 bases;
        for(uint32 id=1;id<=book.accountCount();id++) {
            (int128 c,int64 p,,int128 f)=book.accounts(id);
            sum+=int256(c)-int256(p)*(int256(book.fundingIndex())-f);bases+=p;
        }
        require(bases==0,'base conservation');
        require(sum+int256(uint256(book.insurance()))-int256(uint256(book.badDebt()))==int256(token.balanceOf(address(book))),'cash conservation');
    }
}
