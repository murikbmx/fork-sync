# fork-sync

Daily GitHub Actions job that keeps the murikbmx LineageOS forks up to date.

- Fork list is read from the `murikbmx/android` manifest (`default.xml`, `snippets/*.xml`, and the `gmscompat` branch), so new forks are picked up automatically.
- Forks whose parent is `LineageOS/*`: `merge-upstream` on `lineage-24.0` (merge or fast-forward, never force).
- Forks with a `gmscompat` branch: `lineage-24.0` is merged into it on the server.
- Watches the AOSP tag that LineageOS pins; when it changes, the AOSP-based gmscompat forks need a manual merge.
- Conflicts open an issue labelled `sync-conflict` (one per problem, commented on repeat, closed automatically when the sync succeeds again).
- Forks with a non-LineageOS parent (crDroid, TheMuppets, microG) or no parent are skipped.

Manual run: Actions → Fork sync → Run workflow (optional dry run).
Secret `FORK_SYNC_TOKEN`: fine-grained PAT, Contents read/write on the forks.
