"""Loads config/settings.<account>.yaml + .env.<account> into typed config
objects. Every account (demo1, live1, ...) gets its own settings file and
env file so accounts are fully independent — see SETUP.md."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_account_name(name: str) -> str:
    """Shared --account validator (used as argparse `type=`) — account names
    flow straight into file paths (.env.<account>, settings.<account>.yaml,
    logs/<account>/, KILL_SWITCH_<account>), so this keeps them restricted
    to a safe charset instead of allowing path separators or traversal."""
    if not ACCOUNT_NAME_RE.match(name):
        raise ValueError(
            f"Invalid account name {name!r}. Only letters, digits, '-' and '_' are allowed."
        )
    return name


@dataclass
class MT5Config:
    login: int | None
    password: str | None
    server: str | None
    terminal_path: str | None
    timeout_ms: int


@dataclass
class EMAPeriodsConfig:
    fast: int
    mid: int
    slow: int
    # Paired with `fast` (EMA5) for the reversal-confirmation check: once an
    # open trade's cross looks invalid, EMA5 vs this EMA9 line is watched as
    # a faster confirmation before stopping out. Optional — defaults to 9,
    # matching every existing account's config without requiring an edit.
    reversal_slow: int = 9


@dataclass
class SessionWindow:
    start: str  # "HH:MM", Asia/Colombo local time
    end: str


@dataclass
class PositionSizingTier:
    max_balance: float | None  # None = no upper bound (this is the top tier)
    lots: float


@dataclass
class ExecutionConfig:
    mode: str  # shadow | demo_execute | live_execute
    require_demo_account: bool
    magic_number: int
    order_deviation_points: int
    order_comment: str
    # Force-closes any position on the symbol whose magic number doesn't
    # match magic_number above — i.e. anything opened by hand in the MT5
    # terminal GUI (manual orders always carry magic=0, confirmed from
    # live account deal history; MT5's order dialog has no magic field).
    reject_manual_trades: bool = False
    # Other magic numbers this process should also treat as "ours", not
    # foreign — for running two of our own processes against the same real
    # MT5 account simultaneously (e.g. an M1 leg and an M3 leg both trading
    # demo1), each with its own magic_number, each listing the other's
    # here so reject_manual_trades doesn't fight between them. Still
    # rejects any magic number NOT in this set (genuine manual/foreign
    # trades, magic=0 included).
    sibling_magic_numbers: list[int] = field(default_factory=list)


@dataclass
class LoggingConfig:
    log_dir: str
    level: str


@dataclass
class KillSwitchConfig:
    file_path: str


@dataclass
class DualCrossConfig:
    """Config specific to strategy_variant=dual_cross (bot/strategy/
    state_machine_dual_cross.py) — see docs/STRATEGY_DUAL_CROSS_SPEC.md.
    Mandatory (not optional/dormant) for that variant: the engine's own
    constructor refuses to run without it."""
    # The entire entry condition (§3 of the spec): a live tick's
    # provisional EMA13/21 must land within this many dollars of each
    # other, AND the relationship must have just flipped versus the
    # previous closed candle. Deliberately a separate field from
    # early_entry_threshold_usd — that one is optional/off-by-default and
    # paired with the old gap-threshold immediate-entry logic; this one is
    # mandatory and has different semantics (the whole entry condition,
    # not a supplement to a gap check).
    cross_tolerance_usd: float
    # Hard cap (§5a): at most this many opposite-direction positions open
    # at once, per account. A blocked signal does not consume that
    # candle's one-shot-entry slot — it can still fire later in the same
    # still-forming candle if a slot frees up.
    max_concurrent_positions: int = 2
    # Gates real (non-shadow) placement of the SECOND simultaneous
    # position: refuses if the connected account isn't confirmed
    # hedging-mode (a netting-mode account would just net the second
    # order against the first at the broker, not open two real tickets).
    # Checked at main.py startup (hard abort) and again, redundantly,
    # right before DualCrossEngine._enter() would place that second real
    # order.
    require_hedging_account: bool = True


@dataclass
class DualCrossConfirmedEntryConfig:
    """Config specific to strategy_variant=dual_cross_confirmed_entry
    (bot/strategy/state_machine_dual_cross_confirmed_entry.py). A dual_cross
    variant built on user request (2026-08-18): entries only ever happen on
    an already-confirmed candle-close cross — no tick-based tolerance entry
    at all, so a genuine cross can never be missed the way dual_cross's
    early-entry path sometimes misses one. The trade-off moves to the
    OTHER side instead: closing the current position (whenever a confirmed
    opposite cross occurs) now prioritizes reacting fast (tick-based) over
    waiting for full confirmation, with confirmation only as a fallback.
    Same $stop_loss_usd/take_profit_usd/session-gating as dual_cross
    otherwise. Unlike dual_cross, this variant holds AT MOST ONE position
    at a time (see the engine module's docstring for why that's an
    inherent consequence of always closing the opposite on a confirmed
    cross, not a separate design choice) — so there's no position cap and
    no hedging-account requirement to configure here. Mandatory (not
    optional/dormant) for this variant — the engine's own constructor
    refuses to run without it."""
    # Tolerance used ONLY for the tick-based closing check (NOT for entry —
    # entries have no tolerance at all in this variant, see above): while a
    # position is open, watch every tick for the OPPOSITE direction's
    # provisional EMA13/21 to flip within this many dollars — the moment it
    # does, close that position right then, before waiting for the candle
    # to actually close and confirm the reversal.
    closing_tolerance_usd: float


@dataclass
class DualCrossTightExitConfig:
    """Config specific to strategy_variant=dual_cross_tight_exit
    (bot/strategy/state_machine_dual_cross_tight_exit.py). Built
    2026-08-19 directly from a real-trade-history finding: 96.9% of
    dual_cross's real losses on demo1_m1/demo1_m3 traced back to either a
    tick-based entry whose own candle failed to confirm it
    (validation_failed) or a second concurrent position getting displaced
    (closed_by_concurrent_validation). Keeps dual_cross's tick-based entry
    (same cross_tolerance_usd semantics), but adds a tight early_exit_usd
    net for the not-yet-validated window and a single-position "reversal
    swap" in place of dual_cross's concurrent-position mechanism — see the
    engine module's docstring for the full design. Like
    dual_cross_confirmed_entry, this variant never holds two positions at
    once, so there's no position cap and no hedging-account requirement to
    configure here. Mandatory (not optional/dormant) for this variant —
    the engine's own constructor refuses to run without it."""
    # Same semantics as dual_cross.cross_tolerance_usd (Β§3): the
    # tick-based entry condition — a live tick's provisional EMA13/21 must
    # land within this many dollars of each other, AND have just flipped
    # versus the previous closed candle.
    cross_tolerance_usd: float
    # NEW mechanism, not present in dual_cross or dual_cross_confirmed_entry:
    # while a position is still unvalidated (its own entry candle hasn't
    # closed yet), watched every tick — if price moves this many dollars
    # against the position, close it immediately at that small, capped
    # loss instead of waiting for the candle to close and taking whatever
    # loss that produces.
    early_exit_usd: float
    # Live default (False): after ONE tick-based entry attempt this
    # candle hits the early-exit net, no further tick-based retries are
    # tried for the rest of that candle — the only way a new position can
    # open for the remainder of that candle is the close-confirmed
    # fallback. Set True ONLY for a backtest-only comparison config (via
    # scripts/backtest.py --settings-path) reproducing an earlier draft of
    # this engine that allowed unlimited same-candle tick-based retries —
    # requested by the user 2026-08-19 specifically to compare against the
    # corrected one-attempt behavior without touching the live config.
    # Never set True in a live-deployed config/settings.<account>.yaml.
    allow_multiple_tick_attempts_per_candle: bool = False


