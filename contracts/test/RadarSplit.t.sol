// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {RadarSplit} from "../src/RadarSplit.sol";

/// @dev Minimal 6-decimal USDC-like token for tests.
contract MockUSDC is ERC20 {
    constructor() ERC20("Mock USDC", "USDC") {}
    function decimals() public pure override returns (uint8) { return 6; }
    function mint(address to, uint256 amt) external { _mint(to, amt); }
}

/// @dev Malicious ERC20 whose transferFrom reenters RadarSplit.settle with the same args.
contract ReentrantUSDC is ERC20 {
    RadarSplit public target;
    bytes32 public pid;
    address public dev;
    uint256 public amt;
    bool armed;

    constructor() ERC20("Reentrant USDC", "rUSDC") {}
    function decimals() public pure override returns (uint8) { return 6; }
    function mint(address to, uint256 a) external { _mint(to, a); }
    function arm(RadarSplit _t, bytes32 _pid, address _dev, uint256 _amt) external {
        target = _t; pid = _pid; dev = _dev; amt = _amt; armed = true;
    }
    function transferFrom(address from, address to, uint256 value) public override returns (bool) {
        if (armed) {
            armed = false; // one-shot, avoid infinite loop
            target.settle(pid, dev, amt); // reenter — must be blocked
        }
        return super.transferFrom(from, to, value);
    }
}

