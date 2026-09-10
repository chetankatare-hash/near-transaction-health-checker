#!/usr/bin/env python3
"""
Test suite for the NEAR Transaction Health Checker skill.

This sandbox has no outbound network access, so live HTTP calls to NEAR RPC
/ NearBlocks can't be made from here directly. To still prove the skill
works against REAL blockchain data (not invented data), this script:

  1. Loads a real transaction payload that was fetched live from the
     NearBlocks public API just before writing this skill
     (see sample_data/real_tx_2Bh8uAqF.json — fetched from
     https://api.nearblocks.io/v1/account/v2.keypom.near/txns?method=create_drop
     on 2026-09-10). That transaction is real, mainnet, and confirmed:
     https://nearblocks.io/txns/2Bh8uAqFLwfUic7hmFXXYsThLsCckLHU8UQq6c2thi5u

  2. Feeds that real payload through the skill's actual parsing/formatting
     code (_report_from_nearblocks, format_report) by monkeypatching only
     the network boundary (nearblocks_lookup_tx / rpc calls) — none of the
     skill's business logic is mocked.

  3. Also runs a synthetic-but-realistic NEAR RPC "Failure" payload (shaped
     exactly like real NEAR RPC responses) through the primary RPC parsing
     path, to prove failure-explanation logic works.

  4. Runs the input-validation and "not found" edge cases.

In a normal (networked) deployment, skill.py talks to NEAR RPC and
NearBlocks directly — no monkeypatching needed there.
"""

import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import skill  # noqa: E402


def line(title):
    print("\n" + "#" * 70)
    print("# " + title)
    print("#" * 70)


# ---------------------------------------------------------------------
# TEST 1: Real, live-fetched, SUCCESSFUL mainnet transaction
#         (via the NearBlocks fallback path)
# ---------------------------------------------------------------------
line("TEST 1 — Real mainnet transaction (SUCCESS) via NearBlocks data")

real_tx_path = Path(__file__).parent / "sample_data" / "real_tx_2Bh8uAqF.json"
real_tx = json.loads(real_tx_path.read_text())

print(f"Loaded real transaction: {real_tx['transaction_hash']}")
print(f"(Verifiable at: https://nearblocks.io/txns/{real_tx['transaction_hash']})")

with mock.patch.object(skill, "rpc_get_tx_status", side_effect=skill.RpcUnavailableError("simulated: no network in this sandbox")), \
     mock.patch.object(skill, "nearblocks_lookup_tx", return_value=real_tx):
    report1 = skill.check_transaction(
        real_tx["transaction_hash"],
        sender_account_id=None,  # force auto-resolution from the payload
        network="mainnet",
    )

output1 = skill.format_report(report1)
print(output1)

assert report1.found is True
assert report1.success is True
assert report1.sender == "dragov.near"
assert report1.receiver == "v2.keypom.near"
assert report1.block_height == 159173238
assert report1.gas_burnt_tgas == round(5378796884351 / skill.TGAS, 4)
print("\n[PASS] Real successful transaction parsed and formatted correctly.")


# ---------------------------------------------------------------------
# TEST 2: Realistic FAILED transaction via the primary RPC path
#         (payload shaped exactly like a real NEAR RPC `tx` response,
#          based on NEAR's documented FunctionCallError/GuestPanic format)
# ---------------------------------------------------------------------
line("TEST 2 — Simulated NEAR RPC response for a FAILED transaction")

failed_rpc_result = {
    "transaction": {
        "signer_id": "alice.near",
        "receiver_id": "guest-book.near",
    },
    "transaction_outcome": {
        "block_hash": "FAKEBLOCKHASHFORTESTINGxxxxxxxxxxxxxxxxxxx",
        "outcome": {"gas_burnt": 2428030339426, "tokens_burnt": "242803033942600000000"},
    },
    "receipts_outcome": [
        {"outcome": {"gas_burnt": 2214887462834, "tokens_burnt": "221488746283400000000"}}
    ],
    "status": {
        "Failure": {
            "ActionError": {
                "index": 0,
                "kind": {
                    "FunctionCallError": {
                        "ExecutionError": "Smart contract panicked: message length exceeds 100 characters"
                    }
                },
            }
        }
    },
}

with mock.patch.object(skill, "rpc_get_tx_status", return_value=failed_rpc_result), \
     mock.patch.object(skill, "rpc_get_block_height", return_value=112233445):
    report2 = skill.check_transaction(
        "3f8VXtjkn5FsEqpKFbZU6A42K3eKJ3Ab5yQLSAmvKFnH",  # real-format hash, used only as an example ID here
        sender_account_id="alice.near",
        network="mainnet",
    )

output2 = skill.format_report(report2)
print(output2)

assert report2.found is True
assert report2.success is False
assert report2.failure_kind == "FunctionCallError"
assert "panicked" in report2.explanation.lower() or "contract" in report2.explanation.lower()
print("\n[PASS] Failed transaction correctly explained in plain language.")


# ---------------------------------------------------------------------
# TEST 3: Invalid transaction hash (graceful handling)
# ---------------------------------------------------------------------
line("TEST 3 — Invalid transaction hash input")

report3 = skill.check_transaction("not-a-real-hash!!!", sender_account_id="alice.near")
output3 = skill.format_report(report3)
print(output3)

assert report3.found is False
assert "doesn't look like" in report3.error or "base58" in report3.error
print("\n[PASS] Invalid hash handled gracefully, no crash.")


# ---------------------------------------------------------------------
# TEST 4: Transaction not found anywhere (RPC + NearBlocks both miss)
# ---------------------------------------------------------------------
line("TEST 4 — Unknown/nonexistent transaction (RPC + fallback both fail)")

with mock.patch.object(skill, "rpc_get_tx_status", side_effect=skill.TransactionNotFoundError(
        "The NEAR network has no record of this transaction. "
        "It may not exist, may not have been indexed yet, or the sender account is wrong.")):
    report4 = skill.check_transaction(
        "1111111111111111111111111111111111111111z",
        sender_account_id="alice.near",
        network="mainnet",
    )

output4 = skill.format_report(report4)
print(output4)

assert report4.found is False
assert "no record" in report4.error.lower()
print("\n[PASS] Unknown transaction handled gracefully, no crash.")


# ---------------------------------------------------------------------
# TEST 5: Full IronClaw execute() entrypoint, real transaction
# ---------------------------------------------------------------------
line("TEST 5 — IronClaw execute() entrypoint (real transaction, sender omitted)")

with mock.patch.object(skill, "rpc_get_tx_status", side_effect=skill.RpcUnavailableError("simulated: no network in this sandbox")), \
     mock.patch.object(skill, "nearblocks_lookup_tx", return_value=real_tx):
    result = skill.execute({"tx_hash": real_tx["transaction_hash"], "network": "mainnet"})

print(json.dumps({k: v for k, v in result.items() if k != "data"}, indent=2))
print("\n(data field omitted above for brevity, but is fully populated: "
      f"{len(result['data'])} fields)")

assert result["ok"] is True
assert result["success"] is True
assert "SUCCESS" in result["message"]
print("\n[PASS] IronClaw execute() entrypoint works end-to-end.")

line("ALL TESTS PASSED")
