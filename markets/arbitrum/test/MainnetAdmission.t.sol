// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;
import './Support.sol';
import '../src/MachineBookMainnet.sol';
interface ChainVm { function chainId(uint256) external; }
contract MainnetAdmissionTest is Base {
    function testAtomicPausedAdmissionAndOwnerRecovery() public {
        ChainVm(address(vm)).chainId(42161);
        MachineBookMainnet venue = new MachineBookMainnet(address(token), address(oracle), address(this), config());
        require(venue.paused() && venue.guardian() == address(this));
        vm.startPrank(ALICE);
        uint32 id = venue.open(); token.mint(ALICE, 1000000000000);
        token.approve(address(venue), 1000000000000); venue.deposit(id, 1000000000000);
        vm.expectRevert(MachineBook.Halted.selector);
        venue.replace(id, qcmd(1,999,1001,1,1));
        venue.withdraw(id,1000000000000,ALICE);
        vm.stopPrank();
        require(token.balanceOf(address(venue)) == 0);
        vm.prank(ALICE); vm.expectRevert(MachineBook.Unauthorized.selector); venue.setPaused(false);
        venue.setPaused(false); require(!venue.paused());
    }
    function testRejectWrongChain() public {
        ChainVm(address(vm)).chainId(421614);
        vm.expectRevert();
        new MachineBookMainnet(address(token), address(oracle), address(this), config());
    }
}
