# Security policy

FuseIoT controls physical equipment. Please report vulnerabilities privately
to **security@fuseiot.dev** rather than opening a public issue. Include steps
to reproduce and the affected version. We aim to acknowledge reports within 3
working days.

## Supported versions

| Version | Supported |
|---|---|
| 2.x | Yes |
| < 2.0 | No |

## Built-in protections

- The server requires an API token to listen on non-loopback addresses, and
  compares tokens in constant time.
- Only the commands a capability declares can be invoked remotely, whether
  from the REST API, rules or cloud. Parameters are validated, and request
  bodies are limited to 64 KB.
- The dashboard is served with a restrictive Content-Security-Policy and
  `frame-ancestors 'none'`.
- The image runs as a non-root user. The provided `docker-compose.yml` also
  makes the filesystem read-only, drops all capabilities and sets
  `no-new-privileges`.

See [docs/deployment.md](docs/deployment.md#3-security-checklist) for the
operator checklist.
