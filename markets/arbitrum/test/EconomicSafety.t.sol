// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;
import './Support.sol';
contract EconomicSafety is Base {
    function _bankruptLong() private returns(uint32 victim) {
        victim=create(address(0x4001),110e6);quote(alice,0,1000,0,1);take(victim,true,1000,1,8);oracle.set(800,800);
    }
    function testLatentBankruptcyBlocksWinnerBeforeKeeperArrives() public {
        uint32 victim=_bankruptLong();require(book.equity(victim,800)<0 && book.badDebt()==0);
        uint128 amount=uint128(uint256(book.equity(alice,800))-80e6);
        vm.prank(ALICE);vm.expectRevert(MachineBook.Halted.selector);book.withdraw(alice,amount,ALICE);
        require(!book.solventAt(800));conservation();
    }
    function testTopupRestoresSolvencyWithoutGlobalScan() public {
        uint32 victim=_bankruptLong();token.mint(BOB,100e6);vm.prank(BOB);book.deposit(victim,100e6);
        require(book.solventAt(800));vm.prank(ALICE);book.withdraw(alice,1,ALICE);conservation();
    }
    function testBankruptLiquidationCannotMintLiquidatorProfitFromInsurance() public {
        uint32 victim=_bankruptLong();int256 before=book.equity(carol,800);
        vm.prank(CAROL);book.liquidate(victim,carol,1);
        require(book.equity(carol,800)==before && book.badDebt()==90e6);conservation();
    }
    function testPositiveEquityLiquidationRewardCannotExceedEquity() public {
        uint32 victim=create(address(0x4001),110e6);quote(alice,0,1000,0,1);take(victim,true,1000,1,8);
        oracle.set(895,895);int256 equity=book.equity(victim,895);require(equity==4500000);
        int256 before=book.equity(carol,895);vm.prank(CAROL);book.liquidate(victim,carol,1);
        require(book.equity(carol,895)-before==equity && cash(victim)==0 && book.badDebt()==0);conservation();
    }
    function testShortBankruptcyAlsoBlocksEarlyWinner() public {
        uint32 victim=create(address(0x4001),110e6);quote(alice,1000,0,1,0);take(victim,false,1000,1,8);
        oracle.set(1200,1200);require(book.equity(victim,1200)<0 && !book.solventAt(1200));
        vm.prank(ALICE);vm.expectRevert(MachineBook.Halted.selector);book.withdraw(alice,1,ALICE);
    }
    function testFundingAloneCanActivateLatentLossGate() public {
        uint32 victim=create(address(0x4001),110e6);quote(alice,0,1000,0,1);take(victim,true,1000,1,8);
        oracle.set(1000,1010);book.syncFunding();
        // Settle bounded intervals repeatedly: each accrues under 0.1%/hour cap.
        for(uint256 i;i<1400;i++){vm.warp(vm.getBlockTimestamp()+300);book.syncFunding();}
        require(book.equity(victim,1000)<0 && !book.solventAt(1000));
        vm.prank(ALICE);vm.expectRevert(MachineBook.Halted.selector);book.withdraw(alice,1,ALICE);
    }
    function testUnsettledProjectedFundingAlsoBlocksWithdrawal() public {
        uint32 victim=create(address(0x4001),100500001);quote(alice,0,1000,0,1);take(victim,true,1000,1,8);
        oracle.set(900,910);book.syncFunding();require(book.equity(victim,900)==1);
        vm.warp(vm.getBlockTimestamp()+300);
        require(book.equity(victim,900)==-74999 && !book.solventAt(900));
        vm.prank(ALICE);vm.expectRevert(MachineBook.Halted.selector);book.withdraw(alice,1,ALICE);
    }
    function testRiskSlotsReusedAfterFlatCloseout() public {
        for(uint256 i;i<10;i++){
            quote(alice,999,1001,2,2);take(bob,true,1001,1,8);take(bob,false,999,1,8);
            (uint16 active,uint16 high,)=book.riskCapacity();require(active==0 && high==2);
            require(book.solventAt(1) && book.solventAt(65535));
        }conservation();
    }
    function testFullMarketCanCloseIntoPreviouslyFlatCounterparty() public {
        // Setup spans thousands of normal actions. Only the final operation is
        // gas-metered; this test checks capacity behavior, not throughput.
        vm.pauseGasMetering();quote(alice,0,1000,0,4095);uint32 first;
        for(uint32 i;i<4095;i++){
            uint32 id=create(address(uint160(0x10000+i)),200e6);
            if(i==0)first=id;take(id,true,1000,1,1);
        }
        (uint16 active,,)=book.riskCapacity();require(active==4096);
        quote(carol,1000,0,1,0);vm.resumeGasMetering();
        take(first,false,1000,1,1);
        require(pos(first)==0 && pos(carol)==1);
        (active,,)=book.riskCapacity();require(active==4096);conservation();
    }
    function _tiny() private {
        MachineBook.Config memory c=config();c.tickValue=1;book=new MachineBook(address(token),address(oracle),address(this),c);
        alice=create(ALICE,1e6);bob=create(BOB,1e6);carol=create(CAROL,1e6);
    }
    function testSameNotionalPaysSameFeeAcrossCounterpartyFragmentation() public {
        _tiny();quote(alice,0,1000,0,2);take(bob,true,1000,2,8);uint128 whole=book.insurance();
        _tiny();quote(alice,0,1000,0,1);quote(carol,0,1000,0,1);take(bob,true,1000,2,8);
        require(whole==1 && book.insurance()==whole);conservation();
    }
    function testFuzzFragmentationFeeInvariant(uint16 tick,uint8 clips,uint8 fee) public {
        tick=uint16(uint256(tick)%60000+100);uint32 count=uint32(clips%16+1);fee=fee%100+1;
        MachineBook.Config memory c=config();c.tickValue=1;c.takerFeeBps=fee;
        oracle.set(tick,tick);book=new MachineBook(address(token),address(oracle),address(this),c);
        uint32 taker=create(BOB,1e12);
        for(uint32 i;i<count;i++){uint32 maker=create(address(uint160(0x7000+i)),1e12);quote(maker,0,tick,0,1);}
        take(taker,true,tick,count,32);
        require(book.insurance()==(uint256(tick)*count*fee+9999)/10000);conservation();
    }
}
contract SolvencyHarness {
    using SolvencyIndex for SolvencyIndex.State;
    SolvencyIndex.State private state;
    mapping(uint32 => SolvencyIndex.Account) private ledger;
    function set(uint32 id,int48 base,int128 cash,int128 funding) external {
        ledger[id].base=base;ledger[id].cash=cash;ledger[id].funding=funding;state.update(ledger,id);
    }
    function solvent(int256 m) external view returns(bool){return state.solvent(m);}
    function counts() external view returns(uint16,uint16){return(state.active,state.highWater);}
    function check() external view {
        require(state.active==state.count[0]+state.count[1]);
        for(uint16 side;side<2;side++){
            uint16 offset=side<<15;
            for(uint16 i=1;i<=state.count[side];i++){
                uint256 node=state.nodes[offset+i];require(node!=0);
                require(ledger[uint32(node)].riskLocation==offset+i,'heap location');
                if(i>1)require(state.nodes[offset+(i>>1)]>=node,'heap order');
            }
            require(state.nodes[offset+state.count[side]+1]==0,'removed tail');
        }
    }
}
contract SolvencyMathTest {
    Vm private constant vm=Vm(address(uint160(uint256(keccak256('hevm cheat code')))));
    function testExtremeBarrierUsesMoreThanSigned129Bits() public {
        SolvencyHarness h=new SolvencyHarness();h.set(1,1,type(int128).min,type(int128).min);
        int256 threshold=int256(uint256(1)<<128);
        require(!h.solvent(threshold-1) && h.solvent(threshold));h.check();
    }
    function testFuzzExactSignedThresholds(int48 base,int128 cash,int128 checkpoint,int128 mark) public {
        // Independent full-precision direct equity, including extreme signed inputs.
        SolvencyHarness h=new SolvencyHarness();h.set(1,base,cash,checkpoint);
        bool expected=base==0 || int256(cash)+int256(base)*(int256(mark)+int256(checkpoint))>=0;
        require(h.solvent(mark)==expected,'affine threshold');
    }
    function testFuzzTwoSidesUpdateRemoval(int64 b,int64 s,int128 bc,int128 sc,int64 mark) public {
        b=int64(uint64(b)%1000000+1);s=-int64(uint64(s)%1000000+1);
        SolvencyHarness h=new SolvencyHarness();h.set(1,int48(b),bc,17);h.set(2,int48(s),sc,-31);
        bool a=int256(bc)+int256(b)*(int256(mark)+17)>=0;
        bool z=int256(sc)+int256(s)*(int256(mark)-31)>=0;
        require(h.solvent(mark)==(a&&z));h.set(1,0,0,0);require(h.solvent(mark)==z);
        h.set(2,0,0,0);require(h.solvent(mark));(uint16 active,uint16 high)=h.counts();require(active==0 && high==2);
    }
    function testFullCapacityUpdateFlipRemoveAndReuse() public {
        SolvencyHarness h=new SolvencyHarness();
        for(uint32 i=1;i<=4096;i++)h.set(i,1,-int128(uint128(i)),0);
        h.check();require(!h.solvent(4095) && h.solvent(4096));
        vm.expectRevert(SolvencyIndex.PositionCapacity.selector);h.set(4097,1,0,0);
        h.set(4096,-1,10000,0);h.check();require(h.solvent(4095) && !h.solvent(4094));
        h.set(2048,0,0,0);h.set(4097,1,-6000,0);h.check();require(!h.solvent(5999) && h.solvent(6000));
        for(uint32 i=1;i<=4097;i++)h.set(i,0,0,0);
        h.check();(uint16 active,uint16 high)=h.counts();require(active==0 && high==4096 && h.solvent(0));
    }
    function testFuzzRepeatedHeapMutations(uint256 seed) public {
        SolvencyHarness h=new SolvencyHarness();
        int64[32] memory bases;int128[32] memory cashes;int128[32] memory checkpoints;
        for(uint256 step;step<96;step++){
            seed=uint256(keccak256(abi.encode(seed)));uint32 id=uint32(seed%32);
            int64 base=int64(uint64(seed>>32)%101)-50;
            int128 cash=int128(uint128(seed>>64)%100000)-50000;
            int128 checkpoint=int128(uint128(seed>>128)%1000)-500;
            bases[id]=base;cashes[id]=cash;checkpoints[id]=checkpoint;h.set(id+1,int48(base),cash,checkpoint);
            int256 mark=int256((seed>>192)%2000)-1000;bool expected=true;
            for(uint32 i;i<32;i++)if(bases[i]!=0 && int256(cashes[i])+int256(bases[i])*(mark+checkpoints[i])<0)expected=false;
            require(h.solvent(mark)==expected,'mutation envelope');h.check();
        }
    }
}
