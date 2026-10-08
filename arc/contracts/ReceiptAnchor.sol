// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// Anchors what a buyer got from an x402 nanopayment on Arc. Holds no funds, has no owner.
/// Gateway-batched payments leave no per-call transaction, so the receipt hash is the proof.
contract ReceiptAnchor {
    event Receipt(
        address indexed payer,
        bytes32 indexed endpoint, // keccak256(resource URL)
        bytes32 receipt, // keccak256(PAYMENT-RESPONSE as received)
        uint256 amount, // USDC atomic units charged
        bool delivered,
        string proof // "eip3009:tx:<hash>" | "gateway:tx:<hash>" | "gateway:transfer:<id>" | "none"
    );

    uint256 public total;
    mapping(bytes32 => uint256) public perEndpoint;

    function attest(bytes32 endpoint, bytes32 receipt, uint256 amount, bool delivered, string calldata proof) external {
        total++;
        perEndpoint[endpoint]++;
        emit Receipt(msg.sender, endpoint, receipt, amount, delivered, proof);
    }
}
