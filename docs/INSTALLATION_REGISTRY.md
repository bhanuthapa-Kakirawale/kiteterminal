# Installation registry: location, Microsoft Store Python, verification

Applies to the build with `backend/installation_state.py` as changed on 2026-11-12 (post-V12 closure).
Operator-facing. Nothing here touches the database.

## 1. What the registry is

The terminal keeps two identical **installation records** (ids, paths, timestamps, schema version names; no
account data):

| Record | Where | Purpose |
|---|---|---|
| beside the database | `backend\kite_terminal.db.installation.json` | binds the database's identity (file id, application_id, newest schema) |
| registry | `<registry dir>\<40 hex>.json` (hash of the database path) | survives the deletion of the database, its folder or the whole repository, so a start can tell "established installation, database missing" from a true first run |

`<registry dir>` is `KT_INSTALLATION_REGISTRY_DIR` when set, else `%LOCALAPPDATA%\KiteTerminal\installations`.

The registry is **evidence, not a gate**: if it cannot be written, the start (and `migrate-v12`, `restore-backup`)
still succeeds, the beside-database record still binds the identity, and the failure is logged as
`event=installation.record_failed … INSTALLATION_REGISTRY_UNAVAILABLE …`. `acknowledge-database --confirm` is the
exception: it writes the beside-database record and then stops with that error, so set up the registry before using it.

## 2. Why it fails under Microsoft Store Python

Store Python runs as an **MSIX-packaged app**. Windows redirects a packaged app's writes under `%LOCALAPPDATA%` and
`%APPDATA%` into the package's private store
(`%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\LocalCache\…`, or `D:\WpSystem\<SID>\…`
when the Store app is installed on another drive). On this machine that store is on D: while `%LOCALAPPDATA%` is on C:,
so renaming the temporary record onto its final name crossed volumes and failed with **WinError 17**
(`ERROR_NOT_SAME_DEVICE`), observed at the V12 migration (D5) and on every start since. A record written there would
also be visible only to that one Store package and deleted when that package is reset or uninstalled.

So the build **never writes the registry under AppData from Store Python**. It refuses before creating anything and
tells you what to do. It never picks another directory on its own.

## 3. Identify the runtime

```powershell
Get-Content .venv\pyvenv.cfg
python -m backend.installation_state
```

- `pyvenv.cfg` `home = …\WindowsApps\PythonSoftwareFoundation.Python…` means Store Python. A python.org build shows
  e.g. `…\AppData\Local\Programs\Python\Python313`.
- The check prints `store_python_packaged: True` or `False`, the registry directory in use, and `location:`
  (`OK` or the reason it is refused).

## 4. Fix A (recommended): an explicit registry directory

