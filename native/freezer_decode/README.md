# Bounded Nitro freezer decoder

This module uses the public OffchainLabs geth fork at commit
`5eee8f546e7602b7deeef7909f291bbebc77e233`. The module replacement resolves the
exact upstream source rather than an unpublished local checkout. Dependency
licenses remain with their respective modules.

The input is line-delimited JSON containing bounded compressed header, body,
receipt and hash records. The decoder validates transaction and receipt roots,
bloom, record sizes and page-local parent links before emitting records. It
cannot recover internal traces or independently prove consensus finality.

Run `go test ./...` and `go build` on an authorized Linux build host with Go
1.24 or later. Historical binary hashes and operator policies are not shipped;
a newly compiled binary must not be substituted into an old hash-bound grant.
