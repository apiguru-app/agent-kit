#!/usr/bin/env python3
"""Generate a fresh Ethereum wallet for receiving x402 payments.

Read this before you run it -- it creates something that will hold real money.

What it does:
  * Generates a BIP-39 mnemonic using the operating system's cryptographic
    RNG (os.urandom, via eth_account). Not a password, not a hash of
    anything, not derived from this machine's state.
  * Derives the standard Ethereum account (m/44'/60'/0'/0/0) from it.
  * Writes the secret to a file you name, and prints ONLY the public address.

What it deliberately does NOT do:
  * Print the mnemonic or private key to the terminal. Terminal output gets
    scrolled, screenshotted, copied into chats and captured by logs. The
    secret goes to a file and nowhere else.
  * Overwrite an existing file. It refuses, so a second run cannot silently
    destroy a wallet that already holds funds.
  * Touch the network. Generation is entirely offline.

Usage:
    pip install eth-account
    python tools/new_wallet.py --out "C:/Users/you/keys/apiguru-x402-wallet.txt"

This produces a plain EOA: no contract to deploy, no passkey, no account
abstraction. Import the mnemonic into MetaMask, Rabby or Coinbase Wallet
whenever you want to spend the funds -- it is a standard 12-word phrase and
every wallet accepts it.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a fresh EOA for receiving x402 payments."
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Where to write the secret. Choose somewhere outside this repo.",
    )
    parser.add_argument(
        "--label",
        default="Apiguru x402 revenue wallet",
        help="A note stored alongside the secret so future-you knows what it is.",
    )
    args = parser.parse_args(argv)

    out = pathlib.Path(args.out).expanduser()
    if out.exists():
        print(
            f"REFUSING: {out} already exists.\n"
            "Overwriting could destroy a wallet that holds funds. Pick another "
            "path, or move the existing file first.",
            file=sys.stderr,
        )
        return 1

    try:
        from eth_account import Account
    except ImportError:
        print("Missing dependency. Run: pip install eth-account", file=sys.stderr)
        return 2

    # Mnemonic generation is flagged unaudited upstream; the derivation path
    # below is the ubiquitous Ethereum standard and is what every wallet app
    # uses to import a 12-word phrase.
    Account.enable_unaudited_hdwallet_features()
    account, mnemonic = Account.create_with_mnemonic(num_words=12)

    out.parent.mkdir(parents=True, exist_ok=True)
    body = f"""{args.label}
Created: {datetime.now(timezone.utc).isoformat(timespec="seconds")}

ADDRESS (public, safe to share):
{account.address}

RECOVERY PHRASE (SECRET - anyone with this owns the funds):
{mnemonic}

PRIVATE KEY (SECRET - equivalent to the phrase above):
{account.key.hex()}

Derivation path: m/44'/60'/0'/0/0   (standard Ethereum)

WHAT TO DO NOW
1. Write the 12-word recovery phrase on paper, or put it in a password
   manager. That is your only backup - nobody can restore it for you.
2. Delete this file once it is backed up somewhere safe.
3. To spend the funds later, import the phrase into MetaMask, Rabby or
   Coinbase Wallet. Any wallet accepts it.

The address is only ever RECEIVING here. The private key never needs to go
on the server, into the repo, or into any config file.
"""
    out.write_text(body, encoding="utf-8")

    try:
        out.chmod(0o600)  # no-op on most Windows setups, meaningful elsewhere
    except OSError:
        pass

    print(f"Wallet created.\n")
    print(f"  ADDRESS : {account.address}")
    print(f"  SECRET  : written to {out}")
    print(
        "\nThe recovery phrase was NOT printed here on purpose. Open that file,"
        "\nback up the 12 words, then delete it."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
