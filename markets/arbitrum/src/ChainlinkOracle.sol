// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;
import './Interfaces.sol';

contract ChainlinkOracle is IMarketOracle {
    error InvalidFeed();
    IAggregator public immutable indexFeed;
    IAggregator public immutable markFeed;
    IAggregator public immutable sequencerFeed;
    uint256 public immutable tickSize; // Price units at feed decimals, both feeds equal decimals.
    uint256 public immutable maxAge;
    uint256 public immutable recoveryGrace;
    constructor(address index_, address mark_, address sequencer_, uint256 tick_, uint256 age_, uint256 grace_) {
        if (index_.code.length == 0 || mark_.code.length == 0 || sequencer_.code.length == 0 ||
            tick_ == 0 || age_ == 0 || grace_ < 3600 ||
            IAggregator(index_).decimals() != IAggregator(mark_).decimals()) revert InvalidFeed();
        indexFeed = IAggregator(index_); markFeed = IAggregator(mark_);
        sequencerFeed = IAggregator(sequencer_); tickSize = tick_; maxAge = age_; recoveryGrace = grace_;
    }
    function _read(IAggregator feed) private view returns (uint16) {
        (uint80 id, int256 answer,, uint256 updated, uint80 answered) = feed.latestRoundData();
        if (id == 0 || answer <= 0 || updated == 0 || updated > block.timestamp ||
            block.timestamp - updated > maxAge || answered < id) revert InvalidFeed();
        uint256 p = uint256(answer) / tickSize;
        if (p == 0 || p > type(uint16).max) revert InvalidFeed();
        return uint16(p);
    }
    function read() external view returns (uint16 indexTick, uint16 markTick) {
        (, int256 down, uint256 started,,) = sequencerFeed.latestRoundData();
        if (down != 0 || started == 0 || started > block.timestamp || block.timestamp - started <= recoveryGrace)
            revert InvalidFeed();
        indexTick = _read(indexFeed);
        markTick = address(markFeed) == address(indexFeed) ? indexTick : _read(markFeed);
    }
}
