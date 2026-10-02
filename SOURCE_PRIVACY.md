# Source privacy and private recovery settings

Keep reusable code in Git, but store user-specific configuration and operational
snapshots outside Git. A private repository is not a substitute for separating
credentials from source.

- `.env` holds local login names, password hashes and service credentials. Never commit it.
- `config/private_sources.json` holds the private survey spreadsheet ID, existing
  measurement-key prefix, workbook/report names and extraction-version label.
  It is ignored by Git. Copy `config/private_sources.example.json` to that path
  when configuring a new machine, and fill in the values locally.
- Restore the original private settings **before** importing into an existing
  survey database. Changing the key prefix can create different measurement identities.
  The migration only relocates configuration; it does not rename existing DB records.
- Root-level `*_FIX_REPORT_*.md` and `*_FIX_STATUS_*.md` are private operational
  snapshots, ignored by Git. Keep these with private backups, not public source docs.
- `_backups/` is ignored. Private snapshots can contain account names, author
  emails and activity records even when actual passwords are excluded. Keep the
  backup destination unshared. Back up local settings separately from `.env` secrets.

Default example account names are `viewer` and `admin`. After the account migration,
login names and password hashes live in MySQL `accounts`; `.env` contains only the
legacy bootstrap identities. Account DB backups must also remain private.
The UI uses the generic label `User Survey` without changing its `user_sheet` data key.

Ignoring or editing a current file does not remove earlier copies from Git history.
Any coordinated history rewrite and force-push requires separate approval.
