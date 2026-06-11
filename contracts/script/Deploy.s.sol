// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {RadarSplit} from "../src/RadarSplit.sol";

/// @notice Deploys RadarSplit to an Arbitrum-stack chain.
/// @dev    Configure via env vars, then run (example for Arbitrum Sepolia):
///
///   export RADAR_USDC=0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d   # chain USDC
///   export RADAR_OWNER=0x...                                       # owner (gateway wallet)
///   export RADAR_FEE_RECIPIENT=0x...                              # fee recipient (gateway wallet)
///   export RADAR_FEE_BPS=0                                         # 0 = promo (100% to dev)
///   forge script script/Deploy.s.sol \
///     --rpc-url https://sepolia-rollup.arbitrum.io/rpc \
///     --account deployer --broadcast --verify
///
/// Robinhood Chain testnet: RADAR_USDC=0x5B6C7cAF7F99f99154fD8375ec935Fcf03F326f5,
///   --rpc-url https://rpc.testnet.chain.robinhood.com/rpc
///
/// NOTE: deployment broadcasts a real transaction and needs a funded key — run it
/// yourself; this repo never holds keys.
contract Deploy is Script {
    function run() external returns (RadarSplit radar) {
        address usdc = vm.envAddress("RADAR_USDC");
        // Fee recipient + bps are optional; default to the 0% ecosystem promo.
        address feeRecipient = vm.envOr("RADAR_FEE_RECIPIENT", address(0));
        uint256 feeBps = vm.envOr("RADAR_FEE_BPS", uint256(0));
        // Owner controls setFeeConfig — default to the fee recipient (the gateway
        // wallet), then to the broadcaster, never the zero address.
        address owner = vm.envOr("RADAR_OWNER", feeRecipient);
        if (owner == address(0)) owner = msg.sender;

        // Guard before the downcast so a typo (e.g. 65537) can't silently wrap to a
        // small valid fee; the contract also caps at 1500, this just fails earlier+clearer.
        require(feeBps <= 1500, "RADAR_FEE_BPS must be <= 1500 (15%)");

        vm.startBroadcast();
        // forge-lint: disable-next-line(unsafe-typecast)  — bounded by the require above
        radar = new RadarSplit(usdc, owner, feeRecipient, uint16(feeBps));
        vm.stopBroadcast();

        console2.log("RadarSplit deployed at:", address(radar));
        console2.log("  usdc:", usdc);
        console2.log("  owner:", owner);
        console2.log("  feeBps:", feeBps);
        console2.log("  feeRecipient:", feeRecipient);
    }
}
