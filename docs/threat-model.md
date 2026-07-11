# Threat Model (Initial)

## Key Risks
- Unauthorized Telegram users triggering workflows.
- Prompt injection via chat content.
- Destructive command execution by agent runtime.
- Secret exfiltration from runtime environment.

## Controls in this slice
- Allowlist-only Telegram user authorization.
- Deny-by-default policy gateway for command evaluation.
- Structured audit logging for accepted and dropped events.
- No private key material stored in service code.
