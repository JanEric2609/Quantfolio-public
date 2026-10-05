# Security

## Reporting a vulnerability

Please report it privately through GitHub: the repository's **Security** tab → **Report a vulnerability**. Do not open a public issue. Include what you found, how to reproduce it, and what it lets an attacker do. You will get an answer within a week; this is a personal project, so there is no bug bounty.

## What the app promises

- **Read-only access to money.** DKB is reached over FinTS for account information only; no payment-initiation call exists in the code, and every TAN is approved by the account holder in the DKB app. Scalable Capital is reached only through a root-owned wrapper (`infra/scalable/quantfolio-sc-ro`) that allows a fixed list of read commands, with a read-only login and an empty trade allow-list. A report that either can be made to move money or place an order is the most serious kind.
- **Secrets at rest are encrypted** with Fernet (`backend/app/foundation/core/security.py`), are never logged, and never sit in plain settings.
- **Outbound requests** to user-supplied URLs (news feeds, LLM endpoints) pass an SSRF guard that refuses private and link-local addresses where the feature is not meant to reach the local network.
- **Production refuses weak defaults:** with `APP_ENV=production` or a PostgreSQL database, the app will not start on the development JWT secret or without an encryption key.

## Scope

In scope: the backend, the frontend, the wrapper and the deployment scripts in this repository. Out of scope: your own deployment's network, the third-party services the app talks to, and findings that need an already-compromised admin account or host.
