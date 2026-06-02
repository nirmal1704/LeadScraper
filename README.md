# [LeadScraper](https://your-app.vercel.app)

Find small local businesses in India without websites and pitch them one.

---

## What it does

You describe the kind of businesses you're looking for in plain English. The app searches Google Maps at a neighbourhood level, checks each business for a working website, and scores every lead. No website means they need you — those come first. When it's done, you download an Excel file with everything.

## Tech stack

| | |
|---|---|
| Frontend | Next.js on Vercel |
| Backend | FastAPI on Render |
| Browser automation | Playwright |
| AI | Groq (Llama 3.1) |
| Auth | Firebase |
| Database | Firestore |
| File storage | Firebase Storage |

## How to use it

1. Sign in with Google
2. Type what you're looking for — e.g. *"yoga studios and dance academies in Pune, small local businesses only"*
3. Optionally pick which sources to search. Leave it blank and the AI decides
4. Click **Start**
5. Watch leads appear in real time as they're found
6. When it finishes, click **Download Excel**

The Excel file is sorted by priority. Hot leads have no website at all — call those first.

---

v0.0.1
