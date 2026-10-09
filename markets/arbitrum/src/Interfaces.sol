// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;
interface ICollateral {
    function transfer(address, uint256) external returns (bool);
    function transferFrom(address, address, uint256) external returns (bool);
    function balanceOf(address) external view returns (uint256);
    function decimals() external view returns (uint8);
}
interface IMarketOracle {
    // Prices in fixed market ticks. Revert on stale, out-of-range, or downtime.
    function read() external view returns (uint16 indexTick, uint16 markTick);
}
interface IAggregator {
    function decimals() external view returns (uint8);
    function latestRoundData() external view returns (uint80, int256, uint256, uint256, uint80);
}