Requirements for the directory: an **absolute** path on a **fixed local drive**; **outside** the repository (it must
survive the repository's deletion); **outside** the Python installation and `.venv`; **outside** `%LOCALAPPDATA%` and
`%APPDATA%` when the interpreter is Store Python; writable by the account that runs the terminal; never a network share,
a removable disk, a cloud-synced folder (OneDrive) or `%TEMP%`. The code refuses a relative path, the repository, the
Python installation and (Store Python) AppData. The rest is the operator's responsibility.

Recommended: `%USERPROFILE%\KiteTerminal\installations` (here `C:\Users\<you>\KiteTerminal\installations`).

1. Stop the terminal (runbook §1.2).
2. Set it **once, as a user environment variable**, so the terminal and every `db_maintenance` command see the same
   value. `.env` is not suitable: the server reads it, most `db_maintenance` commands do not.
   ```powershell
   [Environment]::SetEnvironmentVariable('KT_INSTALLATION_REGISTRY_DIR', "$env:USERPROFILE\KiteTerminal\installations", 'User')
   ```
3. **Open a new console** (already-open consoles, and any scheduled task started before, keep the old environment).
   Confirm: `$env:KT_INSTALLATION_REGISTRY_DIR` prints the path.
4. Check the location and that a file can really be written and renamed there (creates the directory, writes and
   removes one probe file; never opens the database):
   ```powershell
   python -m backend.installation_state --probe
   ```
   Expected now: `location: OK`, `probe: OK`, `verdict: NOT_WRITTEN` (exit 1: the record itself does not exist yet).
5. Start the terminal normally. Its log must show `event=installation.recorded by=startup … registry=<your directory>`
   and no `installation.record_failed`.
6. Verify (section 6).

Undo: `[Environment]::SetEnvironmentVariable('KT_INSTALLATION_REGISTRY_DIR', $null, 'User')`, new console. Keep the
directory and its records: deleting them removes evidence the start relies on (runbook "Never delete").

## 5. Fix B (alternative): rebuild `.venv` from python.org Python

Appropriate when you would rather keep the default location, or when the Store interpreter causes other problems. It
changes the audited runtime, so treat it as a build change:

- Do not replace the existing `.venv` in place. Build a new one beside it (`py -3.13 -m venv .venv_pyorg`, using a
  python.org installer, never the Store), `pip install -r requirements-dev.txt`, run the full test suite on it, and
  only then swap the folders with the terminal stopped. Keep the old `.venv` until the new one has run a normal start.
- Recovery: rename the folders back. Neither the database nor the records change.
- The registry records already written with Fix A stay valid. If you keep `KT_INSTALLATION_REGISTRY_DIR` set, a
  python.org interpreter uses the same directory, so nothing moves.

## 6. Verify a successful registry write

```powershell
python -m backend.installation_state
echo "exit=$LASTEXITCODE"
```

Success: `verdict: OK`, exit 0. That means the registry record exists, is readable, and names the same `install_id`,
`application_id` and `canonical_path` as the record beside the database. Other verdicts:

| Verdict | Meaning | Action |
|---|---|---|
| `LOCATION_REFUSED` | the directory breaks a rule in section 4 (the reason is printed) | choose another directory (section 4) |
| `PROBE_FAILED` | `--probe` could not write and rename a file there | fix permissions or free space; check the path is local |
| `NOT_WRITTEN` | the location is fine; no record yet | start the terminal once, then re-check |
| `RECORD_UNREADABLE` | a record exists but cannot be read | do **not** delete it; runbook §4.4 `RECORD_UNREADABLE` |
| `MISMATCH` | the registry record names another installation | stop; runbook §4.4 (`acknowledge-database --confirm` only after inspection) |

## 7. When it fails

- The start is not affected; the database is not touched; trading safety is unchanged. The log line
  `installation.record_failed … INSTALLATION_REGISTRY_UNAVAILABLE` names the cause and this document.
- Never delete the record beside the database, never edit either record by hand, never point the variable at the
  repository to get past the check.
- Old temporaries from before this fix (`…\LocalCache\Local\KiteTerminal\installations\*.json.tmp<pid>`) are harmless,
  are never read as records, and may be left in place.

## 8. Running these commands from Bash or the Claude Code `!` prefix

The runbook's commands are written for PowerShell. Bash (Git Bash; the Claude Code `!` prefix uses it) treats `\` as an
escape character. An unquoted Windows path loses its backslashes **before** the program sees it:

| | Typed in Bash / `!` | What Python receives |
|---|---|---|
| ✖ | `--offsite-record F:\KiteTerminal_Backup\x.json` | `F:KiteTerminal_Backupx.json` (a *drive-relative* path: this is how the first V12 migration attempt was refused, `OFFSITE_BACKUP_REFUSED`) |
| ✖ | `.venv\Scripts\python.exe` | `.venvScriptspython.exe` (command not found) |
| ✖ | `--backup D:\My Backups\x_bak` (space, unquoted) | two arguments `D:My` and `Backupsx_bak` |
| ✔ | `--offsite-record 'F:\KiteTerminal_Backup\x.json'` | exactly that |
| ✔ | `--backup 'D:\My Backups\x_bak'` | exactly that (space kept) |
| ✔ | `.venv/Scripts/python.exe` or `--backup F:/KiteTerminal_Backup/x_bak` | forward slashes: Windows and Python accept them |

Rules:
- Put every Windows path in **single quotes**. Inside single quotes Bash changes nothing: `\`, `:`, spaces and `$`
  are passed as typed. Double quotes are not safe: `$` still expands and `\\` collapses to `\`.
- Always write the drive with its backslash or slash (`F:\…`, `F:/…`). `F:something` means "relative to the current
  directory on F:".
- Before a command that matters, print what the program will receive:
  `printf '%s\n' 'F:\KiteTerminal_Backup\x.json'`.
- In Bash the exit code is `$?` (`echo "exit=$?"`). `$LASTEXITCODE` exists only in PowerShell, and in Bash it prints an
  empty value.
- In PowerShell, backslashes need no quoting, but a path with spaces does: `'D:\My Backups\x_bak'`.
