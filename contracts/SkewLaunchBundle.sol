// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "./SkewArtifactMining.sol";

/// @notice One deployment creates the license contract, work verifier, and zero-supply SKEW token.
/// @dev The factory retains no administrator or money-moving authority.
contract SkewLaunchBundle {
    SkewDataPass public immutable dataPass;
    SkewArtifactMining public immutable mining;
    SkewSolutionToken public immutable token;
    event Launched(address indexed owner, address dataPass, address mining, address token);

    constructor(address owner, address paymentAsset) {
        dataPass = new SkewDataPass(owner, paymentAsset);
        mining = new SkewArtifactMining(owner, address(dataPass));
        token = mining.rewardToken();
        emit Launched(owner, address(dataPass), address(mining), address(token));
    }
}
