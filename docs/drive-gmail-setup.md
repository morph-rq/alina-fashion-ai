# Google Drive and Gmail setup

Status: implemented and tested against a simulated Google service only. **No real Google account has been
connected or verified yet**; the steps below need the account owner. Requirements were checked against
Google's documentation on 2026-10-18 (sources at the end).

## What the app does with each permission

| Capability (Connections page) | Google scope | Google classification | What the app does |
| --- | --- | --- | --- |
| Pick Drive files; write exports and recovery archives | `drive.file` | non-sensitive | Reads only files you pick in the Google Picker; creates its own "Alina Fashion exports" and "Alina Fashion recovery archives" folders. |
| Watch a Drive folder unattended | `drive.readonly` | restricted | Lists the selected folder and follows Drive change tracking for it. Google's consent covers all of Drive; the app filters to the folder. |
| Read e-mail matching a query | `gmail.readonly` | restricted | Reads messages matching your query and start date. Never deletes, labels, archives, marks read or sends. There is no label-only Gmail permission. |

Why a separate folder permission: with `drive.file`, access is per file the user chooses or the app creates.
Google does not document that picking a *folder* grants access to the files inside it, so the app does not
claim it. For unattended folder ingestion, `drive.readonly` is required and the page explains that before
consent.

Google consent is granular: if you untick a permission, the app records what was actually granted and keeps
the related features off.

## 1. Google Cloud project (account owner)

1. <https://console.cloud.google.com/> → create a project (for example "Alina Fashion local").
2. **APIs & Services → Library**: enable **Google Drive API**, **Gmail API** and (for picking files)
   **Google Picker API**.
3. **Google Auth Platform / OAuth consent screen**: user type **External** (or Internal if you use a Google
   Workspace organisation). Add your own address as a **test user**.
   * In **Testing** status, Google issues refresh tokens that expire after **7 days** (unless only basic
     profile scopes are requested). The Connections page will then show *Reconnect*.
   * To avoid weekly reconnection, publish the app ("In production"). Restricted scopes (`drive.readonly`,
     `gmail.readonly`) then show an "unverified app" warning unless Google verifies the app; restricted-scope
     verification and, for server-side storage of such data, a security assessment, apply to public apps.
     For a single operator's own account, accepting the unverified-app warning is the usual choice; confirm
     the current policy in Google's documentation.
4. **Clients → Create client → Web application.** Authorised redirect URI, exactly:
   `http://127.0.0.1:8001/connections/google/callback`
   (Google allows `http` for loopback addresses. If you open the app at another address, set
   `FASHION_PUBLIC_BASE_URL` and register the matching URI.)
5. Download the client JSON.

For the Picker (optional, to select individual files): create an **API key** restricted to the Picker API
and to the HTTP referrer `http://127.0.0.1:8001/*`, and note the project **number**. Put them in `.env`:

```
FASHION_GOOGLE_API_KEY=...
FASHION_GOOGLE_APP_ID=<project number>
```

The Picker page loads Google's official `https://apis.google.com/js/api.js` loader, which is dynamic and
cannot be pinned with Subresource Integrity; it is loaded only when these two settings are present.

## 2. Configure the app

Either upload the client JSON on **Connections → One-time Google setup** (stored DPAPI-protected under
`data/private`, outside backups) or set in `.env`:

```
FASHION_GOOGLE_CLIENT_ID=....apps.googleusercontent.com
FASHION_GOOGLE_CLIENT_SECRET=...
```

Restart the app after changing `.env`. Never paste these values into chat or commit them.

## 3. Connect

1. **Connections → Connect a Google account**: tick the capabilities, read the explanations, tick
   *I have read what each permission allows*, **Continue to Google**, choose the account and consent.
2. The app verifies the `state` value (and PKCE) on return, records the account e-mail and the granted
   scopes, and stores the refresh token protected. Reconnecting with a *different* account is refused.
3. Add sources and destinations under the account:
   * **Gmail source**: a search such as `label:alina-orders` and a start date. This is the import boundary.
     The first sync lists matching mail from the start date; later syncs use Gmail history. Use
     *Backfill from date* to deliberately import older mail; already captured messages are skipped.
   * **Watch folder**: paste the folder link (needs the folder permission).
   * **Pick Drive files…**: opens the Picker (needs the API key and project number).
   * **Exports folder / Recovery archive folder**: folders created by the app.
4. **Sync now** or wait for the schedule (every 15 minutes by default, per source).

## How capture works

* Each Drive file version (`headRevisionId`/checksum) and each Gmail message is stored once. Replays are
  recognised by account + remote id + version. A changed Drive file becomes new evidence; earlier versions
  stay. Removed or unshared files are noted on their earlier captures, which are kept.
* Google Docs/Sheets/Slides are exported (Sheets → XLSX, Docs/Slides → PDF) and the export format is recorded.
* E-mail: the original message is stored as `message/rfc822`; headers, a plain-text body and attachments are
  extracted locally. HTML is converted to text without loading links, images or tracking pixels.
  Unsupported attachments are recorded as *ignored* with the reason; undecodable ones as *failed*.
* Checkpoints (Drive page token, Gmail history id) are saved together with the items observed. A failed
  download is recorded as a failed inbox item with a retry job before the checkpoint moves on. If Gmail
  history has expired (HTTP 404), the app performs a bounded full resync from the start date and reports it.
* Throttling (429/5xx) is retried with backoff; revoked or expired consent marks the connection
  *needs reconnect* and stops its jobs.
* Sync runs only while the app is running. After a stop it continues from the saved checkpoints.
  Continuous capture while the computer is off would need the app to run as a service elsewhere.

## Disconnect

**Disconnect** revokes the token at Google, deletes it locally and pauses the sources. Captured evidence and
records remain. You can also remove the app at <https://myaccount.google.com/permissions>.

## Not implemented

Live Google Sheets editing (the app exports XLSX, it does not edit Sheets in place), push notifications
(polling is used on localhost), sending e-mail, and non-Gmail providers.

## Sources

* Drive scopes: <https://developers.google.com/workspace/drive/api/guides/api-specific-auth>
* Drive change tracking: <https://developers.google.com/workspace/drive/api/guides/manage-changes>
* Gmail scopes: <https://developers.google.com/workspace/gmail/api/auth/scopes>
* Gmail synchronisation: <https://developers.google.com/workspace/gmail/api/guides/sync>
* OAuth web-server flow: <https://developers.google.com/identity/protocols/oauth2/web-server>
* Refresh-token expiry: <https://developers.google.com/identity/protocols/oauth2>