contract RadarSplitTest is Test {
    MockUSDC usdc;
    RadarSplit radar;

    address gateway;
    address agent;
    address dev;

    bytes32 constant PID = keccak256("payment-1");

    event Settled(
        bytes32 indexed paymentId,
        address indexed payer,
        address indexed developer,
        uint256 devAmount,
        uint256 fee,
        address feeRecipient
    );
    event FeeConfigUpdated(uint16 feeBps, address feeRecipient);

    function setUp() public {
        gateway = makeAddr("gateway");
        agent = makeAddr("agent");
        dev = makeAddr("dev");

        usdc = new MockUSDC();
        // owner = gateway, feeRecipient = gateway, 0% promo fee.
        vm.prank(gateway);
        radar = new RadarSplit(address(usdc), gateway, gateway, 0);

        usdc.mint(agent, 1_000_000); // 1 USDC (6dp)
        vm.prank(agent);
        usdc.approve(address(radar), type(uint256).max);
    }

    // ── Happy path: 0% promo → 100% to developer ────────────────────────────────
    function test_settle_zeroFee_paysFullAmountToDeveloper() public {
        uint256 amount = 2000;
        vm.expectEmit(true, true, true, true);
        emit Settled(PID, agent, dev, amount, 0, gateway);

        vm.prank(agent);
        radar.settle(PID, dev, amount);

        assertEq(usdc.balanceOf(dev), amount, "dev gets 100%");
        assertEq(usdc.balanceOf(gateway), 0, "no fee under promo");
        assertTrue(radar.isSettled(PID, agent));
    }

    // ── Fee split math ──────────────────────────────────────────────────────────
    function test_settle_withFee_splits85_15() public {
        vm.prank(gateway);
        radar.setFeeConfig(1500, gateway);

        uint256 amount = 1000;
        vm.prank(agent);
        radar.settle(PID, dev, amount);

        assertEq(usdc.balanceOf(dev), 850, "dev gets 85%");
        assertEq(usdc.balanceOf(gateway), 150, "gateway gets 15%");
    }

    function test_settle_feeRounding_devGetsRemainder() public {
        vm.prank(gateway);
        radar.setFeeConfig(1500, gateway);
        uint256 amount = 7; // fee = floor(7*1500/10000)=1; dev=6
        vm.prank(agent);
        radar.settle(PID, dev, amount);
        assertEq(usdc.balanceOf(gateway), 1);
        assertEq(usdc.balanceOf(dev), 6, "rounding favors developer");
    }

    // ── Replay protection (same payer) ──────────────────────────────────────────
    function test_settle_replayReverts() public {
        vm.prank(agent);
        radar.settle(PID, dev, 1000);

        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(RadarSplit.AlreadySettled.selector, PID));
        radar.settle(PID, dev, 1000);
    }

    // ── Griefing fix: a third party cannot burn the agent's paymentId ───────────
    function test_settle_replayKeyNamespacedByPayer() public {
        address attacker = makeAddr("attacker");
        address attackerDev = makeAddr("attackerDev");
        usdc.mint(attacker, 1000);
        vm.prank(attacker);
        usdc.approve(address(radar), type(uint256).max);

        // Attacker pre-consumes the SAME paymentId with their own address.
        vm.prank(attacker);
        radar.settle(PID, attackerDev, 1);

        // Agent's legitimate settle on the same id still succeeds (different key).
        vm.prank(agent);
        radar.settle(PID, dev, 2000);

        assertEq(usdc.balanceOf(dev), 2000, "agent settles despite attacker pre-call");
        assertTrue(radar.isSettled(PID, agent));
        assertTrue(radar.isSettled(PID, attacker));
    }

    // ── Reentrancy guard ────────────────────────────────────────────────────────
    function test_settle_reentrancyBlocked() public {
        ReentrantUSDC eviltoken = new ReentrantUSDC();
        vm.prank(gateway);
        RadarSplit r = new RadarSplit(address(eviltoken), gateway, gateway, 0);

        eviltoken.mint(agent, 10_000);
        vm.prank(agent);
        eviltoken.approve(address(r), type(uint256).max);
        eviltoken.arm(r, PID, dev, 1000);

        // The reentrant inner settle() must make the outer call revert (nonReentrant).
        vm.prank(agent);
        vm.expectRevert(ReentrancyGuard.ReentrancyGuardReentrantCall.selector);
        r.settle(PID, dev, 1000);
    }

    // ── Input validation ────────────────────────────────────────────────────────
    function test_settle_zeroDeveloperReverts() public {
        vm.prank(agent);
        vm.expectRevert(RadarSplit.ZeroDeveloper.selector);
        radar.settle(PID, address(0), 1000);
    }

    function test_settle_zeroAmountReverts() public {
        vm.prank(agent);
        vm.expectRevert(RadarSplit.ZeroAmount.selector);
        radar.settle(PID, dev, 0);
    }

    function test_settle_insufficientAllowanceReverts() public {
        address poor = makeAddr("poor");
        usdc.mint(poor, 1000);
        vm.prank(poor);
        vm.expectRevert();
        radar.settle(keccak256("p2"), dev, 1000);
    }

    // ── Admin / access control ──────────────────────────────────────────────────
    function test_setFeeConfig_emitsEvent() public {
        vm.expectEmit(false, false, false, true);
        emit FeeConfigUpdated(500, gateway);
        vm.prank(gateway);
        radar.setFeeConfig(500, gateway);
        assertEq(radar.feeBps(), 500);
    }

    function test_setFeeConfig_overCapReverts() public {
        vm.prank(gateway);
        vm.expectRevert(abi.encodeWithSelector(RadarSplit.FeeTooHigh.selector, uint16(1501)));
        radar.setFeeConfig(1501, gateway);
    }

    function test_setFeeConfig_onlyOwner() public {
        vm.prank(agent);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, agent));
        radar.setFeeConfig(100, gateway);
    }

    function test_setFeeConfig_feeWithoutRecipientReverts() public {
        vm.prank(gateway);
        vm.expectRevert(RadarSplit.FeeRecipientRequired.selector);
        radar.setFeeConfig(100, address(0));
    }

    function test_owner_isConstructorOwnerNotDeployer() public view {
        assertEq(radar.owner(), gateway, "owner is the explicit _owner arg");
    }

    // ── Constructor guards ──────────────────────────────────────────────────────
    function test_constructor_zeroTokenReverts() public {
        vm.expectRevert(RadarSplit.ZeroToken.selector);
        new RadarSplit(address(0), gateway, gateway, 0);
    }

    function test_constructor_feeWithoutRecipientReverts() public {
        vm.expectRevert(RadarSplit.FeeRecipientRequired.selector);
        new RadarSplit(address(usdc), gateway, address(0), 100);
    }

    function test_constructor_emitsFeeConfig() public {
        vm.expectEmit(false, false, false, true);
        emit FeeConfigUpdated(0, gateway);
        new RadarSplit(address(usdc), gateway, gateway, 0);
    }

    // ── Fuzz: split is conservative (dev + fee == amount, fee ≤ cap) ─────────────
    function testFuzz_splitConservesAmount(uint96 amount, uint16 bps) public {
        amount = uint96(bound(amount, 1, 1_000_000));
        bps = uint16(bound(bps, 0, radar.MAX_FEE_BPS()));
        vm.prank(gateway);
        radar.setFeeConfig(bps, gateway);

        usdc.mint(agent, amount);
        vm.prank(agent);
        radar.settle(keccak256(abi.encode(amount, bps)), dev, amount);

        uint256 fee = (uint256(bps) * amount) / 10_000;
        assertEq(usdc.balanceOf(gateway), fee);
        assertEq(usdc.balanceOf(dev), amount - fee);
    }
}
