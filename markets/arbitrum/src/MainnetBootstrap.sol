// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;
import './MachineBookMainnet.sol';
import './ChainlinkOracle.sol';

/// One transaction creates the fixed ETH/USDC experiment, already paused.
/// No proxy, upgrade path, arbitrary calls or collateral custody here.
contract MainnetBootstrap {
    address public immutable oracle;
    address public immutable market;
    constructor() {
        require(block.chainid == 42161, 'Arbitrum One only');
        oracle = address(new ChainlinkOracle(
            0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612,
            0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612,
            0xFdB631F5EE196F0ed6FAa767959853A9F217697D,
            1e7, 3600, 3600
        ));
        market = address(new MachineBookMainnet(
            0xaf88d065e77c8cC2239327C5EDb3A432268e5831,
            oracle, msg.sender,
            MachineBook.Config(100, 100, 2000, 1000, 5, 50, 500, 10, 30, 300)
        ));
    }
}
