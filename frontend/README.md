# My Booth Agent — Frontend

**Tagline:** हर बूथ की समझ

This is the website users open in the browser.  
It talks to your backend API and uses Firebase for login.

You do **not** need to know JavaScript to deploy this.  
Follow the steps below one by one.

---

## What you will end up with

- Website live at: **https://www.myboothagent.com**
- Users sign in with email + password
- They choose a constituency and chat with Arjun
- Language: English or Hindi
- Voice: microphone to speak questions; Listen on answers (needs Sarvam on backend)

---

## Things you need before starting

1. A computer (Windows, Mac, or Linux)
2. An internet connection
3. A Google account (for Firebase / GCP)
4. Your domain: **myboothagent.com** (you already have this)
5. About 30–60 minutes the first time

Optional but useful later:
- Your backend already running (Cloud Run URL), so chat answers work in production

---

# PART A — Install tools on your computer (one time only)

## Step 1: Install Node.js

Node.js lets your computer build the website files.

1. Open: https://nodejs.org  
2. Download the **LTS** version (the one marked “Recommended”).  
3. Run the installer. Click Next until it finishes.  
4. Restart your terminal / command prompt.

**Check it worked:**

- Windows: open **Command Prompt** or **PowerShell**
- Mac: open **Terminal**

Type this and press Enter:

```bash
node -v
```

You should see something like `v20.x.x` or `v22.x.x`.  
If you see an error, Node is not installed correctly — repeat Step 1.

Also check:

```bash
npm -v
```

You should see a number like `10.x.x`.

---

## Step 2: Install Firebase CLI

This tool uploads your website to Google’s hosting.

In the same terminal, type:

```bash
npm install -g firebase-tools
```

Wait until it finishes.

**Check it worked:**

```bash
firebase --version
```

You should see a version number.

---

## Step 3: Log in to Firebase

```bash
firebase login
```

A browser window opens. Sign in with the Google account that owns your Firebase / GCP project.  
Allow the permissions. When the terminal says you are logged in, you are done.

---

# PART B — Create a Firebase project (one time)

1. Open https://console.firebase.google.com  
2. Click **Add project** (or use an existing GCP project).  
3. Give it a name, e.g. `my-booth-agent`.  
4. You can turn off Google Analytics if you do not need it.  
5. Wait until the project is ready.

### Enable Email/Password login

1. In the Firebase console, open your project.  
2. Left menu → **Build** → **Authentication**.  
3. Click **Get started**.  
4. Choose **Sign-in method**.  
5. Click **Email/Password** → Enable → Save.

### Create a Web App (to get config keys)

1. Project Overview (gear icon) → **Project settings**.  
2. Scroll to **Your apps**.  
3. Click the **Web** icon (`</>`).  
4. Nickname: `booth-web`.  
5. **Do not** tick “Firebase Hosting” yet if asked (we will set it up from the terminal).  
6. Register the app.  
7. You will see a config object like:

```js
apiKey: "AIza..."
authDomain: "something.firebaseapp.com"
projectId: "my-booth-agent"
appId: "1:123:web:abc"
```

**Copy these four values** into a notepad. You need them in the next part.

### Create a test user (so you can log in)

1. Authentication → **Users** → **Add user**.  
2. Enter an email and a password.  
3. Save. This is the account you will use on the website.

---

# PART C — Prepare the website on your computer

## Step 4: Open the frontend folder

The frontend code is in:

```
booth-agent/frontend
```

In the terminal:

```bash
cd path/to/booth-agent/frontend
```

Example (change to your real path):

```bash
cd ~/booth-agent/frontend
```

---

## Step 5: Install project packages

Still inside `frontend` folder:

```bash
npm install
```

This downloads all libraries. Wait until it finishes (may take 1–3 minutes).

---

## Step 6: Create your secret config file

Still inside `frontend`:

**Windows (PowerShell):**

```powershell
Copy-Item .env.example .env.local
```

**Mac / Linux:**

```bash
cp .env.example .env.local
```

Open `.env.local` in any text editor (Notepad, VS Code, TextEdit).

Fill it like this (use **your** values from Firebase):

```
NEXT_PUBLIC_API_URL=https://YOUR-BACKEND-URL
NEXT_PUBLIC_FIREBASE_API_KEY=AIza...your key...
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=your-project.firebaseapp.com
NEXT_PUBLIC_FIREBASE_PROJECT_ID=your-project-id
NEXT_PUBLIC_FIREBASE_APP_ID=1:123:web:abc
```

**About NEXT_PUBLIC_API_URL**

- For testing with a local backend: `http://localhost:8080`  
- For production: your Cloud Run URL, e.g.  
  `https://booth-backend-xxxxx-as.a.run.app`  
  (no slash at the end)

Save the file.

---

## Step 7: Test the website on your computer (optional but recommended)

```bash
npm run dev
```

Open a browser and go to:

```
http://localhost:3000
```

You should see the login page with the gold logo and tagline **हर बूथ की समझ**.

- If Firebase is configured: sign in with the user you created.  
- If not configured: it runs in **demo mode** (no real login).

