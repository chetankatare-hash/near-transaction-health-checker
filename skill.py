#!/usr/bin/env python3
"""
NEAR Transaction Health Checker
================================
An IronClaw custom skill that inspects a NEAR Protocol transaction and
explains, in plain language, whether it succeeded or failed and why.

Primary data source: NEAR RPC (JSON-RPC `tx` method) — the authoritative,
on-chain source of truth.

Fallback data source: NearBlocks public REST API (GET-only) — used to
resolve the sender account when it isn't supplied, and as a backup if the
RPC endpoint is unreachable or rate-limited.

This file is self-contained and has no IronClaw-specific import
dependency, so it works both as:
  1. A registered IronClaw skill (via `execute(params)`).
  2. A standalone CLI tool (`python skill.py <tx_hash> [--sender ...]`).
  3. A library (`from skill import check_transaction`).
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

RPC_ENDPOINTS = {
    "mainnet": [
        "https://rpc.mainnet.near.org",
        "https://near.lava.build",
    ],
    "testnet": [
        "https://rpc.testnet.near.org",
        "https://near-testnet.lava.build",
    ],
}

NEARBLOCKS_BASE = {
    "mainnet": "https://api.nearblocks.io",
    "testnet": "https://api-testnet.nearblocks.io",
}

REQUEST_TIMEOUT = 12
TGAS = 10**12
YOCTO_PER_NEAR = 10**24

# Base58 alphabet NEAR hashes/account keys are encoded with.
_BASE58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class NearHealthCheckerError(Exception):
    """Base error for this skill."""


class InvalidTxHashError(NearHealthCheckerError):
    pass


class RpcUnavailableError(NearHealthCheckerError):
    pass


class TransactionNotFoundError(NearHealthCheckerError):
    pass


# --------------------------------------------------------------------------
# Human-readable explanations for common on-chain failure kinds
# --------------------------------------------------------------------------
# Keys are matched against the JSON key names NEAR uses inside
# `status.Failure`. Each entry gives a plain-language reason and a concrete
# next step the user can check.

FAILURE_EXPLANATIONS = {
    "AccountDoesNotExist": (
        "The transaction tried to interact with an account that doesn't exist on-chain.",
        "Double-check the receiver account ID for typos, and confirm it has actually been created.",
    ),
    "ActorNoPermission": (
        "The signer doesn't have permission to perform this action on the target account.",
        "Verify you're using an access key that has the right permissions for this account.",
    ),
    "LackBalanceForState": (
        "The account doesn't hold enough NEAR to cover storage/state costs for this action.",
        "Top up the account's balance, or reduce the amount of data/storage the transaction writes.",
    ),
    "NotEnoughBalance": (
        "The sender doesn't have enough NEAR to cover the transferred amount plus gas fees.",
        "Check the account's available balance and lower the amount or top it up before retrying.",
    ),
    "TriesToUnstake": (
        "The account tried to unstake more than it currently has staked.",
        "Check the account's current staked balance before submitting an unstake request.",
    ),
    "TriesToStake": (
        "The staking action failed validation (e.g. staking below the minimum amount).",
        "Confirm the stake amount meets the validator/staking pool's minimum requirements.",
    ),
    "InsufficientStake": (
        "The stake amount is below what's required to become/remain a validator.",
        "Increase the staked amount to meet the network's minimum stake threshold.",
    ),
    "AccountAlreadyExists": (
        "The transaction tried to create an account that already exists.",
        "Use a different account ID, or skip account creation if it already exists.",
    ),
    "CreateAccountOnlyByRegistrar": (
        "Only the network's registrar contract is allowed to create this type of account.",
        "Top-level accounts must be created via the registrar/marketplace flow, not directly.",
    ),
    "CreateAccountNotAllowed": (
        "This predecessor account isn't allowed to create the requested sub-account.",
        "You can only create direct sub-accounts of an account you control (e.g. `x.yourname.near`).",
    ),
    "DeleteAccountStaking": (
        "The account can't be deleted while it still has an active staking position.",
        "Unstake and withdraw all funds from the account before attempting to delete it.",
    ),
    "DeleteKeyDoesNotExist": (
        "The transaction tried to remove an access key that isn't on the account.",
        "Check the public key you're trying to delete — it may already be removed.",
    ),
    "AddKeyAlreadyExists": (
        "The transaction tried to add an access key that's already registered to the account.",
        "Use a different key pair, or skip adding the key since it's already present.",
    ),
    "DeleteAccountHasRent": (
        "The account has funds/state remaining and can't be deleted this way.",
        "Withdraw remaining balance and clear stored data before deleting the account.",
    ),
    "RentUnpaid": (
        "The account ran out of balance to pay for its own storage.",
        "Send more NEAR to the account to cover ongoing storage costs.",
    ),
    "TriesToRemoveLastAccessKey": (
        "The transaction would remove the last access key, permanently locking the account.",
        "Add a new access key before removing the old one, or keep at least one key on the account.",
    ),
    "OnlyImplicitAccountCreationAllowed": (
        "Only implicit (64-char hex) account creation is allowed here; named account creation is blocked.",
        "Use an implicit account, or create named accounts through the standard registrar flow.",
    ),
    "FunctionCallError": (
        "The smart contract call itself failed (the contract code returned an error or panicked).",
        "Check the contract's expected arguments, your attached deposit/gas, and the method name for typos.",
    ),
    "CompilationError": (
        "The target contract's WASM code failed to compile/load.",
        "The contract itself may be broken or not properly deployed — verify the contract account.",
    ),
    "MethodResolveError": (
        "The method you called doesn't exist on the target contract.",
        "Double-check the method name and confirm the contract actually exposes it.",
    ),
    "WasmTrap": (
        "The contract crashed while executing (an internal WASM runtime error, e.g. out-of-bounds access).",
        "This is usually a contract bug. Report it to the contract's developers with your input arguments.",
    ),
    "GuestPanic": (
        "The contract explicitly panicked / rejected the call, usually due to a failed internal check.",
        "Read the panic message above for the exact reason, then adjust your call's inputs accordingly.",
    ),
    "GasExceeded": (
        "The transaction ran out of attached gas before it could finish executing.",
        "Retry with more attached gas (many wallets/dApps default to 30 TGas, but complex calls need more).",
    ),
    "GasLimitExceeded": (
        "The call requested more gas than the network allows in a single transaction.",
        "Split the work into multiple smaller transactions/cross-contract calls.",
    ),
    "BalanceExceeded": (
        "The contract tried to use more NEAR balance than it has available.",
        "Check the contract's balance — it may need a top-up to complete this operation.",
    ),
    "InvalidAccessKeyError": (
        "The access key used to sign this transaction is invalid for this action.",
        "Reconnect your wallet or re-derive the key — it may be the wrong key or lack permissions.",
    ),
    "AccessKeyNotFound": (
        "The access key used to sign wasn't found on the sender's account.",
        "The key may have been removed or rotated — reconnect your wallet with a valid key.",
    ),
    "ReceiverMismatch": (
        "This access key is only allowed to call a specific contract, and it doesn't match the receiver.",
        "Use a full-access key, or a function-call key scoped to this exact receiver.",
    ),
    "NotEnoughAllowance": (
        "This function-call access key has run out of its allowance to pay for gas.",
        "Add a new access key with a fresh allowance, or use a full-access key.",
    ),
    "RequiresFullAccess": (
        "This action (e.g. adding/removing keys, staking) requires a full-access key.",
        "Sign the transaction with a full-access key rather than a limited function-call key.",
    ),
    "MethodNameMismatch": (
        "This access key is restricted to certain method names, and the one called isn't allowed.",
        "Use a key that's permitted to call this method, or use a full-access key.",
    ),
    "DepositWithFunctionCall": (
        "This access key isn't allowed to attach a NEAR deposit to function calls.",
        "Use a full-access key if the call needs to attach a deposit.",
    ),
    "InvalidNonce": (
        "The transaction's nonce is invalid — usually stale or reused.",
        "Refresh your wallet/session and resubmit; the nonce needs to be higher than the account's last used nonce.",
    ),
    "Expired": (
        "The transaction expired before it could be included in a block.",
        "This often happens after long delays signing — just retry the transaction.",
    ),
    "InvalidReceiver": (
        "The receiver account ID in this transaction is not a valid NEAR account ID.",
        "Check the receiver ID for typos or invalid characters.",
    ),
    "InvalidSignerId": (
        "The sender account ID in this transaction is not a valid NEAR account ID.",
        "Check the sender ID for typos or invalid characters.",
    ),
    "NotEnoughBalanceForActionError": (
        "The account doesn't have enough NEAR to cover the cost of this action.",
        "Top up the sender's account before retrying.",
    ),
}

# Fallback text used when the exact failure kind isn't in our lookup table.
GENERIC_FAILURE_EXPLANATION = (
    "The transaction was rejected by the network or by a contract along the execution path.",
    "Check the raw error details below, and confirm the receiver account, method name, "
    "deposit, and gas amount are all correct.",
)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class TxReport:
    tx_hash: str
    network: str
    found: bool = False
    success: Optional[bool] = None
    sender: Optional[str] = None
    receiver: Optional[str] = None
    block_height: Optional[int] = None
    block_hash: Optional[str] = None
    gas_burnt_tgas: Optional[float] = None
    tokens_burnt_near: Optional[float] = None
    success_value_decoded: Optional[str] = None
    failure_kind: Optional[str] = None
    failure_raw: Optional[Any] = None
    explanation: Optional[str] = None
    suggestion: Optional[str] = None
    data_source: str = ""
    warnings: list = field(default_factory=list)
    error: Optional[str] = None


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_tx_hash(tx_hash: str) -> str:
    """Loosely validate a NEAR transaction hash (base58, ~43-44 chars)."""
    if not tx_hash or not isinstance(tx_hash, str):
        raise InvalidTxHashError("No transaction hash was provided.")
    tx_hash = tx_hash.strip()
    if len(tx_hash) < 32 or len(tx_hash) > 48:
        raise InvalidTxHashError(
            f"'{tx_hash}' doesn't look like a NEAR transaction hash "
            "(expected a base58 string around 43-44 characters long)."
        )
    if not all(c in _BASE58_ALPHABET for c in tx_hash):
        raise InvalidTxHashError(
            f"'{tx_hash}' contains characters that aren't valid base58, "
            "so it can't be a real NEAR transaction hash."
        )
    return tx_hash


# --------------------------------------------------------------------------
# NEAR RPC access (primary source)
# --------------------------------------------------------------------------

def _rpc_call(method: str, params: Any, network: str, timeout: int = REQUEST_TIMEOUT) -> dict:
    if requests is None:
        raise RpcUnavailableError("The 'requests' library isn't installed.")

    last_error = None
    for endpoint in RPC_ENDPOINTS.get(network, []):
        payload = {"jsonrpc": "2.0", "id": "near-tx-health-checker", "method": method, "params": params}
        try:
            resp = requests.post(endpoint, json=payload, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - we deliberately try the next endpoint
            last_error = exc
            continue

        if "error" in data:
            err = data["error"]
            cause = (err.get("cause") or {}).get("name", "")
            if cause == "UNKNOWN_TRANSACTION" or "does not exist" in json.dumps(err).lower():
                raise TransactionNotFoundError(
                    "The NEAR network has no record of this transaction. "
                    "It may not exist, may not have been indexed yet, or the sender account is wrong."
                )
            last_error = NearHealthCheckerError(json.dumps(err))
            continue

        return data.get("result", {})

    raise RpcUnavailableError(
        f"Couldn't reach any NEAR RPC endpoint for '{network}'. Last error: {last_error}"
    )


def rpc_get_tx_status(tx_hash: str, sender_account_id: str, network: str) -> dict:
    """Calls the NEAR RPC `tx` method — the authoritative on-chain result."""
    return _rpc_call(
        "tx",
        {"tx_hash": tx_hash, "sender_account_id": sender_account_id, "wait_until": "EXECUTED"},
        network,
    )


def rpc_get_block_height(block_hash: str, network: str) -> Optional[int]:
    try:
        result = _rpc_call("block", {"block_id": block_hash}, network)
        return result.get("header", {}).get("height")
    except Exception:  # noqa: BLE001 - block height is a nice-to-have, not critical
        return None


# --------------------------------------------------------------------------
# NearBlocks access (fallback / sender-resolution source)
# --------------------------------------------------------------------------

def nearblocks_lookup_tx(tx_hash: str, network: str) -> Optional[dict]:
    """Looks up a transaction via NearBlocks' public GET API.

    Used to (a) resolve the sender account when the user didn't provide one,
    and (b) as a fallback data source if the RPC endpoints are unreachable.
    """
    if requests is None:
        return None
    base = NEARBLOCKS_BASE.get(network)
    if not base:
        return None
    url = f"{base}/v1/txns/{tx_hash}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None

    # NearBlocks nests the transaction under "txns": [...]
    txns = data.get("txns") if isinstance(data, dict) else None
    if txns:
        return txns[0]
    if isinstance(data, dict) and data.get("transaction_hash"):
        return data
    return None


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def _decode_success_value(value: str) -> Optional[str]:
    if not value:
        return None
    try:
        raw = base64.b64decode(value)
        return raw.decode("utf-8")
    except Exception:  # noqa: BLE001
        return None


_FAILURE_WRAPPER_KEYS = {"ActionError", "InvalidTxError"}


def _find_failure_kind(failure_obj: Any) -> tuple[Optional[str], Any]:
    """Walks a NEAR `Failure` status object to find the specific error kind name.

    Typical shapes:
      {"ActionError": {"index": 0, "kind": {"FunctionCallError": {"ExecutionError": "..."}}}}
      {"InvalidTxError": {"InvalidAccessKeyError": {"AccessKeyNotFound": {...}}}}
      {"InvalidTxError": "Expired"}

    Only known "wrapper" keys (ActionError / InvalidTxError) are unwrapped; the
    first non-wrapper key found is treated as the actual error kind, so we
    don't accidentally descend into a contract's own error payload (e.g. the
    "ExecutionError" string inside a FunctionCallError).
    """
    node = failure_obj
    kind_name = None
    inner: Any = node
    while isinstance(node, dict) and len(node) == 1:
        key, value = next(iter(node.items()))
        if key in _FAILURE_WRAPPER_KEYS:
            node = value.get("kind", value) if isinstance(value, dict) else value
            continue
        kind_name = key
        inner = value
        break
    else:
        if isinstance(node, str):
            kind_name = node
            inner = node
    return kind_name, inner


def explain_failure(failure_obj: Any) -> tuple[str, str, str]:
    """Returns (kind_name, explanation, suggestion) for a Failure status object."""
    kind_name, inner = _find_failure_kind(failure_obj)
    explanation, suggestion = FAILURE_EXPLANATIONS.get(kind_name, GENERIC_FAILURE_EXPLANATION)

    extra = ""
    if kind_name == "FunctionCallError" and isinstance(inner, dict):
        exec_error = inner.get("ExecutionError")
        if exec_error:
            extra = f" Contract error message: \"{exec_error}\""
    return kind_name or "UnknownError", explanation + extra, suggestion


# --------------------------------------------------------------------------
# Core logic
# --------------------------------------------------------------------------

def _report_from_rpc_result(tx_hash: str, network: str, result: dict, warnings: list) -> TxReport:
    report = TxReport(tx_hash=tx_hash, network=network, found=True, data_source="NEAR RPC")
    report.warnings = warnings

    transaction = result.get("transaction", {})
    report.sender = transaction.get("signer_id")
    report.receiver = transaction.get("receiver_id")

    outcome = result.get("transaction_outcome", {}).get("outcome", {})
    report.block_hash = result.get("transaction_outcome", {}).get("block_hash")

    total_gas = outcome.get("gas_burnt", 0)
    total_tokens = int(outcome.get("tokens_burnt", 0) or 0)
    for r in result.get("receipts_outcome", []):
        r_outcome = r.get("outcome", {})
        total_gas += r_outcome.get("gas_burnt", 0)
        total_tokens += int(r_outcome.get("tokens_burnt", 0) or 0)
    report.gas_burnt_tgas = round(total_gas / TGAS, 4)
    report.tokens_burnt_near = round(total_tokens / YOCTO_PER_NEAR, 8)

    status = result.get("status", {})
    if "SuccessValue" in status:
        report.success = True
        report.success_value_decoded = _decode_success_value(status["SuccessValue"])
    elif "SuccessReceiptId" in status:
        report.success = True
    elif "Failure" in status:
        report.success = False
        report.failure_raw = status["Failure"]
        kind, explanation, suggestion = explain_failure(status["Failure"])
        report.failure_kind = kind
        report.explanation = explanation
        report.suggestion = suggestion
    else:
        report.warnings.append("Transaction status was in an unrecognized format.")

    if report.block_hash:
        height = rpc_get_block_height(report.block_hash, network)
        if height:
            report.block_height = height

    return report


def _report_from_nearblocks(tx_hash: str, network: str, tx: dict, warnings: list) -> TxReport:
    report = TxReport(tx_hash=tx_hash, network=network, found=True, data_source="NearBlocks API (fallback)")
    report.warnings = warnings + [
        "Live NEAR RPC was unavailable, so this report was built from the NearBlocks "
        "explorer API instead. Core success/failure status is still accurate, but some "
        "fields (like the exact contract error message) may be less detailed."
    ]

    report.sender = tx.get("predecessor_account_id") or tx.get("signer_account_id")
    report.receiver = tx.get("receiver_account_id")

    block = tx.get("block") or tx.get("receipt_block") or {}
    report.block_height = block.get("block_height")
    report.block_hash = tx.get("included_in_block_hash")

    outcome_status = tx.get("outcomes", {}).get("status")
    if outcome_status is None:
        outcome_status = (tx.get("receipt_outcome") or {}).get("status")
    report.success = bool(outcome_status)

    gas_burnt = (tx.get("receipt_outcome") or {}).get("gas_burnt")
    if gas_burnt is not None:
        report.gas_burnt_tgas = round(gas_burnt / TGAS, 4)

    fee = (tx.get("outcomes_agg") or {}).get("transaction_fee")
    if fee is not None:
        report.tokens_burnt_near = round(int(fee) / YOCTO_PER_NEAR, 8)

    if report.success is False:
        report.failure_kind = "Unknown (see NearBlocks explorer for full details)"
        report.explanation = (
            "NearBlocks recorded this transaction's execution as failed, but this fallback "
            "data source doesn't expose the full structured error. "
        )
        report.suggestion = (
            f"For the exact failure reason, view this transaction directly on the explorer: "
            f"https://{'testnet.' if network == 'testnet' else ''}nearblocks.io/txns/{tx_hash} "
            "or re-run this check once the NEAR RPC endpoint is reachable."
        )

    return report


def check_transaction(
    tx_hash: str,
    sender_account_id: Optional[str] = None,
    network: str = "mainnet",
) -> TxReport:
    """Main entrypoint: analyzes a NEAR transaction and returns a TxReport."""
    warnings: list = []

    try:
        tx_hash = validate_tx_hash(tx_hash)
    except InvalidTxHashError as exc:
        report = TxReport(tx_hash=tx_hash or "", network=network, found=False)
        report.error = str(exc)
        return report

    nb_tx = None
    if not sender_account_id:
        nb_tx = nearblocks_lookup_tx(tx_hash, network)
        if nb_tx:
            sender_account_id = nb_tx.get("predecessor_account_id") or nb_tx.get("signer_account_id")
        if not sender_account_id:
            report = TxReport(tx_hash=tx_hash, network=network, found=False)
            report.error = (
                "No sender account was provided, and it couldn't be auto-resolved from the "
                "explorer API. NEAR's RPC requires the sender account to look up a transaction — "
                "please supply it (e.g. --sender alice.near)."
            )
            return report

    # 1) Try the authoritative RPC path first.
    try:
        result = rpc_get_tx_status(tx_hash, sender_account_id, network)
        return _report_from_rpc_result(tx_hash, network, result, warnings)
    except TransactionNotFoundError as exc:
        report = TxReport(tx_hash=tx_hash, network=network, found=False)
        report.error = str(exc)
        return report
    except RpcUnavailableError as exc:
        warnings.append(f"NEAR RPC unavailable ({exc}); falling back to NearBlocks.")
    except NearHealthCheckerError as exc:
        warnings.append(f"NEAR RPC returned an error ({exc}); falling back to NearBlocks.")

    # 2) Fall back to NearBlocks if RPC failed for any non-fatal reason.
    if nb_tx is None:
        nb_tx = nearblocks_lookup_tx(tx_hash, network)
    if nb_tx:
        return _report_from_nearblocks(tx_hash, network, nb_tx, warnings)

    report = TxReport(tx_hash=tx_hash, network=network, found=False)
    report.warnings = warnings
    report.error = (
        "Couldn't retrieve this transaction from NEAR RPC or from the NearBlocks fallback API. "
        "Please double check the transaction hash, sender account, and network (mainnet/testnet), "
        "or try again in a moment."
    )
    return report


# --------------------------------------------------------------------------
# Output formatting
# --------------------------------------------------------------------------

def format_report(report: TxReport) -> str:
    lines = []

    if not report.found:
        lines.append("NEAR Transaction Health Checker")
        lines.append("=" * 32)
        lines.append(f"Transaction hash : {report.tx_hash or '(none provided)'}")
        lines.append(f"Network          : {report.network}")
        lines.append("")
        lines.append("Result: COULD NOT BE CHECKED")
        lines.append(f"Reason: {report.error}")
        return "\n".join(lines)

    status_word = "SUCCESS" if report.success else "FAILED"
    icon = "✅" if report.success else "❌"

    lines.append("NEAR Transaction Health Checker")
    lines.append("=" * 32)
    lines.append(f"Transaction hash : {report.tx_hash}")
    lines.append(f"Network          : {report.network}")
    lines.append(f"Data source      : {report.data_source}")
    lines.append("")
    lines.append(f"{icon} Status: {status_word}")
    lines.append("")
    lines.append(f"Sender           : {report.sender or 'unknown'}")
    lines.append(f"Receiver         : {report.receiver or 'unknown'}")
    if report.block_height is not None:
        lines.append(f"Block height     : {report.block_height:,}")
    if report.block_hash:
        lines.append(f"Block hash       : {report.block_hash}")
    if report.gas_burnt_tgas is not None:
        lines.append(f"Gas used         : {report.gas_burnt_tgas} TGas")
    if report.tokens_burnt_near is not None:
        lines.append(f"Fee paid         : {report.tokens_burnt_near} NEAR")

    lines.append("")
    if report.success:
        lines.append("Summary: This transaction executed successfully and was confirmed on-chain.")
        if report.success_value_decoded:
            lines.append(f"Return value: {report.success_value_decoded}")
    else:
        lines.append(f"What went wrong ({report.failure_kind}):")
        lines.append(f"  {report.explanation}")
        lines.append("")
        lines.append("What to check next:")
        lines.append(f"  {report.suggestion}")
        if report.failure_raw:
            lines.append("")
            lines.append("Raw error details (for debugging):")
            lines.append(f"  {json.dumps(report.failure_raw)}")

    if report.warnings:
        lines.append("")
        lines.append("Notes:")
        for w in report.warnings:
            lines.append(f"  - {w}")

    return "\n".join(lines)


def report_to_dict(report: TxReport) -> dict:
    return {k: v for k, v in report.__dict__.items()}


# --------------------------------------------------------------------------
# IronClaw skill entrypoint
# --------------------------------------------------------------------------
# IronClaw (and most custom-skill runners) invoke a skill by calling a
# single `execute(params)` function with a plain dict of arguments, and
# expect a plain dict back. This keeps the skill runner-agnostic.

SKILL_NAME = "near_tx_health_checker"


def execute(params: dict) -> dict:
    """IronClaw skill entrypoint.

    Expected params:
        tx_hash (str, required)        - the NEAR transaction hash to check
        sender_account_id (str, opt.)  - the account that signed the tx
        network (str, opt.)            - "mainnet" (default) or "testnet"

    Returns a dict:
        {
          "ok": bool,               # True if the transaction was found & analyzed
          "success": bool | None,   # transaction's own success/failure (None if not found)
          "message": str,           # human-readable report, ready to display
          "data": {...}             # structured fields for programmatic use
        }
    """
    tx_hash = params.get("tx_hash") or params.get("hash") or params.get("transaction_hash")
    sender = params.get("sender_account_id") or params.get("sender")
    network = (params.get("network") or "mainnet").lower()
    if network not in RPC_ENDPOINTS:
        network = "mainnet"

    report = check_transaction(tx_hash, sender_account_id=sender, network=network)
    message = format_report(report)

    return {
        "ok": report.found,
        "success": report.success,
        "message": message,
        "data": report_to_dict(report),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="near-tx-health-checker",
        description="Analyze a NEAR blockchain transaction and explain its result in plain language.",
    )
    parser.add_argument("tx_hash", help="The NEAR transaction hash to check")
    parser.add_argument("--sender", dest="sender", default=None, help="Sender account ID (optional; auto-resolved if omitted)")
    parser.add_argument("--network", dest="network", default="mainnet", choices=["mainnet", "testnet"])
    parser.add_argument("--json", dest="as_json", action="store_true", help="Print raw JSON instead of a formatted report")
    args = parser.parse_args()

    report = check_transaction(args.tx_hash, sender_account_id=args.sender, network=args.network)

    if args.as_json:
        print(json.dumps(report_to_dict(report), indent=2, default=str))
    else:
        print(format_report(report))

    sys.exit(0 if report.found else 1)


if __name__ == "__main__":
    main()
