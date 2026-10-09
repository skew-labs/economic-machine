// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// Exact affine equity envelope. Two indexed max heaps, bounded at 4096 positions.
/// M = indexQuote - globalFunding. Equity = cash + base * (M + checkpoint).
/// Each packed node is ordered signed barrier192 + account32. A heap path is <=12.
library SolvencyIndex {
    error PositionCapacity();
    uint16 internal constant CAPACITY = 4096;
    uint192 private constant SIGN = 1 << 191;
    // Cash128 + position48 + nonce64 + heap-location16 is one 256-bit word.
    // Market cap <=1e9 and one action <=1e9 lots; temporary positions fit 32
    // signed bits. 48 bits keeps headroom, with checked casts in the venue.
    struct Account { int128 cash; int48 base; uint64 nonce; uint16 riskLocation; int128 funding; }
    struct State {
        mapping(uint16 => uint256) nodes; // long positions 1..4096; short = 32768 + position.
        uint16[2] count;
        uint16 highWater;
        uint16 active;
    }
    function update(State storage self, mapping(uint32 => Account) storage ledger, uint32 account) internal {
        Account storage a = ledger[account];
        int64 base = a.base; int128 cash = a.cash; int128 checkpoint = a.funding;
        uint16 location = a.riskLocation;
        if (base == 0) { if (location != 0) _remove(self, ledger, location); return; }
        uint16 side = base < 0 ? 1 : 0;
        uint16 offset = side << 15;
        if (location != 0 && location >> 15 != side) { _remove(self, ledger, location); location = 0; }
        int256 barrier;
        if (base > 0) {
            int256 numerator = -int256(cash); int256 size = int256(base);
            barrier = numerator / size;
            if (numerator > 0 && numerator % size != 0) ++barrier;
            barrier -= int256(checkpoint);
        } else {
            int256 numerator = int256(cash); int256 size = -int256(base);
            int256 floor = numerator / size;
            if (numerator < 0 && numerator % size != 0) --floor;
            barrier = -floor + int256(checkpoint);
        }
        // int128 cash/checkpoint and nonzero int64 base imply a signed 130-bit
        // barrier. No lossy cast: 192 bits leaves 62 bits of explicit headroom.
        uint256 packed = (uint256(uint192(int192(barrier)) ^ SIGN) << 32) | account;
        if (location == 0) {
            if (self.active == CAPACITY) revert PositionCapacity();
            ++self.active; if (self.active > self.highWater) self.highWater = self.active;
            location = offset + ++self.count[side];
        } else if (self.nodes[location] == packed) return;
        _fix(self, ledger, location, packed, offset, self.count[side]);
    }
    function _remove(State storage self, mapping(uint32 => Account) storage ledger, uint16 location) private {
        uint16 side = location >> 15; uint16 offset = side << 15;
        uint16 tail = offset + self.count[side];
        delete ledger[uint32(self.nodes[location])].riskLocation;
        uint256 replacement = self.nodes[tail]; delete self.nodes[tail];
        --self.count[side]; --self.active;
        if (location != tail) _fix(self, ledger, location, replacement, offset, self.count[side]);
    }
    function _fix(State storage self, mapping(uint32 => Account) storage ledger, uint16 location, uint256 value, uint16 offset, uint16 count) private {
        uint16 start = location;
        while (location > offset + 1) {
            uint16 parent = offset + ((location - offset) >> 1);
            uint256 above = self.nodes[parent]; if (value <= above) break;
            self.nodes[location] = above; ledger[uint32(above)].riskLocation = location; location = parent;
        }
        if (location == start) {
            while (true) {
                uint16 child = (location - offset) << 1; if (child > count) break;
                child += offset;
                uint256 below = self.nodes[child];
                if (child < offset + count && self.nodes[child + 1] > below) { ++child; below = self.nodes[child]; }
                if (value >= below) break;
                self.nodes[location] = below; ledger[uint32(below)].riskLocation = location; location = child;
            }
        }
        self.nodes[location] = value; ledger[uint32(value)].riskLocation = location;
    }
    function solvent(State storage self, int256 adjustedMark) internal view returns (bool) {
        uint256 longRoot = self.nodes[1]; uint256 shortRoot = self.nodes[32769];
        return (longRoot == 0 || int256(int192(uint192(longRoot >> 32) ^ SIGN)) <= adjustedMark) &&
            (shortRoot == 0 || int256(int192(uint192(shortRoot >> 32) ^ SIGN)) <= -adjustedMark);
    }
}
