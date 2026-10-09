// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

library BitScan {
    // Input must be nonzero. Seven bounded branches; never scan empty ticks.
    function msb(uint256 x) internal pure returns (uint16 r) {
        if (x >= 1 << 128) { x >>= 128; r += 128; }
        if (x >= 1 << 64) { x >>= 64; r += 64; }
        if (x >= 1 << 32) { x >>= 32; r += 32; }
        if (x >= 1 << 16) { x >>= 16; r += 16; }
        if (x >= 1 << 8) { x >>= 8; r += 8; }
        if (x >= 1 << 4) { x >>= 4; r += 4; }
        if (x >= 1 << 2) { x >>= 2; r += 2; }
        if (x >= 1 << 1) r += 1;
    }
    function lsb(uint256 x) internal pure returns (uint16) {
        unchecked { return msb(x & (~x + 1)); }
    }
}
