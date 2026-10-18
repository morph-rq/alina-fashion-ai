# Upload to GitHub and host the app

## 1. Create a private GitHub repository

Use a **private** repository because this is business software. The repository is configured to exclude databases, customer files, uploaded images, reports, backups, spreadsheets, and `.env` secrets.

From this project folder in PowerShell:

```powershell
git add .
git status
git commit -m "Initial clean app website"
git branch -M main
git remote add origin https://github.com/fashion-work/app.git
git push -u origin main
```

Before committing, check `git status`. It must not list `.env`, `data`, `backups`, customer PDFs, uploaded images, or spreadsheets.

## 2. Host it from the GitHub repository

GitHub Pages cannot run this application because Pages hosts static files and this app needs a Python server and a database. Use the GitHub repository as the source, then deploy it as a Render Web Service.

1. Create a Render account and connect GitHub.
2. In Render, choose **New → Blueprint**.
3. Select the `app` repository under the `fashion-work` GitHub account.
4. Render reads `render.yaml` and creates the web service plus a persistent data disk.
5. Enter a strong private website password for `FASHION_APP_PASSWORD` and the server-side AI API key for `FASHION_AI_API_KEY` when Render asks.
6. Approve the Blueprint and wait for the first deploy.
7. Open the generated `https://...onrender.com` address on the computer and phone. Sign in with username `alina` and the password you chose.

The persistent disk is essential: it keeps the SQLite database, uploads, product images, library files, and generated reports after a redeploy. Do not switch the service to an ephemeral/free setup for real business data.

## 3. Publish future changes

```powershell
git add .
git commit -m "Describe the change"
git push
```

Render automatically redeploys after each push to `main`. Application data stays on the persistent disk and is not stored in GitHub.

## Security checklist

- Keep the GitHub repository private.
- Keep the website password private; a private repository alone does not protect the deployed website.
- Never commit `.env` or paste API keys into source files.
- Keep customer databases, exports, uploads, backups, and reports out of Git.
- Use one Render instance with the attached disk; SQLite is not suitable for multiple web-service instances writing simultaneously.
- Download periodic backups of the business data.
