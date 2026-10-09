// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;
import '../test/Support.sol';
contract ArbitrumForkTest is Base {
    ChainlinkOracle private live;
    uint16 private indexTick;
    function setUp() public override {
        require(block.chainid==42161,'Arbitrum One fork required');
        address eth=0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612;
        address seq=0xFdB631F5EE196F0ed6FAa767959853A9F217697D;
        live=new ChainlinkOracle(eth,eth,seq,1e8,3600,3600);
        (indexTick,)=live.read();require(indexTick>100 && indexTick<65000);
        token=new Token();book=new MachineBook(address(token),address(live),address(this),config());
        alice=create(ALICE,1e12);bob=create(BOB,1e12);
    }
    function testLiveFeedForkQuoteFillWithdrawConservation() public {
        quote(alice,indexTick-1,indexTick+1,2,2);
        require(take(bob,true,indexTick+1,1,8)==1);
        vm.prank(ALICE);book.cancel(alice);
        quote(alice,indexTick,0,1,0);require(take(bob,false,indexTick,1,8)==1);
        require(pos(alice)==0 && pos(bob)==0);conservation();
        uint128 balance=uint128(cash(alice));vm.prank(ALICE);book.withdraw(alice,balance,ALICE);
        balance=uint128(cash(bob));vm.prank(BOB);book.withdraw(bob,balance,BOB);
        require(token.balanceOf(address(book))==book.insurance());
    }
    function testLiveFeedStalesOnForkClockAdvance() public {
        vm.warp(vm.getBlockTimestamp()+3601);vm.expectRevert(ChainlinkOracle.InvalidFeed.selector);live.read();
    }
}
