# Scheduled-Report E-mail Setup

The Scheduled Reports module e-mails generated reports (CSV / Excel) to a single
pre-configured recipient. SMTP credentials are read **only** from environment
variables — nothing is hardcoded.

## 1. Required `.env` variables

Add these to `Backend/.env` (a template is provided in `Backend/.env.example`):

| Variable                 | Required | Description                                                        | Example                         |
|--------------------------|----------|--------------------------------------------------------------------|---------------------------------|
| `SMTP_HOST`              | ✅       | SMTP server hostname                                               | `smtp.gmail.com`                |
| `SMTP_PORT`              | ✅       | SMTP port — `587` for TLS (default), `465` for SSL                 | `587`                           |
| `SMTP_USERNAME`          | ✅       | SMTP login (the sending account)                                   | `you@gmail.com`                 |
| `SMTP_PASSWORD`          | ✅       | SMTP password — for Gmail, a 16-char **App Password**              | `abcd efgh ijkl mnop`           |
| `SMTP_FROM_EMAIL`        | ❌       | "From" address (defaults to `SMTP_USERNAME` if omitted)           | `you@gmail.com`                 |
| `REPORT_RECIPIENT_EMAIL` | ❌       | Destination address (defaults to `ptshivaji8@gmail.com`)           | `ops@example.com`               |

The three ✅ variables are mandatory. If any are missing, e-mail sending is
**disabled gracefully** — the app still starts, a warning is logged, and the
Scheduled Reports page shows a "Configure SMTP" section listing the missing vars.

## 2. Generating a Gmail App Password

Gmail does **not** accept your normal account password over SMTP. Create an App Password:

1. Enable **2-Step Verification**: Google Account → **Security** → *2-Step Verification*.
2. Go to Google Account → **Security** → **App passwords**
   (or visit https://myaccount.google.com/apppasswords).
3. Choose app **Mail**, device **Other**, name it e.g. `Kalyon Reports`.
4. Google shows a **16-character** password (e.g. `abcd efgh ijkl mnop`).
5. Put it in `.env` as `SMTP_PASSWORD` (spaces optional).

Then set:

```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_gmail_address@gmail.com
SMTP_PASSWORD=your_16_char_app_password
SMTP_FROM_EMAIL=your_gmail_address@gmail.com
REPORT_RECIPIENT_EMAIL=where_to_send@example.com
```

## 3. Testing e-mail delivery

1. Restart the backend so the new `.env` values load
   (`uvicorn app.main:app --reload`). On startup the log shows either:
   - `SMTP configured — e-mail sending ENABLED. host=... port=...`, or
   - `SMTP is NOT configured ... Missing environment variable(s): ...`
2. Open the frontend → **Scheduled Reports**.
3. Click **📧 Test Email** (top-right).
   - **Configured:** a sample Excel report is generated and e-mailed to
     `REPORT_RECIPIENT_EMAIL`; a success toast appears.
   - **Not configured:** a clear error toast appears (which variables are missing) —
     it never fails silently.
4. **Run Now** (▶) on any schedule generates that schedule's report and e-mails it.

## 4. Logs

Each send logs: connection status (TLS/SSL established), authentication, recipient,
subject, attachment name, and final **SUCCESS/FAILED** status. Failures include the
full exception (`exc_info`) for troubleshooting.

## 5. Security

- `.env` is git-ignored (`Backend/.gitignore`); only `.env.example` is committed.
- Never commit real credentials. Rotate/revoke the Gmail App Password if leaked.
