// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;
import './Support.sol';
contract MakerEndpointTest is Base {
    function testFuzzMakerPartialFillRemainsInsideTwoSidedEndpoints(uint16 bid,uint16 gap,uint16 bq,uint16 aq,uint16 clip,uint16 margin,bool buy) public {
        bid=uint16(950+bid%45);uint16 ask=uint16(uint256(bid)+gap%50+1);
        bq=bq%10+1;aq=aq%10+1;clip=uint16(clip%(buy?aq:bq)+1);
        MachineBook.Config memory cfg=config();cfg.tickValue=1;cfg.initialBps=uint16(501+margin%9499);
        book=new MachineBook(address(token),address(oracle),address(this),cfg);
        int256 bidNeed=int256((uint256(bq)*1000*cfg.initialBps+9999)/10000)+int256(uint256(bq))*(int256(uint256(bid))-1000);
        int256 askNeed=int256((uint256(aq)*1000*cfg.initialBps+9999)/10000)+int256(uint256(aq))*(1000-int256(uint256(ask)));
        int256 needed=bidNeed>askNeed?bidNeed:askNeed;if(needed<1)needed=1;
        // Exactly the minimum admitted integer cash, not an ample-collateral toy.
        alice=create(ALICE,uint128(uint256(needed)));bob=create(BOB,1e12);carol=create(CAROL,1e12);
        quote(alice,bid,ask,bq,aq);take(bob,buy,buy?ask:bid,clip,1);
        int64 base=pos(alice);int256 c=cash(alice);uint32 leftBid=qty(alice*2);uint32 leftAsk=qty(alice*2+1);
        _endpoint(c,base);_endpoint(c-int256(uint256(leftBid)*bid),base+int64(uint64(leftBid)));
        _endpoint(c+int256(uint256(leftAsk)*ask),base-int64(uint64(leftAsk)));conservation();
    }
    function _endpoint(int256 c,int64 p) private view {
        uint256 size=uint256(p<0?-int256(p):int256(p));require(size<=10000);
        require(c+int256(p)*1000>=int256((size*1000*book.initialBps()+9999)/10000));
    }
    function testScopedMakerPositionCapStillEnforcedBeforeMatching() public {
        vm.prank(ALICE);book.setSession(alice,AGENT,uint40(vm.getBlockTimestamp()+60),2,1e12,1);
        uint256 c=qcmd(nonce(alice)+1,999,1001,2,2)|uint256(1)<<200;
        vm.prank(AGENT);book.replace(alice,c);take(bob,true,1001,1,1);
        require(pos(alice)==-1 && qty(alice*2)==2 && qty(alice*2+1)==1);
        take(bob,false,999,2,1);require(pos(alice)==1);conservation();
    }
}
