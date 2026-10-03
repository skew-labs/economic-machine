// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;
import "SkewSolutionMining.sol";
/// @notice Test fixture only. No cryptographic randomness or real subscription funding.
contract SkewVRFMock is SkewVRF {
    uint256 public next = 1;
    uint96 public balance = 1e18;
    bool public admitted;
    address public subOwner;
    address public lastConsumer;
    mapping(uint256 => address) public consumer;
    constructor() { subOwner = msg.sender; }
    function configure(uint96 amount, bool enabled) external { balance = amount; admitted = enabled; }
    function getSubscription(uint256) external view returns (uint96,uint96,uint64,address,address[] memory users) {
        users = new address[](admitted ? 1 : 0);
        if (admitted) users[0] = lastConsumer;
        return (balance, 0, 0, subOwner, users);
    }
    function register(address target) external { lastConsumer = target; }
    function requestRandomWords(Request calldata r) external returns (uint256 id) {
        require(r.numWords == 1 && r.requestConfirmations == 3 && r.callbackGasLimit == 250000, "REQUEST_ABI");
        require(keccak256(r.extraArgs) == keccak256(abi.encodeWithSelector(bytes4(keccak256("VRF ExtraArgsV1")),false)), "EXTRA_ARGS");
        id = next++; consumer[id] = msg.sender;
    }
    function deliver(uint256 id, uint256[] calldata words) external {
        SkewSolutionMining(consumer[id]).rawFulfillRandomWords(id, words);
    }
    function deliverLimited(uint256 id, uint256[] calldata words) external returns (bool success) {
        (success,) = consumer[id].call{gas:250000}(abi.encodeWithSelector(SkewSolutionMining.rawFulfillRandomWords.selector,id,words));
    }
}
