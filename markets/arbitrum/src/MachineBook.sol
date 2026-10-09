// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;
import './Interfaces.sol';
import './BitScan.sol';
import './SolvencyIndex.sol';

/// One isolated perpetual market. Lot/tick value is in raw collateral units.
/// Two reusable order IDs per account; 256x256 sparse price bitmap per side.
contract MachineBook {
    using BitScan for uint256;
    using SolvencyIndex for SolvencyIndex.State;
    SolvencyIndex.State private solvencyIndex;
    error Invalid(); error Unauthorized(); error Sequence(); error Risk();
    error Halted(); error Oracle(); error Transfer(); error Reentrant();
    struct Order { uint32 prev; uint32 next; uint32 lots; uint16 tick; uint40 expiry; uint32 epoch; }
    struct Level { uint32 head; uint32 tail; }
    struct Session { address key; uint40 expiry; uint32 epoch; uint32 maxPosition; uint128 remaining; uint8 permissions; }
    struct TakeScratch { uint256 notional; uint256 fee; int256 initialEquity; int64 initial; }
    struct Config {
        uint64 tickValue; uint32 maxPosition; uint16 initialBps; uint16 maintenanceBps;
        uint16 takerFeeBps; uint16 liquidationBps; uint16 priceBandBps; uint16 fundingCapBps;
        uint32 quoteTTL; uint32 fundingHorizon;
    }
    ICollateral public immutable collateral;
    IMarketOracle public immutable oracle;
    address public immutable guardian;
    uint64 public immutable tickValue;
    uint32 public immutable maxPosition;
    uint16 public immutable initialBps;
    uint16 public immutable maintenanceBps;
    uint16 public immutable takerFeeBps;
    uint16 public immutable liquidationBps;
    uint16 public immutable priceBandBps;
    uint16 public immutable fundingCapBps;
    uint32 public immutable quoteTTL;
    uint32 public immutable fundingHorizon;
    mapping(uint32 => SolvencyIndex.Account) private ledger;
    mapping(uint32 => address) public owners;
    mapping(uint32 => Session) public sessions;
    mapping(uint32 => Order) public orders;
    mapping(uint32 => Level) public levels; // side << 16 | tick; bid=0, ask=1.
    mapping(uint16 => uint256) public words; // side << 8 | tick >> 8.
    uint256[2] public roots;
    uint32 public accountCount;
    uint128 public insurance;
    uint128 public badDebt;
    int128 public fundingIndex;
    int128 public fundingVelocity; // Raw collateral / lot / second, integer, signed.
    uint40 public fundingTime;
    bool public paused;
    uint256 transient entered; // EIP-1153, supported on Arbitrum since ArbOS 20.
    event Opened(uint32 indexed account, address indexed owner);
    event CollateralChanged(uint32 indexed account, int256 delta);
    event SessionChanged(uint32 indexed account, address key, uint32 epoch, uint40 expiry, uint32 maxPosition, uint128 budget, uint8 permissions);
    event Quote(uint32 indexed account, uint256 command);
    event Cancelled(uint32 indexed account);
    event Fill(uint32 indexed maker, uint32 indexed taker, bool takerBuy, uint32 lots, uint16 tick, uint256 fee);
    event Taken(uint32 indexed account, uint64 nonce, uint32 filled);
    event Liquidated(uint32 indexed target, uint32 indexed liquidator, uint32 lots, uint256 pricePerLot, uint256 debt);
    event LiquidationReward(uint32 indexed target, uint32 indexed liquidator, uint256 amount);
    event Funding(int128 index, int128 velocity, uint40 time);
    event InsuranceAdded(uint256 amount, uint128 insurance, uint128 badDebt);
    event Pause(bool value);
    modifier lock() { if (entered != 0) revert Reentrant(); entered = 1; _; entered = 0; }
    constructor(address token, address feed, address guard, Config memory c) {
        if (token.code.length == 0 || feed.code.length == 0 || guard == address(0) ||
            c.tickValue == 0 || c.tickValue > 1e12 || c.maxPosition == 0 || c.maxPosition > 1e9 ||
            c.initialBps < 100 || c.initialBps > 10000 || c.maintenanceBps == 0 ||
            c.maintenanceBps >= c.initialBps || c.takerFeeBps > 100 || c.liquidationBps == 0 ||
            c.liquidationBps >= c.maintenanceBps || c.priceBandBps == 0 || c.priceBandBps > 2000 ||
            c.fundingCapBps > 100 || c.quoteTTL == 0 || c.quoteTTL > 300 ||
            c.fundingHorizon == 0 || c.fundingHorizon > 3600 || ICollateral(token).decimals() != 6) revert Invalid();
        collateral = ICollateral(token); oracle = IMarketOracle(feed); guardian = guard;
        tickValue = c.tickValue; maxPosition = c.maxPosition; initialBps = c.initialBps;
        maintenanceBps = c.maintenanceBps; takerFeeBps = c.takerFeeBps; liquidationBps = c.liquidationBps;
        priceBandBps = c.priceBandBps; fundingCapBps = c.fundingCapBps;
        quoteTTL = c.quoteTTL; fundingHorizon = c.fundingHorizon;
        fundingTime = uint40(block.timestamp);
    }
    // Preserve the four-word account ABI; position widens to int64 for clients.
    function accounts(uint32 id) external view returns (int128 cash, int64 base, uint64 nonce, int128 funding) {
        SolvencyIndex.Account storage a = ledger[id]; return (a.cash, a.base, a.nonce, a.funding);
    }
    function open() external lock returns (uint32 id) {
        if (accountCount >= type(uint32).max / 2 - 1) revert Invalid();
        id = ++accountCount; owners[id] = msg.sender; ledger[id].funding = fundingIndex;
        emit Opened(id, msg.sender);
    }
    function setPaused(bool value) external lock {
        if (msg.sender != guardian) revert Unauthorized(); paused = value; emit Pause(value);
    }
    function deposit(uint32 id, uint128 amount) external lock {
        if (owners[id] == address(0) || amount == 0) revert Invalid();
        _receive(amount); _cash(ledger[id], int256(uint256(amount))); _updateRisk(id);
        emit CollateralChanged(id, int256(uint256(amount)));
    }
    function withdraw(uint32 id, uint128 amount, address to) external lock {
        _owner(id); if (badDebt != 0 || to == address(0) || to == address(this) || amount == 0) revert Halted();
        uint16 price = _sync(); _requireSolvent(price); _settle(ledger[id]);
        _cash(ledger[id], -int256(uint256(amount)));
        if (!_healthy(id, price, maxPosition)) revert Risk();
        _updateRisk(id);
        uint256 beforeVault = collateral.balanceOf(address(this));
        uint256 beforeTo = collateral.balanceOf(to);
        _token(abi.encodeCall(ICollateral.transfer, (to, uint256(amount))));
        if (collateral.balanceOf(address(this)) + amount != beforeVault || collateral.balanceOf(to) != beforeTo + amount)
            revert Transfer();
        emit CollateralChanged(id, -int256(uint256(amount)));
    }
    function addInsurance(uint128 amount) external lock {
        if (amount == 0) revert Invalid(); _receive(amount);
        uint128 covered = amount < badDebt ? amount : badDebt;
        badDebt -= covered; insurance += amount - covered;
        emit InsuranceAdded(amount, insurance, badDebt);
    }
    function setSession(uint32 id, address key, uint40 expiry, uint32 cap, uint128 budget, uint8 permissions) external lock {
        _owner(id);
        if (key == address(0) || key == msg.sender || expiry <= block.timestamp || expiry > block.timestamp + 7 days ||
            cap == 0 || cap > maxPosition || budget == 0 || permissions == 0 || permissions > 3) revert Invalid();
        _cancel(id); Session storage s = sessions[id]; uint32 epoch = s.epoch + 1;
        sessions[id] = Session(key, expiry, epoch, cap, budget, permissions);
        emit SessionChanged(id, key, epoch, expiry, cap, budget, permissions);
    }
    function revokeSession(uint32 id) external lock {
        _owner(id); _cancel(id); Session storage s = sessions[id];
        s.key = address(0); s.expiry = 0; s.epoch += 1; s.remaining = 0;
        emit SessionChanged(id, address(0), s.epoch, 0, 0, 0, 0);
    }
    function cancel(uint32 id) external lock {
        if (msg.sender != owners[id] && msg.sender != sessions[id].key) revert Unauthorized();
        ledger[id].nonce += 1; _cancel(id); // Burn the next in-flight command. Oracle independent.
    }
    function cancelThrough(uint32 id, uint64 throughNonce) external lock {
        if (msg.sender != owners[id] && msg.sender != sessions[id].key) revert Unauthorized();
        uint64 current = ledger[id].nonce;
        if (throughNonce <= current || uint256(throughNonce) > uint256(current) + 64 || throughNonce == type(uint64).max) revert Sequence();
        ledger[id].nonce = throughNonce; _cancel(id);
    }
    /// Bits [0:64] nonce, [64:104] expiry, [104:120] bid, [120:136] ask,
    /// [136:168] bid lots, [168:200] ask lots, [200:232] session epoch (owner=0).
    function replace(uint32 id, uint256 command) external lock {
        _active(); uint16 price = _sync(); _requireSolvent(price); _replace(id, command, price);
    }
    /// 1..32 records, each account ID big-endian 4 bytes + command 32 bytes.
    /// Same session caller, one oracle/funding sync, atomic nonce/risk validation.
    function replaceBatch(bytes calldata records) external lock {
        _active(); uint256 length = records.length;
        if (length == 0 || length % 36 != 0 || length > 32 * 36) revert Invalid();
        uint16 price = _sync(); _requireSolvent(price);
        for (uint256 offset; offset < length; offset += 36) {
            uint32 id; uint256 command;
            assembly ("memory-safe") {
                id := shr(224, calldataload(add(records.offset, offset)))
                command := calldataload(add(add(records.offset, offset), 4))
            }
            _replace(id, command, price);
        }
    }
    function _replace(uint32 id, uint256 command, uint16 p) private { if (command >> 232 != 0) revert Invalid();
        uint40 expiry = uint40(command >> 64);
        uint32 epoch = _auth(id, uint64(command), expiry, 1, uint32(command >> 200));
        uint16 bid = uint16(command >> 104); uint16 ask = uint16(command >> 120);
        uint32 bq = uint32(command >> 136); uint32 aq = uint32(command >> 168);
        if ((bid == 0) != (bq == 0) || (ask == 0) != (aq == 0) || (bq != 0 && aq != 0 && bid >= ask)) revert Invalid();
        _settle(ledger[id]);
        bool keepBid = _retain(id * 2, bid, bq, epoch);
        bool keepAsk = _retain(id * 2 + 1, ask, aq, epoch);
        if (bq != 0) { _band(bid, p); if (best(1) != 0 && bid >= best(1)) revert Invalid(); if (keepBid) { orders[id * 2].lots = bq; orders[id * 2].expiry = expiry; } else _insert(id * 2, bid, bq, expiry, epoch); }
        if (aq != 0) { _band(ask, p); if (best(0) != 0 && ask <= best(0)) revert Invalid(); if (keepAsk) { orders[id * 2 + 1].lots = aq; orders[id * 2 + 1].expiry = expiry; } else _insert(id * 2 + 1, ask, aq, expiry, epoch); }
        uint32 cap = epoch == 0 ? maxPosition : sessions[id].maxPosition;
        if (!_healthy(id, p, cap)) revert Risk();
        emit Quote(id, command);
    }
    /// Bits nonce64, expiry40, limit16, lots32, steps8, buy1, reduceOnly1, sessionEpoch32.
    /// IOC. Every visited node consumes a step, including stale/self/unsafe nodes.
    function take(uint32 id, uint256 command) external lock returns (uint32 filled) {
        _active(); if (command >> 194 != 0) revert Invalid();
        uint32 epoch = _auth(id, uint64(command), uint40(command >> 64), 2, uint32(command >> 162));
        uint16 limit = uint16(command >> 104); uint32 left = uint32(command >> 120);
        uint8 steps = uint8(command >> 152); bool buy = command >> 160 & 1 != 0;
        bool reduce = command >> 161 & 1 != 0;
        if (left == 0 || left > maxPosition || steps == 0 || steps > 32) revert Invalid();
        uint16 p = _sync(); _requireSolvent(p); _band(limit, p); _settle(ledger[id]); _cancel(id);
        TakeScratch memory scratch;
        scratch.initial = ledger[id].base; scratch.initialEquity = _equity(ledger[id], p);
        if (reduce && ((buy && scratch.initial >= 0) || (!buy && scratch.initial <= 0) || left > _abs(scratch.initial))) revert Risk();
        uint8 side = buy ? 1 : 0;
        for (uint8 visited; visited < steps && left != 0; ++visited) {
            uint16 tick = best(side);
            if (tick == 0 || (buy ? tick > limit : tick < limit)) break;
            uint32 oid = levels[uint32(side) << 16 | tick].head;
            Order memory o = orders[oid]; uint32 maker = oid / 2;
            _settle(ledger[maker]);
            uint32 clip = o.lots < left ? o.lots : left;
            uint256 notional = uint256(clip) * tick * tickValue;
            uint32 makerCap = o.epoch == 0 ? maxPosition : sessions[maker].maxPosition;
            if (o.expiry <= block.timestamp || owners[maker] == owners[id] ||
                !_orderSession(maker, o, notional) || !_healthy(maker, p, makerCap)) {
                _remove(oid); continue;
            }
            _charge(id, epoch, notional); _charge(maker, o.epoch, notional);
            int64 delta = int64(uint64(clip)); if (!buy) delta = -delta;
            _trade(id, maker, delta, uint256(tick) * tickValue);
            scratch.notional += notional;
            uint256 fee = (scratch.notional * takerFeeBps + 9999) / 10000 - scratch.fee; scratch.fee += fee;
            _cash(ledger[id], -int256(fee)); insurance += uint128(fee);
            if (clip == o.lots) _remove(oid); else orders[oid].lots = o.lots - clip;
            // This fill is inside the pre-admitted endpoints at the same index.
            // Makers pay no fee; bid < ask makes any round trip nonnegative.
            // Cash/base and remaining orders therefore preserve makerCap and IM.
            // Reentrancy is blocked and oracle/funding cannot change in this loop.
            // See docs/MAKER_FILL_INVARIANT.md; changing fees invalidates it.
            // A closing taker frees its slot before a flat maker needs one at capacity.
            if (ledger[id].base == 0) _updateRisk(id);
            _updateRisk(maker);
            left -= clip; filled += clip;
            emit Fill(maker, id, buy, clip, tick, fee);
        }
        if (!_healthy(id, p, epoch == 0 ? maxPosition : sessions[id].maxPosition) &&
            !(reduce && _improves(id, p, scratch.initial, scratch.initialEquity))) revert Risk();
        _updateRisk(id);
        emit Taken(id, uint64(command), filled);
    }
    /// Permissionless bounded head cleanup. No price/oracle dependency for expiry.
    function prune(uint8 side, uint8 steps) external lock returns (uint8 removed) {
        if (side > 1 || steps == 0 || steps > 32) revert Invalid();
        while (removed < steps) {
            uint16 tick = best(side); if (tick == 0) break;
            uint32 oid = levels[uint32(side) << 16 | tick].head;
            Order memory o = orders[oid];
            if (o.expiry > block.timestamp && _orderSession(oid / 2, o, uint256(o.lots) * tick * tickValue)) break;
            _remove(oid); ++removed;
        }
    }
    function liquidate(uint32 target, uint32 liquidator, uint32 lots) external lock {
        _owner(liquidator);
        if (target == liquidator || owners[target] == address(0) || owners[target] == owners[liquidator]) revert Invalid();
        uint16 p = _sync(); _settle(ledger[target]); _settle(ledger[liquidator]);
        SolvencyIndex.Account storage a = ledger[target]; uint256 size = _abs(a.base);
        int256 eq = _equity(a, p);
        if (size == 0 || lots == 0 || lots > size || eq >= int256((size * p * tickValue * maintenanceBps + 9999) / 10000)) revert Risk();
        if (eq < 0 && lots != size) revert Risk();
        _cancel(target); _cancel(liquidator);
        uint256 px = uint256(p) * tickValue;
        uint256 reward = eq > 0 ? uint256(eq) : 0;
        uint256 rewardCap = uint256(lots) * px * liquidationBps / 10000;
        if (reward > rewardCap) reward = rewardCap;
        int64 delta = a.base > 0 ? int64(uint64(lots)) : -int64(uint64(lots));
        _trade(liquidator, target, delta, px);
        _cash(a, -int256(reward)); _cash(ledger[liquidator], int256(reward));
        if (!_healthy(liquidator, p, maxPosition)) revert Risk();
        uint256 debt;
        if (a.base == 0 && a.cash < 0) {
            debt = uint256(-int256(a.cash)); a.cash = 0;
            uint256 covered = debt < insurance ? debt : insurance;
            insurance -= uint128(covered); badDebt += uint128(debt - covered);
        }
        _updateRisk(target); _updateRisk(liquidator);
        emit LiquidationReward(target, liquidator, reward);
        emit Liquidated(target, liquidator, lots, px, debt);
    }
    function _updateRisk(uint32 id) private {
        solvencyIndex.update(ledger, id);
    }
    function _requireSolvent(uint16 p) private view {
        if (!solvencyIndex.solvent(int256(uint256(p) * tickValue) - fundingIndex)) revert Halted();
    }
    function solventAt(uint16 p) external view returns (bool) {
        uint256 elapsed = block.timestamp - fundingTime;
        if (elapsed > fundingHorizon) elapsed = fundingHorizon;
        int256 index = int256(fundingIndex) + int256(fundingVelocity) * int256(elapsed);
        return badDebt == 0 && solvencyIndex.solvent(int256(uint256(p) * tickValue) - index);
    }
    function riskCapacity() external view returns (uint16 active, uint16 highWater, uint16 capacity) {
        return (solvencyIndex.active, solvencyIndex.highWater, 4096);
    }
    function syncFunding() external lock { _sync(); }
    function best(uint8 side) public view returns (uint16) {
        if (side > 1) revert Invalid(); uint256 root = roots[side]; if (root == 0) return 0;
        uint16 word = side == 0 ? root.msb() : root.lsb();
        uint256 leaf = words[uint16(side) << 8 | word];
        return (word << 8) | (side == 0 ? leaf.msb() : leaf.lsb());
    }
    function equity(uint32 id, uint16 p) external view returns (int256) {
        SolvencyIndex.Account storage a = ledger[id];
        uint256 elapsed = block.timestamp - fundingTime;
        if (elapsed > fundingHorizon) elapsed = fundingHorizon;
        int256 idx = int256(fundingIndex) + int256(fundingVelocity) * int256(elapsed);
        return int256(a.cash) + int256(a.base) * int256(uint256(p) * tickValue) - int256(a.base) * (idx - a.funding);
    }
    function _active() private view { if (paused || badDebt != 0) revert Halted(); }
    function _owner(uint32 id) private view { if (msg.sender != owners[id]) revert Unauthorized(); }
    function _auth(uint32 id, uint64 nonce, uint40 expiry, uint8 permission, uint32 expectedEpoch) private returns (uint32 epoch) {
        if (owners[id] == address(0) || expiry <= block.timestamp || expiry > block.timestamp + quoteTTL) revert Invalid();
        if (msg.sender != owners[id]) {
            Session storage s = sessions[id];
            if (msg.sender != s.key || expiry > s.expiry || s.permissions & permission == 0 || expectedEpoch != s.epoch) revert Unauthorized(); epoch = s.epoch;
        } else if (expectedEpoch != 0) revert Unauthorized();
        SolvencyIndex.Account storage a = ledger[id]; if (nonce != a.nonce + 1) revert Sequence(); a.nonce = nonce;
    }
    function _orderSession(uint32 id, Order memory o, uint256 cost) private view returns (bool) {
        if (o.epoch == 0) return true; Session storage s = sessions[id];
        return s.epoch == o.epoch && s.expiry > block.timestamp && s.remaining >= cost && s.key != address(0);
    }
    function _charge(uint32 id, uint32 epoch, uint256 cost) private {
        if (epoch != 0) {
            Session storage s = sessions[id]; if (cost > s.remaining) revert Risk(); s.remaining -= uint128(cost);
        }
    }
    function _sync() private returns (uint16 p) {
        (uint16 index, uint16 mark) = oracle.read();
        if (index == 0 || mark == 0) revert Oracle(); p = index;
        uint256 elapsed = block.timestamp - fundingTime;
        if (elapsed > fundingHorizon) elapsed = fundingHorizon;
        int256 next = int256(fundingIndex) + int256(fundingVelocity) * int256(elapsed);
        if (next < type(int128).min || next > type(int128).max) revert Invalid(); fundingIndex = int128(next);
        int256 premium = (int256(uint256(mark)) - int256(uint256(index))) * int256(uint256(tickValue));
        int256 cap = int256(uint256(index) * tickValue * fundingCapBps / 10000);
        if (premium > cap) premium = cap; if (premium < -cap) premium = -cap;
        fundingVelocity = int128(premium / 3600); fundingTime = uint40(block.timestamp);
        emit Funding(fundingIndex, fundingVelocity, fundingTime);
    }
    function _settle(SolvencyIndex.Account storage a) private {
        _cash(a, -int256(a.base) * (int256(fundingIndex) - a.funding)); a.funding = fundingIndex;
    }
    function _cash(SolvencyIndex.Account storage a, int256 delta) private {
        int256 next = int256(a.cash) + delta;
        if (next < type(int128).min || next > type(int128).max) revert Invalid(); a.cash = int128(next);
    }
    function _trade(uint32 buyer, uint32 seller, int64 delta, uint256 price) private {
        int64 nextBuyer = int64(ledger[buyer].base) + delta;
        int64 nextSeller = int64(ledger[seller].base) - delta;
        if (nextBuyer < type(int48).min || nextBuyer > type(int48).max ||
            nextSeller < type(int48).min || nextSeller > type(int48).max) revert Invalid();
        ledger[buyer].base = int48(nextBuyer); ledger[seller].base = int48(nextSeller);
        int256 value = int256(delta) * int256(price);
        _cash(ledger[buyer], -value); _cash(ledger[seller], value);
    }
    function _equity(SolvencyIndex.Account storage a, uint16 p) private view returns (int256) {
        return int256(a.cash) + int256(a.base) * int256(uint256(p) * tickValue);
    }
    function _improves(uint32 id, uint16 p, int64 beforeBase, int256 beforeEquity) private view returns (bool) {
        SolvencyIndex.Account storage a = ledger[id]; int256 afterEquity = _equity(a, p);
        uint256 beforeSize = _abs(beforeBase); uint256 afterSize = _abs(a.base);
        return afterEquity >= 0 && afterSize <= beforeSize &&
            afterEquity * int256(beforeSize) >= beforeEquity * int256(afterSize);
    }
    function _point(int256 cash, int256 base, uint16 p, uint32 cap) private view returns (bool) {
        uint256 size = uint256(base < 0 ? -base : base);
        if (size > cap) return false;
        int256 eq = cash + base * int256(uint256(p) * tickValue);
        return eq >= int256((size * p * tickValue * initialBps + 9999) / 10000);
    }
    function _healthy(uint32 id, uint16 p, uint32 cap) private view returns (bool) {
        SolvencyIndex.Account storage a = ledger[id];
        int256 cash = int256(a.cash) - int256(a.base) * (int256(fundingIndex) - a.funding);
        int256 base = a.base;
        if (!_point(cash, base, p, cap)) return false;
        Order storage b = orders[id * 2]; Order storage s = orders[id * 2 + 1];
        // bid < ask: any simultaneous fills decompose into a one-sided endpoint
        // plus a nonnegative round trip. Checking both one-sided endpoints suffices.
        return _point(cash - int256(uint256(b.lots) * b.tick * tickValue), base + int256(uint256(b.lots)), p, cap) &&
            _point(cash + int256(uint256(s.lots) * s.tick * tickValue), base - int256(uint256(s.lots)), p, cap);
    }
    function _band(uint16 price, uint16 index) private view {
        if (price == 0 || uint256(price > index ? price - index : index - price) * 10000 > uint256(index) * priceBandBps) revert Oracle();
    }
    function _retain(uint32 oid, uint16 tick, uint32 lots, uint32 epoch) private returns (bool) {
        Order storage o = orders[oid];
        if (lots != 0 && o.tick == tick && lots <= o.lots && o.epoch == epoch && o.expiry > block.timestamp) return true;
        _remove(oid); return false;
    }
    function _insert(uint32 oid, uint16 tick, uint32 lots, uint40 expiry, uint32 epoch) private {
        uint32 side = oid & 1; uint32 key = side << 16 | tick;
        Level storage l = levels[key]; uint32 tail = l.tail;
        orders[oid] = Order(tail, 0, lots, tick, expiry, epoch);
        if (tail == 0) {
            l.head = oid; uint16 w = uint16(side << 8) | tick >> 8;
            words[w] |= uint256(1) << (tick & 255); roots[side] |= uint256(1) << (tick >> 8);
        } else orders[tail].next = oid;
        l.tail = oid;
    }
    function _remove(uint32 oid) private {
        Order memory o = orders[oid]; if (o.lots == 0) return;
        uint32 side = oid & 1; Level storage l = levels[side << 16 | o.tick];
        if (o.prev == 0) l.head = o.next; else orders[o.prev].next = o.next;
        if (o.next == 0) l.tail = o.prev; else orders[o.next].prev = o.prev;
        if (l.head == 0) {
            uint16 w = uint16(side << 8) | o.tick >> 8;
            words[w] &= ~(uint256(1) << (o.tick & 255));
            if (words[w] == 0) roots[side] &= ~(uint256(1) << (o.tick >> 8));
        }
        delete orders[oid];
    }
    function _cancel(uint32 id) private { _remove(id * 2); _remove(id * 2 + 1); emit Cancelled(id); }
    function _abs(int64 x) private pure returns (uint256) { return uint256(x < 0 ? -int256(x) : int256(x)); }
    function _token(bytes memory data) private {
        (bool ok, bytes memory result) = address(collateral).call(data);
        if (!ok || (result.length != 0 && (result.length != 32 || !abi.decode(result, (bool))))) revert Transfer();
    }
    function _receive(uint128 amount) private {
        uint256 before = collateral.balanceOf(address(this));
        _token(abi.encodeCall(ICollateral.transferFrom, (msg.sender, address(this), uint256(amount))));
        if (collateral.balanceOf(address(this)) != before + amount) revert Transfer();
    }
}
