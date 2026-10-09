// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;
import './MachineBook.sol';

/// Same matching/accounting runtime, deployed with admission paused atomically.
contract MachineBookMainnet is MachineBook {
    constructor(address token, address feed, address guard, Config memory c)
        MachineBook(token, feed, guard, c)
    {
        require(block.chainid == 42161, 'Arbitrum One only');
        paused = true;
        emit Pause(true);
    }
}
