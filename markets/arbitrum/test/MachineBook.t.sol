// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;
import './Support.sol';
contract MachineBookTest is Base {
    function testAtomicPackedBatchAndIndependentNonces() public {
        vm.prank(ALICE);book.setSession(alice,AGENT,uint40(block.timestamp+1000),10,1e12,1);
        vm.prank(BOB);book.setSession(bob,AGENT,uint40(block.timestamp+1000),10,1e12,1);
        bytes memory packet=abi.encodePacked(alice,qcmd(1,999,1001,2,2)|(uint256(1)<<200),bob,qcmd(1,998,1002,2,2)|(uint256(1)<<200));
        vm.prank(AGENT);book.replaceBatch(packet);require(nonce(alice)==1 && nonce(bob)==1);
        packet=abi.encodePacked(alice,qcmd(2,999,1001,1,1)|(uint256(1)<<200),bob,qcmd(1,998,1002,1,1)|(uint256(1)<<200));
        vm.prank(AGENT);vm.expectRevert(MachineBook.Sequence.selector);book.replaceBatch(packet);
        require(nonce(alice)==1 && qty(alice*2)==2);conservation();
        vm.prank(AGENT);vm.expectRevert(MachineBook.Invalid.selector);book.replaceBatch(hex'00');
    }
    function test256MakersEightBoundedSweeps() public {
        for(uint32 i;i<256;i++){
            uint32 id=create(address(uint160(0x5000+i)),1000e6);quote(id,0,1001,0,1);
        }
        for(uint32 i;i<8;i++)require(take(bob,true,1001,32,32)==32);
        require(pos(bob)==256 && book.best(1)==0);conservation();
    }
    function testMassLiquidation64AccountsAfterTwentyPercentShock() public {
        uint32[64] memory victims;quote(alice,0,1000,0,64);
        for(uint32 i;i<64;i++){
            victims[i]=create(address(uint160(0x6000+i)),110e6);take(victims[i],true,1000,1,8);
        }
        oracle.set(800,800);
        for(uint32 i;i<64;i++){
            vm.prank(CAROL);book.liquidate(victims[i],carol,1);
            require(pos(victims[i])==0 && cash(victims[i])==0);
        }
        require(book.badDebt()>0 && pos(carol)==64);conservation();
    }
    function testReduceOnlyImprovesLeverageBelowInitialMargin() public {
        uint32 small=create(address(0x4001),1010e6);quote(alice,0,1000,0,10);take(small,true,1000,10,8);
        oracle.set(960,960);quote(alice,960,0,1,0);
        uint256 command=tcmd(nonce(small)+1,false,960,1,8,true);
        vm.prank(address(0x4001));book.take(small,command);require(pos(small)==9);conservation();
    }
    function testInsuranceAbsorbsBankruptcyBeforeHalt() public {
        token.mint(address(this),200e6);token.approve(address(book),200e6);book.addInsurance(200e6);
        uint32 small=create(address(0x4001),110e6);quote(alice,0,1000,0,1);take(small,true,1000,1,8);
        oracle.set(800,800);vm.prank(CAROL);book.liquidate(small,carol,1);
        require(book.badDebt()==0 && book.insurance()>0 && book.insurance()<200e6);conservation();
    }
    function testUnsafeTakerRollsBackMakerFillAndNonce() public {
        uint32 small=create(address(0x4001),1e6);quote(alice,0,1001,0,1);
        uint256 command=tcmd(1,true,1001,1,8,false);
        vm.prank(address(0x4001));vm.expectRevert(MachineBook.Risk.selector);book.take(small,command);
        require(qty(alice*2+1)==1 && nonce(small)==0 && pos(alice)==0);conservation();
    }
    function testUnsafeLiquidatorRollsBackVictim() public {
        uint32 small=create(address(0x4001),110e6);uint32 broke=create(address(0x4002),1);
        quote(alice,0,1000,0,1);take(small,true,1000,1,8);oracle.set(800,800);
        vm.prank(address(0x4002));vm.expectRevert(MachineBook.Risk.selector);book.liquidate(small,broke,1);
        require(pos(small)==1 && book.badDebt()==0);conservation();
    }
    function testMaximumTickAndBidAskWordRemoval() public {
        oracle.set(65534,65534);quote(alice,65533,65535,1,1);take(bob,true,65535,1,8);
        require(book.best(1)==0 && book.best(0)==65533);vm.prank(ALICE);book.cancel(alice);require(book.roots(0)==0);conservation();
    }
    function testUnauthorizedGuardianAndOwner() public {
        vm.prank(BOB);vm.expectRevert(MachineBook.Unauthorized.selector);book.setPaused(true);
        vm.prank(BOB);vm.expectRevert(MachineBook.Unauthorized.selector);book.setSession(alice,AGENT,uint40(block.timestamp+100),1,1,1);
    }
    function testOldSessionCannotReplayAfterSameKeyRenewal() public {
        vm.prank(ALICE);book.setSession(alice,AGENT,uint40(block.timestamp+1000),10,1e12,1);
        uint256 old=qcmd(1,999,1001,1,1)|(uint256(1)<<200);
        vm.prank(ALICE);book.setSession(alice,AGENT,uint40(block.timestamp+1000),10,1e12,1);
        vm.prank(AGENT);vm.expectRevert(MachineBook.Unauthorized.selector);book.replace(alice,old);
        uint256 fresh=qcmd(1,999,1001,1,1)|(uint256(2)<<200);vm.prank(AGENT);book.replace(alice,fresh);
    }
    function testCancelBurnsInflightAndBoundedPipeline() public {
        uint256 old=qcmd(1,999,1001,1,1);vm.prank(ALICE);book.cancel(alice);
        vm.prank(ALICE);vm.expectRevert(MachineBook.Sequence.selector);book.replace(alice,old);
        vm.prank(ALICE);book.cancelThrough(alice,5);
        old=qcmd(4,999,1001,1,1);vm.prank(ALICE);vm.expectRevert(MachineBook.Sequence.selector);book.replace(alice,old);
        quote(alice,999,1001,1,1);require(nonce(alice)==6);
        vm.prank(ALICE);vm.expectRevert(MachineBook.Sequence.selector);book.cancelThrough(alice,type(uint64).max);
    }
    function testFIFOAndPartialFill() public {
        quote(alice,999,1001,5,5);quote(carol,998,1001,7,7);
        require(take(bob,true,1001,6,8)==6);
        require(pos(alice)==-5 && pos(carol)==-1 && pos(bob)==6);
        require(qty(carol*2+1)==6 && book.best(1)==1001);conservation();
    }
    function testAmendDecreaseKeepsPriorityIncreaseLosesPriority() public {
        quote(alice,0,1001,0,5);quote(carol,0,1001,0,5);
        quote(alice,0,1001,0,4);take(bob,true,1001,1,8);require(pos(alice)==-1);
        quote(alice,0,1001,0,7);take(bob,true,1001,1,8);require(pos(carol)==-1);conservation();
    }
    function testReplaceAtomicOnCross() public {
        quote(alice,999,1001,2,2);quote(bob,998,1002,2,2);
        uint256 c=qcmd(nonce(bob)+1,1001,1003,2,2);
        vm.prank(BOB);vm.expectRevert(MachineBook.Invalid.selector);book.replace(bob,c);
        require(qty(bob*2)==2 && book.best(0)==999);require(nonce(bob)==1);
    }
    function testNonceReplayAndHighBits() public {
        uint256 c=qcmd(1,999,1001,2,2);vm.prank(ALICE);book.replace(alice,c);
        vm.prank(ALICE);vm.expectRevert(MachineBook.Sequence.selector);book.replace(alice,c);
        vm.prank(ALICE);vm.expectRevert(MachineBook.Invalid.selector);book.replace(alice,c|1<<240);
    }
    function testNoSelfTradeAcrossAccounts() public {
        uint32 other=create(ALICE,1e12);quote(alice,0,1001,0,4);
        require(take(other,true,1001,3,8)==0);require(pos(other)==0 && qty(alice*2+1)==0);conservation();
    }
    function testExpiredHeadConsumesStep() public {
        quote(alice,0,1001,0,4);vm.warp(block.timestamp+10);quote(carol,0,1002,0,4);
        vm.warp(block.timestamp+41);
        require(take(bob,true,1002,1,1)==0);require(take(bob,true,1002,1,1)==1);require(pos(carol)==-1);
    }
    function testBoundedSweepAndCleanup() public {
        quote(alice,0,1001,0,1);quote(carol,0,1002,0,1);
        require(take(bob,true,1002,2,1)==1);require(book.best(1)==1002);
        vm.warp(block.timestamp+51);require(book.prune(1,1)==1);require(book.best(1)==0);
    }
    function testSessionCannotWithdrawOrExceedBudget() public {
        vm.prank(ALICE);book.setSession(alice,AGENT,uint40(block.timestamp+1000),10,1001e6,3);
        quote(bob,0,1001,0,3);uint256 c=tcmd(1,true,1001,1,8,false)|(uint256(1)<<162);
        vm.prank(AGENT);book.take(alice,c);
        c=tcmd(2,true,1001,1,8,false)|(uint256(1)<<162);vm.prank(AGENT);vm.expectRevert(MachineBook.Risk.selector);book.take(alice,c);
        vm.prank(AGENT);vm.expectRevert(MachineBook.Unauthorized.selector);book.withdraw(alice,1,AGENT);conservation();
    }
    function testMakerSessionBudgetChargedByFillAndRevocationCancels() public {
        vm.prank(ALICE);book.setSession(alice,AGENT,uint40(block.timestamp+1000),10,1001e6,1);
        uint256 c=qcmd(1,0,1001,0,2)|(uint256(1)<<200);vm.prank(AGENT);book.replace(alice,c);
        take(bob,true,1001,1,8);
        require(take(carol,true,1001,1,8)==0 && qty(alice*2+1)==0);
        vm.prank(ALICE);book.revokeSession(alice);
        c=qcmd(2,999,1001,1,1);vm.prank(AGENT);vm.expectRevert(MachineBook.Unauthorized.selector);book.replace(alice,c);conservation();
    }
    function testSessionScopeExpiryAndCap() public {
        vm.prank(ALICE);book.setSession(alice,AGENT,uint40(block.timestamp+100),2,1e12,1);
        uint256 c=qcmd(1,999,1001,3,3)|(uint256(1)<<200);vm.prank(AGENT);vm.expectRevert(MachineBook.Risk.selector);book.replace(alice,c);
        c=tcmd(1,true,1001,1,8,false)|(uint256(1)<<162);vm.prank(AGENT);vm.expectRevert(MachineBook.Unauthorized.selector);book.take(alice,c);
        vm.warp(block.timestamp+51);c=qcmd(1,999,1001,1,1)|(uint256(1)<<200);vm.prank(AGENT);vm.expectRevert(MachineBook.Unauthorized.selector);book.replace(alice,c);
    }
    function testMarginReservationsBlockWithdraw() public {
        uint32 small=create(address(0x4001),110e6);quote(small,999,1001,1,1);
        vm.prank(address(0x4001));vm.expectRevert(MachineBook.Risk.selector);book.withdraw(small,20e6,address(0x4001));
        vm.prank(address(0x4001));book.cancel(small);vm.prank(address(0x4001));book.withdraw(small,110e6,address(0x4001));conservation();
    }
    function testFundingZeroSumAndNoRetroactiveNewRate() public {
        quote(alice,0,1000,0,10);take(bob,true,1000,10,8);
        oracle.set(1000,1010);book.syncFunding();int128 before=book.fundingIndex();int128 v=book.fundingVelocity();
        vm.warp(block.timestamp+60);oracle.set(1000,990);book.syncFunding();
        require(book.fundingIndex()==before+v*60 && book.fundingVelocity()<0);
        quote(alice,0,0,0,0);quote(bob,0,0,0,0);conservation();
    }
    function testFundingGapCapped() public {
        oracle.set(1000,1010);book.syncFunding();int128 v=book.fundingVelocity();vm.warp(block.timestamp+10000);book.syncFunding();require(book.fundingIndex()==v*300);
    }
    function testUnsafeMakerSkippedAfterShock() public {
        uint32 small=create(address(0x4001),110e6);quote(small,1000,0,1,0);oracle.set(900,900);
        require(take(bob,false,900,1,8)==0);require(qty(small*2)==0);conservation();
    }
    function testLiquidationInsuranceAndBadDebtRecovery() public {
        uint32 small=create(address(0x4001),110e6);quote(alice,0,1000,0,1);take(small,true,1000,1,8);
        oracle.set(800,800);vm.prank(CAROL);book.liquidate(small,carol,1);
        require(pos(small)==0 && cash(small)==0 && book.badDebt()>0);conservation();
        vm.prank(ALICE);vm.expectRevert(MachineBook.Halted.selector);book.withdraw(alice,1,ALICE);
        uint256 c=qcmd(nonce(alice)+1,799,801,1,1);vm.prank(ALICE);vm.expectRevert(MachineBook.Halted.selector);book.replace(alice,c);
        uint128 debt=book.badDebt();token.mint(address(this),debt);token.approve(address(book),debt);book.addInsurance(debt);
        require(book.badDebt()==0);conservation();
    }
    function testPartialLiquidationAndHealthyTargetRejection() public {
        uint32 small=create(address(0x4001),210e6);quote(alice,0,1000,0,2);take(small,true,1000,2,8);
        vm.prank(CAROL);vm.expectRevert(MachineBook.Risk.selector);book.liquidate(small,carol,1);
        oracle.set(940,940);vm.prank(CAROL);book.liquidate(small,carol,1);require(pos(small)==1);conservation();
    }
    function testReduceOnlyCannotFlip() public {
        quote(alice,999,1001,5,5);take(bob,true,1001,2,8);
        uint256 c=tcmd(nonce(bob)+1,false,999,3,8,true);vm.prank(BOB);vm.expectRevert(MachineBook.Risk.selector);book.take(bob,c);
        c=tcmd(nonce(bob)+1,false,999,2,8,true);vm.prank(BOB);book.take(bob,c);require(pos(bob)==0);conservation();
    }
    function testPauseAndOracleOutageStillAllowCancellation() public {
        quote(alice,999,1001,2,2);book.setPaused(true);oracle.setStale(true);
        vm.prank(ALICE);book.cancel(alice);require(book.best(0)==0 && book.best(1)==0);
        uint256 c=qcmd(2,999,1001,1,1);vm.prank(ALICE);vm.expectRevert(MachineBook.Halted.selector);book.replace(alice,c);
        vm.prank(ALICE);vm.expectRevert();book.withdraw(alice,1,ALICE);
    }
    function testExactTransferAndReentrancy() public {
        token.mint(ALICE,100);token.setFee(1);vm.prank(ALICE);vm.expectRevert(MachineBook.Transfer.selector);book.deposit(alice,100);
        token.setFee(0);token.hook(address(book),abi.encodeCall(book.open,()));vm.prank(ALICE);book.deposit(alice,100);
        require(!token.callbackSucceeded() && book.accountCount()==3);conservation();
    }
    function testBitmapWordBoundaries() public {
        oracle.set(256,256);quote(alice,255,257,1,1);quote(bob,256,258,1,1);
        require(book.best(0)==256 && book.best(1)==257);vm.prank(BOB);book.cancel(bob);require(book.best(0)==255);
        vm.prank(ALICE);book.cancel(alice);require(book.roots(0)==0 && book.roots(1)==0);
    }
    function testFuzzConservationWithPartialFills(uint32 a,uint32 b,bool buy) public {
        a=a%100+1;b=b%a+1;quote(alice,999,1001,a,a);take(bob,buy,buy?1001:999,b,8);
        vm.warp(block.timestamp+30);oracle.set(1000,1001);book.syncFunding();quote(carol,998,1002,3,3);conservation();
    }
    function testFuzzTwoSidedReservationGeometry(uint32 b,uint32 s,uint32 x,uint32 y) public {
        b=b%100+1;s=s%100+1;x=x%(b+1);y=y%(s+1);quote(alice,999,1001,b,s);
        if(x>0)take(bob,false,999,x,8);if(y>0)take(carol,true,1001,y,8);conservation();
    }
}
contract OracleTest is Base {
    FeedMock private index;FeedMock private mark;FeedMock private seq;ChainlinkOracle private adapter;
    function setUp() public override {
        vm.warp(10000);index=new FeedMock();mark=new FeedMock();seq=new FeedMock();seq.set(0,10000,1,1,1);
        adapter=new ChainlinkOracle(address(index),address(mark),address(seq),1e8,60,3600);
    }
    function testOracleValid() public view { (uint16 p,uint16 m)=adapter.read();require(p==1000 && m==1000); }
    function testOracleStaleFutureIncompleteZeroAndRange() public {
        index.set(1000e8,9939,1,1,1);vm.expectRevert();adapter.read();
        index.set(1000e8,10001,1,1,1);vm.expectRevert();adapter.read();
        index.set(1000e8,10000,1,2,1);vm.expectRevert();adapter.read();
        index.set(0,10000,1,1,1);vm.expectRevert();adapter.read();
        index.set(65536e8,10000,1,1,1);vm.expectRevert();adapter.read();
    }
    function testSequencerDownUninitializedAndGrace() public {
        seq.set(1,10000,1,1,1);vm.expectRevert();adapter.read();
        seq.set(0,10000,0,1,1);vm.expectRevert();adapter.read();
        seq.set(0,10000,9999,1,1);vm.expectRevert();adapter.read();
    }
}
