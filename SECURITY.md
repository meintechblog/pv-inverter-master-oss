# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately** through GitHub:
open the repository's **Security** tab and choose **Report a vulnerability**
(GitHub private vulnerability reporting). Do not open a public issue for
security problems.

Please include the affected version, a description of the problem and,
if possible, steps to reproduce it.

## Supported versions

Only the latest release receives security fixes.

## Known design limits

See the "Security notes" section in the [README](README.md): the web
interface has no login and must only be run in a trusted network, and the
nightly auto-update is enabled by default without signature verification.