@dataclass
class SwapAdxFilterConfig:
    """Config specific to strategy_variant=dual_cross_tight_exit_swap_confirm_adx
    (bot/strategy/state_machine_dual_cross_tight_exit_swap_confirm_adx.py),
    backtest-only as of 2026-08-20. Built from real-trade evidence:
    swapped_confirmed_reversal was 73-75% of all real loss $ on
    dual_cross_tight_exit_swap_confirm, and every real swap-firing moment
    checked against real ADX(14) data landed well below a 25 threshold
    (11-23 range across two independently chart-verified choppy windows).
    Gates ONLY the swap decision (an existing position being reversed) —
    fresh entries from flat are completely unaffected, since the one
    real example with a strong entry-time ADX reading (28) still lost
    once the trend faded, so gating entries wasn't supported by the data
    the way gating swaps was. Mandatory for this variant — the engine's
    own constructor refuses to run without it."""
    # Wilder's ADX period. 14 is the standard default and what the real
    # loss-window analysis above used — change only with a reason.
    adx_period: int = 14
    # Minimum ADX reading (at the candle where a 2-candle-confirmed swap
    # would otherwise fire) required to actually let the swap execute.
    # Below this, the swap is blocked (category swap_blocked_low_adx —
    # pending reversal cancelled, held position keeps running) instead of
    # firing regardless of P/L like the un-gated swap_confirm variant.
    adx_threshold: float = 25.0


