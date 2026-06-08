// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @title RadarSplit — atomic, non-custodial x402 settlement for the AgentPay Arbitrum Radar
/// @author AgentPay (romudille)
/// @notice Settles a single x402 payment from an agent to a Radar-listed project on the
///         Arbitrum stack (Arbitrum One/Sepolia, Robinhood Chain). The payer's USDC is
///         split atomically between the developer and an optional gateway fee, in one tx.
/// @dev    Non-custodial: funds move payer → developer (+ payer → feeRecipient) via
///         `transferFrom`; the contract never holds balances. The gateway fee defaults to
///         0 bps for the Arbitrum/Robinhood ecosystem promo (100% to the project) and is
///         capped at 1500 bps (15%).
///
///         Replay protection is namespaced by **payer**: the guard key is
///         keccak256(paymentId, payer). An agent cannot replay its own payment, and a
///         third party cannot grief a predictable `paymentId` by pre-consuming it (their
///         settle uses a different key). The gateway adapter MUST still verify all
///         `Settled` fields (this contract address, payer, developer, amount) — not just
///         paymentId — before releasing tool data.
contract RadarSplit is ReentrancyGuard, Ownable {
    using SafeERC20 for IERC20;

    /// @notice Hard cap on the gateway fee: 1500 bps = 15%.
    uint16 public constant MAX_FEE_BPS = 1500;

    /// @notice The settlement asset (USDC on the target chain).
    IERC20 public immutable usdc;

    /// @notice Gateway fee in basis points (0 = 100% to the developer).
    uint16 public feeBps;

    /// @notice Recipient of the gateway fee (ignored when feeBps == 0).
    address public feeRecipient;

    /// @notice Consumed (paymentId, payer) keys — prevents double-settlement (replay).
    /// @dev    Key = keccak256(abi.encode(paymentId, payer)); see `_consumeKey`.
    mapping(bytes32 => bool) public consumed;

    /// @notice Emitted once per successful settlement.
    /// @param paymentId    The x402 payment id (gateway-issued).
    /// @param payer        The agent that paid (msg.sender).
    /// @param developer    The Radar-listed project that received its share.
    /// @param devAmount    USDC forwarded to the developer.
    /// @param fee          USDC retained as the gateway fee (0 under the promo).
    /// @param feeRecipient Address that received the fee.
    event Settled(
        bytes32 indexed paymentId,
        address indexed payer,
        address indexed developer,
        uint256 devAmount,
        uint256 fee,
        address feeRecipient
    );

    /// @notice Emitted when the fee configuration changes (and once at construction).
    event FeeConfigUpdated(uint16 feeBps, address feeRecipient);

    error AlreadySettled(bytes32 paymentId);
    error ZeroToken();
    error ZeroDeveloper();
    error ZeroAmount();
    error FeeTooHigh(uint16 feeBps);
    error FeeRecipientRequired();

    /// @param _usdc         USDC token address on the target chain.
    /// @param _owner        Contract owner (controls `setFeeConfig`) — set this to the
    ///                      gateway wallet, not the deploy key.
    /// @param _feeRecipient Fee recipient; may be address(0) iff `_feeBps` is 0.
    /// @param _feeBps       Initial gateway fee in bps (use 0 for the promo).
    constructor(address _usdc, address _owner, address _feeRecipient, uint16 _feeBps)
        Ownable(_owner)
    {
        if (_usdc == address(0)) revert ZeroToken();
        if (_feeBps > MAX_FEE_BPS) revert FeeTooHigh(_feeBps);
        if (_feeBps > 0 && _feeRecipient == address(0)) revert FeeRecipientRequired();
        usdc = IERC20(_usdc);
        feeBps = _feeBps;
        feeRecipient = _feeRecipient;
        emit FeeConfigUpdated(_feeBps, _feeRecipient);
    }

    /// @notice Settle one x402 payment, splitting `amount` USDC between `developer` and the fee.
    /// @dev    Caller (the paying agent) must have approved at least `amount` USDC to this
    ///         contract. Checks-effects-interactions: the replay flag is set before any
    ///         transfer, and the call is `nonReentrant`. Reverts on replay (for this payer),
    ///         zero developer, or zero amount.
    /// @param paymentId The gateway-issued x402 payment id.
    /// @param developer The Radar-listed project receiving its share.
    /// @param amount    Total USDC the agent pays (devAmount + fee).
    function settle(bytes32 paymentId, address developer, uint256 amount)
        external
        nonReentrant
    {
        if (developer == address(0)) revert ZeroDeveloper();
        if (amount == 0) revert ZeroAmount();

        bytes32 key = _consumeKey(paymentId, msg.sender);
        if (consumed[key]) revert AlreadySettled(paymentId);

        // Effects before interactions (replay-safe even under a malicious token).
        consumed[key] = true;

        uint256 fee = (uint256(feeBps) * amount) / 10_000;
        uint256 devAmount = amount - fee;

        // Non-custodial: pull straight from payer to each recipient.
        usdc.safeTransferFrom(msg.sender, developer, devAmount);
        if (fee > 0) {
            usdc.safeTransferFrom(msg.sender, feeRecipient, fee);
        }

        emit Settled(paymentId, msg.sender, developer, devAmount, fee, feeRecipient);
    }

    /// @notice Update the gateway fee and recipient. Owner only; capped at MAX_FEE_BPS.
    /// @param _feeBps       New fee in bps (0 disables the fee).
    /// @param _feeRecipient New fee recipient (required when `_feeBps` > 0).
    function setFeeConfig(uint16 _feeBps, address _feeRecipient) external onlyOwner {
        if (_feeBps > MAX_FEE_BPS) revert FeeTooHigh(_feeBps);
        if (_feeBps > 0 && _feeRecipient == address(0)) revert FeeRecipientRequired();
        feeBps = _feeBps;
        feeRecipient = _feeRecipient;
        emit FeeConfigUpdated(_feeBps, _feeRecipient);
    }

    /// @notice Whether a given (paymentId, payer) pair has already been settled.
    function isSettled(bytes32 paymentId, address payer) external view returns (bool) {
        return consumed[_consumeKey(paymentId, payer)];
    }

    /// @dev Replay guard key — namespacing by payer prevents third-party id griefing.
    function _consumeKey(bytes32 paymentId, address payer) internal pure returns (bytes32) {
        return keccak256(abi.encode(paymentId, payer));
    }
}
