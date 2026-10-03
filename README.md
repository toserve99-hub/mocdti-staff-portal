# MoCDTI Staff Portal (Flask + PostgreSQL, HR-created accounts)

## Deploy
1. Push this folder to a **private** GitHub repo.
2. Render > New > Blueprint > pick the repo (creates the web service + PostgreSQL).
3. Set env vars `ADMIN_EMAIL` and `ADMIN_PASSWORD` (a temporary password for the first HR/Admin).
4. Open the site, sign in with those; you are forced to set your own password. Then **delete `ADMIN_PASSWORD`**
   from Render (it is only used once, when there are no users).

## Managing officers (HR/Admin > "Users & Audit" tab)
- Create account: name, email, role. The system shows a one-time temporary password; give it to the officer.
- The officer signs in, and must set their own password (min. 10 characters) before using the portal.
- Reset password, change role, disable/enable at any time (disabling takes effect immediately).
- 5 wrong passwords locks the account for 15 minutes.
- Every login, edit, account change and password reset is in the audit log.

## Roles
| Role | View | Add/Edit/Quarantine | Delete, users, audit, import | Sees HR-only fields |
|---|---|---|---|---|
| admin (HR) | yes | yes | yes | yes |
| officer | yes | yes | no | no |
| general | yes | no | no | no |

HR-only fields: date of birth, marital status, staff/SSNIT no., qualifications, emergency contact, remarks.

## Moving your existing data
Old file > Import & Backup > Download backup (.json). On the new site, sign in as HR/Admin > Restore from backup.

## Before real data goes in
Test with dummy records, enable Render database backups, and confirm hosting/data-protection
requirements (Act 843) with your Ministry. Passwords are stored hashed (never readable), but the site must be HTTPS (Render provides this).