@dataclass
class AppConfig:
    account: str
    mt5: MT5Config
    symbol: str
    timeframe: str
    candles_to_fetch: int
    tick_poll_interval_seconds: int
    ema_periods: EMAPeriodsConfig
    gap_threshold_usd: float
    take_profit_usd: float
    strategy_variant: str  # "gap_threshold" | "dual_cross"
    sessions: dict[str, list[SessionWindow]]  # keyed by strategy_variant — each variant has its own schedule
    position_sizing: list[PositionSizingTier]
    execution: ExecutionConfig
    logging: LoggingConfig
    kill_switch: KillSwitchConfig
    # Optional dollar stop-loss, checked every tick alongside the existing
    # opposite-cross exit — whichever happens first closes the trade.
    # None (default) = no stop-loss, matching historical behavior
    # (docs/STRATEGY.md #6). Bot-managed, not broker-side.
    stop_loss_usd: float | None = None
    # Optional breakeven-stop: once a trade has moved this many dollars in
    # its favor, a return to the entry price becomes an additional exit
    # condition, alongside the existing take-profit and opposite-cross
    # exits. None (default) = off, matching historical behavior. Checked
    # every tick, bot-managed, not broker-side — see docs/STRATEGY.md #10.
    breakeven_trigger_usd: float | None = None
    # Optional early-entry threshold: while idle (no open position, no
    # pending setup) and the previous candle's real EMA13/21 are known, a
    # provisional EMA13/21 is recomputed on every tick using the CURRENT
    # tick price (never a stale or future value) blended with those real
    # previous values. If the provisional pair comes within this many
    # dollars of each other AND the resulting gap still qualifies as an
    # immediate entry, the trade enters right then — before that candle
    # has even closed, before the cross is formally confirmed. None
    # (default) = off. Only ever applies to the immediate-entry case; the
    # wait-for-EMA5-touch path is completely unaffected. See
    # docs/STRATEGY_PROPOSED_OPEN_GAP.md for the full design and the
    # honest (non-lookahead) verification this was built from.
    early_entry_threshold_usd: float | None = None
    # Mandatory for strategy_variant=dual_cross (None otherwise). See
    # DualCrossConfig's own docstring and docs/STRATEGY_DUAL_CROSS_SPEC.md.
    dual_cross: DualCrossConfig | None = None
    # Mandatory for strategy_variant=dual_cross_confirmed_entry (None
    # otherwise). See DualCrossConfirmedEntryConfig's own docstring.
    dual_cross_confirmed_entry: DualCrossConfirmedEntryConfig | None = None
    # Mandatory for strategy_variant=dual_cross_tight_exit (None
    # otherwise). See DualCrossTightExitConfig's own docstring.
    dual_cross_tight_exit: DualCrossTightExitConfig | None = None
    # Mandatory for strategy_variant=dual_cross_tight_exit_swap_confirm_adx
    # (None otherwise). See SwapAdxFilterConfig's own docstring.
    swap_adx_filter: SwapAdxFilterConfig | None = None


