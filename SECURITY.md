# Security and Private Keys Setup

This document provides instructions on how private keys and secrets are handled securely within the `arb_solol` project.

## Key Management Overview

1. **No Hardcoded Secrets**: There are absolutely **no** hardcoded private keys, API keys, or seed phrases in the source code.
2. **Environment Variables (.env)**: All sensitive credentials are loaded exclusively from environment variables via the `.env` file.
3. **Git Ignore**: The `.env` file is explicitly listed in `.gitignore` to prevent any accidental commits of your private data to the repository.

## Where Keys are Handled

- **`config.py`**: This is the single source of truth for configuration. It loads variables from `.env` using `dotenv`, including:
  - `WALLET_PRIVATE_KEY`
  - `BACKPACK_API_KEY`
  - `BACKPACK_API_SECRET`
- **`wallet.py`**: Reads the `WALLET_PRIVATE_KEY` from `config.py` to authenticate on-chain transactions and balance lookups.
- **`backpack_private.py`**: Reads the `BACKPACK_API_KEY` and `BACKPACK_API_SECRET` to sign REST API requests for Backpack Exchange (e.g., getting balances).

## Instructions for Secure Usage

1. **Copy the Example Configuration**:
   Do not modify `.env.example` with your real keys, as it is tracked by Git. Instead, create a local `.env` file:
   ```bash
   cp .env.example .env
   ```
2. **Add Your Keys**:
   Open the newly created `.env` file and fill in your actual credentials.
   - `WALLET_PRIVATE_KEY`: Your Solana wallet private key (base58 string or JSON array).
   - `BACKPACK_API_KEY` / `BACKPACK_API_SECRET`: Your Backpack Exchange API keys.
3. **Never Share Your `.env`**:
   Keep your `.env` file completely private. Never share it with anyone or upload it anywhere.

By following these practices, your private keys remain secure and are never exposed in the source code or Git history.