Press `Ctrl + C` in the terminal to stop the local server when finished.

---

# PART D — Build and deploy to the internet

## Step 8: Build the production files

Inside `frontend`:

```bash
npm run build
```

This creates a folder called `out` with the finished website.  
If there are errors, read the red text — often a missing value in `.env.local`.

---

## Step 9: Connect this folder to Firebase Hosting

First time only:

```bash
firebase projects:list
```

Confirm your project appears. Then:

```bash
firebase use your-project-id
```

(Replace `your-project-id` with the real one from Firebase settings.)

Initialize hosting (first time):

```bash
firebase init hosting
```

When asked:

| Question | Answer |
|----------|--------|
| Use an existing project? | Yes → select your project |
| What do you want to use as your public directory? | **out** |
| Configure as a single-page app? | **Yes** |
| Set up automatic builds with GitHub? | **No** (for now) |
| File out/index.html already exists. Overwrite? | **No** |

---

## Step 10: Deploy

```bash
npm run build
firebase deploy --only hosting
```

When it finishes, Firebase prints a URL like:

```
https://your-project-id.web.app
```

Open that URL in your browser. Your site is live on the internet.

---

# PART E — Connect your domain www.myboothagent.com

## Step 11: Add the domain in Firebase

1. Firebase Console → your project → **Hosting**.  
2. Click **Add custom domain**.  
3. Enter: `www.myboothagent.com`  
4. Follow the on-screen steps.

Firebase will show you **DNS records** to add (usually a TXT record for verification, then A or CNAME records).

## Step 12: Update DNS at your domain provider

Log in to wherever you bought **myboothagent.com** (GoDaddy, Namecheap, Google Domains, Cloudflare, etc.).

Add the records Firebase shows you. Typical pattern:

| Type | Name | Value |
|------|------|--------|
| TXT | (as shown) | (verification string from Firebase) |
| A or CNAME | www | (values Firebase gives) |

Also set the **apex** domain if you want:

- `myboothagent.com` → redirect to `www.myboothagent.com`  
  (Firebase can help with this, or your DNS provider’s “redirect” feature.)

DNS changes can take **a few minutes to 48 hours**.  
Usually under 1 hour.

## Step 13: Wait for SSL

Firebase automatically issues a free HTTPS certificate for `www.myboothagent.com`.  
When status becomes **Connected**, open:

```
https://www.myboothagent.com
```

You are done.

---

# Everyday updates (after the first deploy)

Whenever you change the website code:

```bash
cd path/to/booth-agent/frontend
npm run build
firebase deploy --only hosting
```

That is all. No need to repeat the domain setup.

---

# Connecting the real backend

1. Deploy the **backend** to Cloud Run (see the main project `docs/ARCHITECTURE.md` and backend README).  
2. Put the Cloud Run URL into `.env.local` as `NEXT_PUBLIC_API_URL`.  
3. Rebuild and redeploy the frontend:

```bash
npm run build
firebase deploy --only hosting
```

4. On Cloud Run, allow the frontend origin (CORS is already open in the sample backend; you can tighten later).

---

# Common problems (simple fixes)

| Problem | What to do |
|---------|------------|
| `node -v` not found | Install Node.js again and restart the terminal |
| `firebase: command not found` | Run `npm install -g firebase-tools` again |
| Build fails about env | Check `.env.local` has no spaces around `=` and no quotes needed |
| Login fails | Create a user under Authentication → Users; enable Email/Password |
| Chat fails | Backend not running or wrong `NEXT_PUBLIC_API_URL` |
| Mic does nothing / permission error | Allow microphone in the browser address bar; use HTTPS (or localhost) |
| Listen does nothing | Backend needs `SARVAM_API_KEY`; check `/voice/synthesize` works |
| Domain not working | Wait for DNS; check records match Firebase exactly |
| Site shows old version | Hard refresh: Ctrl+Shift+R (Windows) or Cmd+Shift+R (Mac) |

---

# Project structure (you do not need to edit these to deploy)

```
frontend/
├── public/logo.png          ← your gold emblem
├── src/app/page.tsx         ← main screens (login, select, chat)
├── src/i18n/messages.ts     ← English + Hindi text
├── src/lib/api.ts           ← talks to backend
├── src/lib/firebase.ts      ← login
├── .env.example             ← template for secrets
├── firebase.json            ← hosting config
└── README.md                ← this file
```

---

# Summary checklist

- [ ] Node.js installed (`node -v`)
- [ ] Firebase CLI installed (`firebase --version`)
- [ ] `firebase login` done
- [ ] Firebase project created
- [ ] Email/Password auth enabled
- [ ] Test user created
- [ ] Web app registered (copied apiKey, etc.)
- [ ] `.env.local` filled
- [ ] `npm install` done
- [ ] `npm run build` succeeds
- [ ] `firebase deploy --only hosting` succeeds
- [ ] Custom domain `www.myboothagent.com` connected
- [ ] Backend URL set in `.env.local` and redeployed

When all boxes are checked, **https://www.myboothagent.com** is live with **हर बूथ की समझ**.