def load_config(account: str, settings_path: str | Path | None = None) -> AppConfig:
    """Loads the account-scoped config. `account` selects .env.<account> and
    config/settings.<account>.yaml (unless settings_path overrides the latter) —
    see SETUP.md. Every account is fully independent: its own MT5 credentials,
    lot sizing, sessions, logs, and kill switch."""
    account = validate_account_name(account)

    env_path = PROJECT_ROOT / f".env.{account}"
    if not env_path.exists():
        raise FileNotFoundError(
            f"{env_path} not found. Copy .env.demo1.example to {env_path.name} and fill in "
            f"real values (see SETUP.md)."
        )
    # override=True: an account's own .env.<account> must always win over
    # anything already in the environment (a stale OS-level var, or another
    # account's .env loaded earlier in the same process) — accounts must
    # stay fully independent, per SETUP.md.
    load_dotenv(env_path, override=True)

    if settings_path is None:
        settings_path = PROJECT_ROOT / "config" / f"settings.{account}.yaml"
    settings_path = Path(settings_path)
    if not settings_path.exists():
        raise FileNotFoundError(
            f"{settings_path} not found. Copy config/settings.demo1.example.yaml to "
            f"{settings_path.name} and adjust as needed (see SETUP.md)."
        )

    # Explicit encoding, not the platform default: on Windows, Python's
    # open() falls back to the locale codepage (cp1252 on this server),
    # which silently misreads any non-ASCII character (an em-dash in a
    # comment, a curly quote, etc.) and can corrupt or fail to parse an
    # otherwise-valid UTF-8 YAML file. utf-8-sig also transparently
    # strips a leading BOM if one is ever present (e.g. from a PowerShell
    # Set-Content -Encoding UTF8, which adds one by default).
    with open(settings_path, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    mt5_raw = raw.get("mt5", {})
    mt5_login = os.getenv("MT5_LOGIN")

    strategy_variant = raw.get("strategy_variant", "gap_threshold")
    sessions_raw = raw["sessions"]
    if strategy_variant not in sessions_raw:
        raise ValueError(
            f"{settings_path}: strategy_variant '{strategy_variant}' has no matching "
            f"entry under 'sessions:'. Available: {list(sessions_raw)}"
        )

    execution = ExecutionConfig(**raw["execution"])
    if execution.reject_manual_trades and execution.magic_number == 0:
        # magic_number=0 is what MT5 assigns to manual trades — with that
        # value, reject_manual_trades could never tell "ours" apart from
        # "manual" and would silently never close anything.
        raise ValueError(
            f"{settings_path}: reject_manual_trades is true but magic_number is 0 — "
            f"every manual trade would be indistinguishable from the bot's own."
        )

    dual_cross_raw = raw.get("dual_cross")
    dual_cross = DualCrossConfig(**dual_cross_raw) if dual_cross_raw is not None else None
    if strategy_variant == "dual_cross":
        # Fail fast at config-load time — the engine constructor also
        # re-checks this defensively for anyone building AppConfig
        # programmatically (e.g. tests), but a clear error here is much
        # more useful than main.py crashing three frames deeper.
        if dual_cross is None:
            raise ValueError(
                f"{settings_path}: strategy_variant is 'dual_cross' but no 'dual_cross:' "
                f"section is present — see docs/STRATEGY_DUAL_CROSS_SPEC.md."
            )
        if raw.get("stop_loss_usd") is None:
            raise ValueError(
                f"{settings_path}: strategy_variant is 'dual_cross' but stop_loss_usd is "
                f"unset — the $15 stop-loss is mandatory for this variant (spec §4a), not optional."
            )

    dual_cross_confirmed_entry_raw = raw.get("dual_cross_confirmed_entry")
    dual_cross_confirmed_entry = (
        DualCrossConfirmedEntryConfig(**dual_cross_confirmed_entry_raw)
        if dual_cross_confirmed_entry_raw is not None else None
    )
    if strategy_variant == "dual_cross_confirmed_entry":
        if dual_cross_confirmed_entry is None:
            raise ValueError(
                f"{settings_path}: strategy_variant is 'dual_cross_confirmed_entry' but no "
                f"'dual_cross_confirmed_entry:' section is present."
            )
        if raw.get("stop_loss_usd") is None:
            raise ValueError(
                f"{settings_path}: strategy_variant is 'dual_cross_confirmed_entry' but "
                f"stop_loss_usd is unset — the $ stop-loss is mandatory for this variant too."
            )

    dual_cross_tight_exit_raw = raw.get("dual_cross_tight_exit")
    dual_cross_tight_exit = (
        DualCrossTightExitConfig(**dual_cross_tight_exit_raw)
        if dual_cross_tight_exit_raw is not None else None
    )
    if strategy_variant == "dual_cross_tight_exit":
        if dual_cross_tight_exit is None:
            raise ValueError(
                f"{settings_path}: strategy_variant is 'dual_cross_tight_exit' but no "
                f"'dual_cross_tight_exit:' section is present."
            )
        if raw.get("stop_loss_usd") is None:
            raise ValueError(
                f"{settings_path}: strategy_variant is 'dual_cross_tight_exit' but "
                f"stop_loss_usd is unset — the $ stop-loss is mandatory for this variant too."
            )

    swap_adx_filter_raw = raw.get("swap_adx_filter")
    swap_adx_filter = (
        SwapAdxFilterConfig(**swap_adx_filter_raw) if swap_adx_filter_raw is not None else None
    )
    if strategy_variant == "dual_cross_tight_exit_swap_confirm_adx":
        # Reuses dual_cross_tight_exit's section unchanged (same as plain
        # swap_confirm) plus its own swap_adx_filter section.
        if dual_cross_tight_exit is None:
            raise ValueError(
                f"{settings_path}: strategy_variant is 'dual_cross_tight_exit_swap_confirm_adx' "
                f"but no 'dual_cross_tight_exit:' section is present (reused unchanged)."
            )
        if swap_adx_filter is None:
            raise ValueError(
                f"{settings_path}: strategy_variant is 'dual_cross_tight_exit_swap_confirm_adx' "
                f"but no 'swap_adx_filter:' section is present."
            )
        if raw.get("stop_loss_usd") is None:
            raise ValueError(
                f"{settings_path}: strategy_variant is 'dual_cross_tight_exit_swap_confirm_adx' but "
                f"stop_loss_usd is unset — the $ stop-loss is mandatory for this variant too."
            )

    if strategy_variant == "dual_cross_confirmed_swap_adx":
        # Deliberately does NOT require dual_cross_tight_exit — this
        # variant has no tick-based entry and no early-exit net at all
        # (see the engine's module docstring), so that section is unused.
        if swap_adx_filter is None:
            raise ValueError(
                f"{settings_path}: strategy_variant is 'dual_cross_confirmed_swap_adx' but no "
                f"'swap_adx_filter:' section is present."
            )
        if raw.get("stop_loss_usd") is None:
            raise ValueError(
                f"{settings_path}: strategy_variant is 'dual_cross_confirmed_swap_adx' but "
                f"stop_loss_usd is unset — the $ stop-loss is mandatory for this variant too."
            )

    if strategy_variant == "dual_cross_confirmed_swap_adx_entryfilter":
        # Same requirements as dual_cross_confirmed_swap_adx -- this is
        # that engine plus one extra entry filter, see
        # bot/strategy/state_machine_dual_cross_confirmed_swap_adx_entryfilter.py's
        # module docstring.
        if swap_adx_filter is None:
            raise ValueError(
                f"{settings_path}: strategy_variant is 'dual_cross_confirmed_swap_adx_entryfilter' but no "
                f"'swap_adx_filter:' section is present."
            )
        if raw.get("stop_loss_usd") is None:
            raise ValueError(
                f"{settings_path}: strategy_variant is 'dual_cross_confirmed_swap_adx_entryfilter' but "
                f"stop_loss_usd is unset — the $ stop-loss is mandatory for this variant too."
            )

    if strategy_variant == "dual_cross_confirmed_swap":
        # Deliberately does NOT require swap_adx_filter or
        # dual_cross_tight_exit — this variant has no tick-based entry, no
        # early-exit net, no swap debounce, and no ADX gate at all (see the
        # engine's module docstring).
        if raw.get("stop_loss_usd") is None:
            raise ValueError(
                f"{settings_path}: strategy_variant is 'dual_cross_confirmed_swap' but "
                f"stop_loss_usd is unset — the $ stop-loss is mandatory for this variant too."
            )

    return AppConfig(
        account=account,
        mt5=MT5Config(
            login=int(mt5_login) if mt5_login else None,
            password=os.getenv("MT5_PASSWORD") or None,
            server=os.getenv("MT5_SERVER") or None,
            terminal_path=os.getenv("MT5_TERMINAL_PATH") or None,
            timeout_ms=mt5_raw.get("timeout_ms", 10000),
        ),
        symbol=raw["symbol"],
        timeframe=raw["timeframe"],
        candles_to_fetch=raw["candles_to_fetch"],
        tick_poll_interval_seconds=raw["tick_poll_interval_seconds"],
        ema_periods=EMAPeriodsConfig(**raw["ema_periods"]),
        gap_threshold_usd=raw["gap_threshold_usd"],
        take_profit_usd=raw["take_profit_usd"],
        strategy_variant=strategy_variant,
        sessions={
            variant: [SessionWindow(**s) for s in windows]
            for variant, windows in sessions_raw.items()
        },
        position_sizing=[PositionSizingTier(**t) for t in raw["position_sizing"]],
        execution=execution,
        logging=LoggingConfig(**raw["logging"]),
        kill_switch=KillSwitchConfig(**raw["kill_switch"]),
        stop_loss_usd=raw.get("stop_loss_usd"),
        breakeven_trigger_usd=raw.get("breakeven_trigger_usd"),
        early_entry_threshold_usd=raw.get("early_entry_threshold_usd"),
        dual_cross=dual_cross,
        dual_cross_confirmed_entry=dual_cross_confirmed_entry,
        dual_cross_tight_exit=dual_cross_tight_exit,
        swap_adx_filter=swap_adx_filter,
    )


SETTINGS_FILENAME_RE = re.compile(r"^settings\.([A-Za-z0-9_-]+)\.yaml$")


def discover_configured_accounts() -> list[str]:
    """Account names with a settings.<account>.yaml present in config/ —
    used by api_server.py's unified gateway to find out which accounts to
    serve without hardcoding the list. Deliberately excludes
    *.example.yaml templates and the legacy pre-multi-account
    config/settings.yaml (neither matches the ACCOUNT_NAME_RE-validated
    group this regex requires)."""
    accounts = []
    for path in (PROJECT_ROOT / "config").glob("settings.*.yaml"):
        match = SETTINGS_FILENAME_RE.match(path.name)
        if match is None:
            continue  # e.g. settings.demo1.example.yaml, or a malformed name
        account = match.group(1)
        try:
            accounts.append(validate_account_name(account))
        except ValueError:
            continue
    return sorted(accounts)
