// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;
import './Support.sol';
contract Handler {
    Vm private constant vm=Vm(address(uint160(uint256(keccak256('hevm cheat code')))));
    MachineBook public book;Token public token;OracleMock public oracle;
    uint256 public successfulQuotes;uint256 public successfulTakes;uint256 public liquidations;
    constructor(MachineBook b,Token t,OracleMock o){book=b;token=t;oracle=o;}
    function owner(uint32 id) private pure returns(address){return address(uint160(0x9000+id));}
    function quote(uint256 seed) external {
        uint32 id=uint32(seed%12+1);(,,uint64 n,)=book.accounts(id);
        uint16 p=oracle.index();uint16 width=uint16(seed>>32)%8+1;
        uint32 q=uint32(seed>>64)%10+1;
        uint256 c=uint256(n+1)|uint256(vm.getBlockTimestamp()+50)<<64|uint256(p-width)<<104|uint256(p+width)<<120|uint256(q)<<136|uint256(q)<<168;
        (address key,uint40 expiry,uint32 epoch,,,)=book.sessions(id);
        if (seed>>80&1!=0 && key!=address(0) && expiry>vm.getBlockTimestamp()+50) {
            c|=uint256(epoch)<<200;vm.prank(key);
        } else vm.prank(owner(id));
        try book.replace(id,c){successfulQuotes++;}catch{}
    }
    function take(uint256 seed) external {
        uint32 id=uint32(seed%12+1);(,,uint64 n,)=book.accounts(id);bool buy=seed>>32&1!=0;
        uint16 p=oracle.index();uint32 q=uint32(seed>>64)%8+1;
        uint256 c=uint256(n+1)|uint256(vm.getBlockTimestamp()+50)<<64|uint256(buy?p+20:p-20)<<104|uint256(q)<<120|uint256(8)<<152|uint256(buy?1:0)<<160;
        vm.prank(owner(id));try book.take(id,c) returns(uint32 filled){if(filled>0)successfulTakes++;}catch{}
    }
    function session(uint256 seed) external {
        uint32 id=uint32(seed%12+1);vm.prank(owner(id));
        if(seed>>32&1!=0)book.revokeSession(id);
        else book.setSession(id,address(uint160(0xa000+id)),uint40(vm.getBlockTimestamp()+1000),100,1e12,3);
    }
    function cancel(uint256 seed) external {uint32 id=uint32(seed%12+1);vm.prank(owner(id));book.cancel(id);}
    function advance(uint256 seed) external {
        vm.warp(vm.getBlockTimestamp()+(seed%30));uint16 p=uint16(980+(seed>>32)%41);oracle.set(p,p+uint16(seed%3));book.syncFunding();
    }
    function prune(uint8 seed) external {book.prune(seed%2,8);}
    function liquidate(uint256 seed) external {
        uint32 id=uint32(seed%12+1);uint32 liq=uint32((seed>>32)%12+1);(,int64 p,,)=book.accounts(id);
        if(p==0)return;uint32 q=uint32(uint64(p<0?-p:p));vm.prank(owner(liq));try book.liquidate(id,liq,q){liquidations++;}catch{}
    }
    function withdraw(uint256 seed) external {uint32 id=uint32(seed%12+1);vm.prank(owner(id));try book.withdraw(id,uint128(seed%1000000+1),owner(id)){}catch{}}
    function deposit(uint256 seed) external {uint32 id=uint32(seed%12+1);uint128 v=uint128(seed%1000000+1);token.mint(owner(id),v);vm.prank(owner(id));book.deposit(id,v);}
}
contract InvariantTest is Base {
    Handler private handler;address[] private targets;
    function setUp() public override {
        vm.warp(10000);token=new Token();oracle=new OracleMock();book=new MachineBook(address(token),address(oracle),address(this),config());
        for(uint32 i=1;i<=12;i++)create(address(uint160(0x9000+i)),10000e6);
        handler=new Handler(book,token,oracle);targets.push(address(handler));
    }
    function targetContracts() public view returns(address[] memory){return targets;}
    function invariantCollateralAndPositionsConserve() public view {conservation();}
    function invariantSolvencyIndexMatchesEveryAccount() public view {
        uint16[5] memory prices=[uint16(1),uint16(900),uint16(1000),uint16(1100),uint16(65535)];
        uint16 active;
        for(uint32 id=1;id<=book.accountCount();id++)if(pos(id)!=0)active++;
        (uint16 tracked,,)=book.riskCapacity();require(tracked==active,'active slots');
        for(uint256 i;i<5;i++){
            bool expected=book.badDebt()==0;
            for(uint32 id=1;id<=book.accountCount();id++)if(book.equity(id,prices[i])<0)expected=false;
            require(book.solventAt(prices[i])==expected,'global solvency');
        }
    }
    function invariantBookLinksAndBitmaps() public view {
        uint256[512] memory expected;uint256[2] memory root;
        for(uint32 oid=2;oid<=book.accountCount()*2+1;oid++){
            (uint32 prev,uint32 next,uint32 q,uint16 tick,,)=book.orders(oid);if(q==0)continue;
            uint32 side=oid&1;(uint32 head,uint32 tail)=book.levels(side<<16|tick);
            require(head!=0 && tail!=0,'empty live level');
            if(prev==0)require(head==oid,'head');else { (,uint32 pn,,uint16 pt,,)=book.orders(prev);require(pn==oid && pt==tick && (prev&1)==side,'prev'); }
            if(next==0)require(tail==oid,'tail');else { (uint32 np,,,uint16 nt,,)=book.orders(next);require(np==oid && nt==tick && (next&1)==side,'next'); }
            uint32 cursor=head;uint32 seen;bool found;
            while(cursor!=0){require(++seen<=book.accountCount(),'cycle');if(cursor==oid)found=true;(,cursor,,,,)=book.orders(cursor);}
            require(found,'disconnected');
            uint16 w=uint16(side<<8)|tick>>8;expected[w]|=uint256(1)<<(tick&255);root[side]|=uint256(1)<<(tick>>8);
        }
        for(uint16 w;w<512;w++)require(book.words(w)==expected[w],'leaf');
        require(book.roots(0)==root[0] && book.roots(1)==root[1],'root');
        require(book.best(0)==0 || book.best(1)==0 || book.best(0)<book.best(1),'crossed');
    }
}
