// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;
import "SolutionMining.sol";
/// @notice Tests only. DOES NOT verify a VRF proof; never marketed as fair randomness.
contract SolutionVRFMock is SolutionVRF {
    uint256 public next = 1;
    mapping(uint256 => address) public consumer;
    function requestRandomWords(Request calldata r) external returns (uint256 id) {
        require(r.numWords == 1 && r.requestConfirmations == 3 && r.callbackGasLimit == 150000, "REQUEST_ABI");
        require(keccak256(r.extraArgs) == keccak256(abi.encodeWithSelector(bytes4(keccak256("VRF ExtraArgsV1")),false)), "EXTRA_ARGS_ABI");
        id = next++; consumer[id] = msg.sender;
    }
    function deliver(uint256 id,uint256[] calldata words) external {
        SolutionMining(consumer[id]).rawFulfillRandomWords(id,words);
    }
}
